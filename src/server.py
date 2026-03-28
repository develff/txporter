"""
txporter - REST API server
Exposes endpoints to trigger transaction sync from financial accounts.
"""

import re
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests as _requests
from flask import Flask, jsonify, render_template, request
from aqbanking import AqBankingClient
from config import load_config
import setup as bank_setup
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
config = load_config()

# Stores running aqbanking processes waiting for TAN confirmation
# { account_id: {"proc": Popen, "client": AqBankingClient, "account": dict} }
_pending_syncs = {}

# Stores in-progress bank setup sessions { setup_id: SetupSession }
_pending_setups = {}

# ── Scheduler ─────────────────────────────────────────────────────────────────
_scheduler_last_run = None  # (date, configured_time) of last scheduled run


def get_scheduler_config() -> dict:
    """Read scheduler section from banks.json."""
    try:
        return bank_setup.load_config().get("scheduler", {})
    except Exception as e:
        logger.warning(f"Could not read scheduler config: {e}")
        return {}


def _user_tz(sched_cfg: dict) -> ZoneInfo:
    """Return a ZoneInfo for the stored timezone, defaulting to UTC."""
    tz_name = sched_cfg.get("timezone") or "UTC"
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning(f"Timezone '{tz_name}' not found (tzdata missing?), falling back to UTC")
        return ZoneInfo("UTC")


def _compute_next_run(sched_cfg: dict):
    """Return ISO timestamp of next scheduled run in the user's timezone, or None."""
    if not sched_cfg.get("enabled") or not sched_cfg.get("time"):
        return None
    try:
        h, m = map(int, sched_cfg["time"].split(":"))
        now = datetime.now(_user_tz(sched_cfg))
        next_run = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        return next_run.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def _fire_webhook(webhook_url: str, account_id: str, error: str) -> None:
    """POST failure notification to the configured webhook URL."""
    if not webhook_url:
        return
    try:
        _requests.post(webhook_url, json={
            "account": account_id,
            "error": error,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, timeout=10)
        logger.info(f"Webhook fired for {account_id}: {error}")
    except Exception as e:
        logger.error(f"Webhook POST failed for {account_id}: {e}")


def _start_pending_timeout(account_id: str, webhook_url: str) -> None:
    """Start a 5-minute timeout watcher for a pending re-auth sync."""
    def watcher():
        _time.sleep(300)
        if account_id in _pending_syncs:
            _pending_syncs.pop(account_id, None)
            msg = "Re-auth timeout after 5 min"
            logger.error(f"Scheduled sync timeout for {account_id}: {msg}")
            _save_last_sync_error(account_id, "timeout", msg)
            _fire_webhook(webhook_url, account_id, msg)
    threading.Thread(target=watcher, daemon=True).start()


def _run_scheduled_sync() -> None:
    """Run sync for all enabled, connected accounts (called by scheduler thread)."""
    logger.info("Running scheduled sync")
    sched_cfg = get_scheduler_config()
    webhook_url = sched_cfg.get("webhook_url", "")
    for account in list(config["accounts"]):
        if not account.get("aqbanking_id"):
            continue
        if account.get("enabled") is False:
            continue
        account_id = account["id"]
        result = start_sync(account)
        if result["status"] == "pending":
            _start_pending_timeout(account_id, webhook_url)
        elif result["status"] == "error":
            _save_last_sync_error(account_id, "error", result.get("message", "sync error"))
            _fire_webhook(webhook_url, account_id, result.get("message", "sync error"))


def _scheduler_loop() -> None:
    """Background thread: fires scheduled sync at the configured daily time."""
    global _scheduler_last_run
    logger.info("Scheduler thread started")
    _last_alive_log_hour = -1
    while True:
        try:
            sched_cfg = get_scheduler_config()
            if sched_cfg.get("enabled") and sched_cfg.get("time"):
                tz = _user_tz(sched_cfg)
                now = datetime.now(tz)
                h, m = map(int, sched_cfg["time"].split(":"))
                if now.hour != _last_alive_log_hour:
                    _last_alive_log_hour = now.hour
                    logger.info(
                        f"Scheduler active — daily at {sched_cfg['time']} "
                        f"({sched_cfg.get('timezone', 'UTC')}), "
                        f"user time {now.strftime('%H:%M')} "
                        f"(last run: {_scheduler_last_run or 'never'})"
                    )
                run_key = (now.date(), sched_cfg["time"])
                if now.hour == h and now.minute == m and _scheduler_last_run != run_key:
                    _scheduler_last_run = run_key
                    logger.info(f"Scheduler triggering sync at {now.strftime('%H:%M:%S')}")
                    threading.Thread(target=_run_scheduled_sync, daemon=True).start()
        except Exception as e:
            logger.error(f"Scheduler check error: {e}")
        _time.sleep(30)


threading.Thread(target=_scheduler_loop, daemon=True, name="txporter-scheduler").start()


@app.route("/", methods=["GET"])
def index():
    """Serve the web UI."""
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/scheduler", methods=["GET"])
def scheduler_get():
    """Return current scheduler config and next scheduled run time."""
    sched_cfg = get_scheduler_config()
    tz = _user_tz(sched_cfg)
    return jsonify({
        "enabled": sched_cfg.get("enabled", False),
        "time": sched_cfg.get("time", ""),
        "webhook_url": sched_cfg.get("webhook_url", ""),
        "timezone": sched_cfg.get("timezone", "UTC"),
        "next_run": _compute_next_run(sched_cfg),
        "user_time_now": datetime.now(tz).strftime("%H:%M"),
    })


@app.route("/scheduler", methods=["POST"])
def scheduler_post():
    """Update scheduler config (enabled, time, webhook_url)."""
    body = request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get("enabled", False))
    time_str = (body.get("time") or "").strip()
    webhook_url = (body.get("webhook_url") or "").strip()
    timezone_name = (body.get("timezone") or "UTC").strip()

    if enabled and time_str:
        if not re.match(r"^\d{2}:\d{2}$", time_str):
            return jsonify({"error": "time must be in HH:MM format"}), 400
        h, m = map(int, time_str.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return jsonify({"error": "time out of range"}), 400
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, KeyError):
        return jsonify({"error": f"Unknown timezone '{timezone_name}'"}), 400

    cfg = bank_setup.load_config()
    cfg["scheduler"] = {
        "enabled": enabled, "time": time_str,
        "webhook_url": webhook_url, "timezone": timezone_name,
    }
    bank_setup.save_config(cfg)
    sched_cfg = cfg["scheduler"]
    return jsonify({
        "enabled": sched_cfg["enabled"],
        "time": sched_cfg["time"],
        "webhook_url": sched_cfg["webhook_url"],
        "timezone": sched_cfg["timezone"],
        "next_run": _compute_next_run(sched_cfg),
    })


@app.route("/sync", methods=["POST"])
def sync_all():
    """Start sync for all enabled, fully configured accounts.

    Optional body: { "from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD", "days": 30 }
    """
    body = request.get_json(force=True, silent=True) or {}
    from_date = body.get("from_date") or None
    to_date = body.get("to_date") or None
    days = int(body.get("days") or 30)
    results = {}
    for account in config["accounts"]:
        if not account.get("aqbanking_id"):
            continue
        if account.get("enabled") is False:
            results[account["id"]] = {"status": "skipped", "message": "Account is disabled"}
            continue
        results[account["id"]] = start_sync(account, from_date=from_date, to_date=to_date, days=days)
    return jsonify(results)


@app.route("/sync/<account_id>", methods=["POST"])
def sync_one(account_id):
    """Start sync for a single account by ID.

    Optional body: { "from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD", "days": 30,
                     "export_format": "json"|"csv" }
    """
    account = next((a for a in config["accounts"] if a["id"] == account_id), None)
    if not account:
        return jsonify({"error": f"Account '{account_id}' not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    from_date = body.get("from_date") or None
    to_date = body.get("to_date") or None
    days = int(body.get("days") or 30)
    export_format = body.get("export_format", "").lower() or None
    return jsonify(start_sync(account, from_date=from_date, to_date=to_date, days=days, export_format=export_format))


@app.route("/sync/<account_id>/confirm", methods=["POST"])
def confirm_one(account_id):
    """Confirm TAN approval for a pending sync.

    Query params:
      ?dry_run=true        — return raw parsed transactions, no forwarding to targets
      ?export_format=json  — return raw transactions as JSON (for browser download)
      ?export_format=csv   — return transactions as CSV text (for browser download)
    """
    if account_id not in _pending_syncs:
        return jsonify({"error": f"No pending sync for '{account_id}'"}), 404
    dry_run = request.args.get("dry_run", "").lower() == "true"
    export_format = request.args.get("export_format", "").lower() or None
    return jsonify(complete_sync(account_id, dry_run=dry_run, export_format=export_format))


@app.route("/accounts", methods=["GET"])
def list_accounts():
    """List all configured accounts."""
    cfg = bank_setup.load_config()
    return jsonify([
        {
            "id": a["id"],
            "name": a["name"],
            "type": a["type"],
            "enabled": a.get("enabled", True),
            **({"aqbanking_id": a["aqbanking_id"]} if "aqbanking_id" in a else {}),
            **({"iban": a["iban"]} if "iban" in a else {}),
            **({"account_number": a["account_number"]} if "account_number" in a else {}),
            **({"bank_code_aq": a["bank_code_aq"]} if "bank_code_aq" in a else {}),
            **({"account_type_label": bank_setup._ACCOUNT_TYPE_LABELS.get(
                a["account_type_label"].lower(), a["account_type_label"]
            )} if "account_type_label" in a else {}),
            **({"last_sync_at": a["last_sync_at"]} if "last_sync_at" in a else {}),
            **({"last_sync_status": a["last_sync_status"]} if "last_sync_status" in a else {}),
            **({"last_sync_error": a["last_sync_error"]} if "last_sync_error" in a else {}),
        }
        for a in cfg["accounts"]
    ])


@app.route("/accounts/<account_id>/toggle", methods=["POST"])
def toggle_account(account_id):
    """Enable or disable an account (excluded from Sync All when disabled)."""
    cfg = bank_setup.load_config()
    account = next((a for a in cfg["accounts"] if a["id"] == account_id), None)
    if not account:
        return jsonify({"error": f"Account '{account_id}' not found"}), 404
    account["enabled"] = not account.get("enabled", True)
    bank_setup.save_config(cfg)
    config["accounts"] = cfg["accounts"]
    return jsonify({"status": "ok", "enabled": account["enabled"]})


@app.route("/accounts/<account_id>/rename", methods=["POST"])
def rename_account(account_id):
    """Update the display name of an account."""
    body = request.get_json(force=True, silent=True) or {}
    new_name = body.get("name", "").strip()
    if not new_name:
        return jsonify({"error": "Field 'name' is required"}), 400
    cfg = bank_setup.load_config()
    account = next((a for a in cfg["accounts"] if a["id"] == account_id), None)
    if not account:
        return jsonify({"error": f"Account '{account_id}' not found"}), 404
    account["name"] = new_name
    bank_setup.save_config(cfg)
    config["accounts"] = cfg["accounts"]
    return jsonify({"status": "ok"})


@app.route("/accounts/<account_ref>", methods=["DELETE"])
def delete_account(account_ref):
    """Remove an account by aqbanking_id (integer) or account id (string).

    Useful for removing incomplete registrations that have no aqbanking_id yet.
    """
    cfg = bank_setup.load_config()
    before = len(cfg["accounts"])

    # Try numeric aqbanking_id first, fall back to string account id
    try:
        numeric_id = int(account_ref)
        cfg["accounts"] = [a for a in cfg["accounts"] if a.get("aqbanking_id") != numeric_id]
    except ValueError:
        numeric_id = None

    if len(cfg["accounts"]) == before:
        # Either not an int, or no match by aqbanking_id — try string id
        cfg["accounts"] = [a for a in cfg["accounts"] if a.get("id") != account_ref]

    if len(cfg["accounts"]) == before:
        return jsonify({"error": f"No account found for '{account_ref}'"}), 404

    bank_setup.save_config(cfg)
    config["accounts"] = cfg["accounts"]
    return jsonify({"status": "ok", "deleted": account_ref})


@app.route("/aqbanking/accounts", methods=["GET"])
def list_aqbanking_accounts():
    """List all accounts known to AqBanking, with a flag for which are already in banks.json."""
    import subprocess
    result = subprocess.run(
        ["aqhbci-tool4", "listaccounts", "-v"], capture_output=True, text=True
    )
    all_aq = bank_setup._parse_listaccounts(result.stdout)
    cfg = bank_setup.load_config()
    configured_keys = {
        (a.get("account_number"), a.get("bank_code_aq"))
        for a in cfg["accounts"]
        if a.get("account_number") and a.get("bank_code_aq")
    }
    for acc in all_aq:
        acc["configured"] = (acc.get("account_number"), acc.get("bank_code")) in configured_keys
    return jsonify(all_aq)


@app.route("/aqbanking/accounts/<int:aqbanking_id>/import", methods=["POST"])
def import_aqbanking_account(aqbanking_id):
    """Add an AqBanking account to banks.json without re-running the setup wizard.

    Copies connection details (url, login, blz, …) from the existing banks.json
    entry that shares the same bank code.  Body: { "name": "...", "id": "..." }
    """
    import subprocess
    body = request.get_json(force=True, silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Field 'name' is required"}), 400

    result = subprocess.run(
        ["aqhbci-tool4", "listaccounts", "-v"], capture_output=True, text=True
    )
    all_aq = bank_setup._parse_listaccounts(result.stdout)
    aq_acc = next((a for a in all_aq if a.get("aqbanking_id") == aqbanking_id), None)
    if aq_acc is None:
        return jsonify({"error": f"AqBanking account {aqbanking_id} not found"}), 404

    cfg = bank_setup.load_config()

    # Verify not already configured
    bank_code = aq_acc.get("bank_code")
    account_number = aq_acc.get("account_number")
    for existing in cfg["accounts"]:
        if (existing.get("account_number") == account_number
                and existing.get("bank_code_aq") == bank_code):
            return jsonify({"error": f"Already configured as '{existing['id']}'"}), 409

    # Find a same-bank entry to copy connection details from
    same_bank = next(
        (a for a in cfg["accounts"] if a.get("bank_code_aq") == bank_code
         or a.get("blz") == bank_code),
        None
    )
    if same_bank is None:
        return jsonify({"error": "No existing configuration for this bank. Run full setup first."}), 400

    account_id = body.get("id", "").strip() or name.lower().replace(" ", "_")
    if any(a["id"] == account_id for a in cfg["accounts"]):
        return jsonify({"error": f"Account id '{account_id}' already exists"}), 409

    new_account = {
        "id": account_id,
        "name": name,
        "type": same_bank.get("type", "fints"),
        "blz": same_bank.get("blz"),
        "url": same_bank.get("url"),
        "login": same_bank.get("login"),
        "hbci_version": same_bank.get("hbci_version", 300),
        "aqbanking_id": aqbanking_id,
        "account_number": account_number,
        "bank_code_aq": bank_code,
    }
    if same_bank.get("tan_mode"):
        new_account["tan_mode"] = same_bank["tan_mode"]
    if aq_acc.get("iban"):
        new_account["iban"] = aq_acc["iban"]
    if aq_acc.get("account_type_label"):
        new_account["account_type_label"] = aq_acc["account_type_label"]

    cfg["accounts"].append(new_account)
    bank_setup.save_config(cfg)
    config["accounts"] = cfg["accounts"]
    return jsonify({"status": "ok", "account": new_account})


@app.route("/setup/profiles", methods=["GET"])
def setup_profiles():
    """List available predefined bank profiles."""
    return jsonify(bank_setup.load_profiles())


@app.route("/setup", methods=["POST"])
def setup_start():
    """Step 1: register a new bank.

    Body (profile-based):
      { "bank": "dkb", "login": "...", "pin": "..." }

    Body (manual, no profile):
      { "blz": "...", "url": "...", "login": "...", "pin": "...",
        "name": "...", "hbci_version": 300 }

    Profile fields can be overridden per-request.
    """
    body = request.get_json(force=True, silent=True) or {}

    pin = body.get("pin")
    login = body.get("login")
    if not pin or not login:
        return jsonify({"error": "Fields 'login' and 'pin' are required"}), 400

    # Merge profile defaults with request overrides
    profile_key = body.get("bank")
    profile = {}
    if profile_key:
        profiles = bank_setup.load_profiles()
        if profile_key not in profiles:
            return jsonify({"error": f"Unknown bank profile '{profile_key}'. "
                                     f"Use GET /setup/profiles to list available profiles."}), 400
        profile = profiles[profile_key]

    blz = body.get("blz") or profile.get("blz")
    url = body.get("url") or profile.get("url")
    hbci_version = body.get("hbci_version") or profile.get("hbci_version", 300)
    tan_mode = body.get("tan_mode") or profile.get("tan_mode")
    account_id = body.get("id") or profile_key or blz
    name = body.get("name") or profile_key or blz

    if not blz or not url:
        return jsonify({"error": "Fields 'blz' and 'url' are required (or supply a known 'bank' profile)"}), 400

    # Check for duplicate account id
    cfg = bank_setup.load_config()
    if any(a["id"] == account_id for a in cfg["accounts"]):
        return jsonify({"error": f"Account id '{account_id}' already exists in banks.json"}), 409

    # Write PIN to pinfile
    bank_setup._write_pin(bank_setup.PINFILE, blz, login, pin)

    # Pre-create account entry in banks.json (without aqbanking_id)
    new_account = {
        "id": account_id,
        "name": name,
        "type": "fints",
        "blz": blz,
        "url": url,
        "login": login,
        "hbci_version": hbci_version,
    }
    if tan_mode is not None:
        new_account["tan_mode"] = tan_mode
    cfg["accounts"].append(new_account)
    bank_setup.save_config(cfg)
    config["accounts"] = cfg["accounts"]

    setup_id = str(uuid.uuid4())
    session = bank_setup.SetupSession(
        setup_id=setup_id,
        account_id=account_id,
        login=login,
        blz=blz,
        url=url,
        hbci_version=hbci_version,
        tan_mode=tan_mode,
        name=name,
    )
    try:
        result = session.step1_register()
    except Exception as e:
        # Roll back account entry on failure
        cfg = bank_setup.load_config()
        cfg["accounts"] = [a for a in cfg["accounts"] if a["id"] != account_id]
        bank_setup.save_config(cfg)
        config["accounts"] = cfg["accounts"]
        logger.error("Setup step 1 failed for %s: %s", account_id, e)
        return jsonify({"error": str(e)}), 500

    _pending_setups[setup_id] = session
    return jsonify(result), 202


@app.route("/setup/<setup_id>/acceptcert", methods=["POST"])
def setup_acceptcert(setup_id):
    """Step 1b: accept or reject the bank's certificate presented during getsysid.

    Body: { "accept": true }
    """
    if setup_id not in _pending_setups:
        return jsonify({"error": f"No pending setup for id '{setup_id}'"}), 404

    body = request.get_json(force=True, silent=True) or {}
    accept = body.get("accept")
    if accept is None:
        return jsonify({"error": "Field 'accept' (true/false) is required"}), 400

    session = _pending_setups[setup_id]
    try:
        result = session.step1b_accept_cert(bool(accept))
    except Exception as e:
        logger.error("Certificate acceptance failed for %s: %s", setup_id, e)
        if not accept:
            _pending_setups.pop(setup_id, None)
        return jsonify({"error": str(e)}), 500

    return jsonify(result), 202


@app.route("/setup/<setup_id>/tanmode", methods=["POST"])
def setup_tanmode(setup_id):
    """Step 2: set TAN mode (only needed if not auto-selected in step 1).

    Body: { "tan_mode": 7940 }
    """
    if setup_id not in _pending_setups:
        return jsonify({"error": f"No pending setup for id '{setup_id}'"}), 404

    body = request.get_json(force=True, silent=True) or {}
    tan_mode = body.get("tan_mode")
    if tan_mode is None:
        return jsonify({"error": "Field 'tan_mode' is required"}), 400

    session = _pending_setups[setup_id]
    try:
        result = session.step2_set_tanmode(int(tan_mode))
    except Exception as e:
        logger.error("Setup step 2 failed for %s: %s", setup_id, e)
        return jsonify({"error": str(e)}), 500

    return jsonify(result), 202


@app.route("/setup/<setup_id>/tan", methods=["POST"])
def setup_submit_tan(setup_id):
    """Step 3b: submit TAN for banks that require explicit TAN entry (e.g. Consorsbank).

    Only needed when /confirm returns status 'pending_tan'.
    Body: { "tan": "123456" }
    """
    if setup_id not in _pending_setups:
        return jsonify({"error": f"No pending setup for id '{setup_id}'"}), 404

    body = request.get_json(force=True, silent=True) or {}
    tan = body.get("tan")
    if not tan:
        return jsonify({"error": "Field 'tan' is required"}), 400

    session = _pending_setups.pop(setup_id)
    try:
        result = session.step3b_submit_tan(str(tan))
    except Exception as e:
        _pending_setups[setup_id] = session  # put back so caller can retry
        logger.error("TAN submission failed for %s: %s", setup_id, e)
        return jsonify({"error": str(e)}), 500

    config["accounts"] = bank_setup.load_config()["accounts"]
    return jsonify(result)


@app.route("/setup/<setup_id>/confirm", methods=["POST"])
def setup_confirm(setup_id):
    """Step 3: confirm TAN approval and finalize setup."""
    if setup_id not in _pending_setups:
        return jsonify({"error": f"No pending setup for id '{setup_id}'"}), 404

    session = _pending_setups.pop(setup_id)
    try:
        result = session.step3_confirm()
    except Exception as e:
        _pending_setups[setup_id] = session  # put back so caller can retry
        logger.error("Setup step 3 failed for %s: %s", setup_id, e)
        return jsonify({"error": str(e)}), 500

    if result.get("status") in ("pending_tan", "pending_account_select"):
        _pending_setups[setup_id] = session
        return jsonify(result), 202

    config["accounts"] = bank_setup.load_config()["accounts"]
    return jsonify(result)


@app.route("/setup/<setup_id>/selectaccount", methods=["POST"])
def setup_select_account(setup_id):
    """Step 3c: choose which AqBanking account to link when auto-detection failed.

    Body: { "aqbanking_id": 9 }
    """
    if setup_id not in _pending_setups:
        return jsonify({"error": f"No pending setup for id '{setup_id}'"}), 404

    body = request.get_json(force=True, silent=True) or {}
    aqbanking_id = body.get("aqbanking_id")
    if aqbanking_id is None:
        return jsonify({"error": "Field 'aqbanking_id' is required"}), 400

    session = _pending_setups.pop(setup_id)
    try:
        result = session.select_account(int(aqbanking_id))
    except Exception as e:
        _pending_setups[setup_id] = session
        logger.error("Account selection failed for %s: %s", setup_id, e)
        return jsonify({"error": str(e)}), 500

    config["accounts"] = bank_setup.load_config()["accounts"]
    return jsonify(result)


@app.route("/status", methods=["GET"])
def status():
    """Return last sync status for all accounts."""
    # TODO: implement persistent status tracking
    return jsonify({"status": "not implemented yet"})


def start_sync(account: dict, from_date: str = None, to_date: str = None, days: int = 30,
               export_format: str = None) -> dict:
    """Start fetching transactions for a FinTS account.

    Blocks until aqbanking-cli has connected to the bank and determined whether
    a TAN is required.  Returns either:
      {"status": "ok", ...}      — completed inline (no TAN needed)
      {"status": "pending", ...} — push sent; caller must POST /sync/{id}/confirm

    export_format: "json" or "csv" — if set and bank completes without TAN, returns raw
    transactions instead of forwarding to targets (same behaviour as the confirm endpoint).
    """
    account_id = account["id"]
    logger.info(f"Starting sync for account: {account_id}")
    try:
        client = AqBankingClient(account)
        if account.get("type") == "fints":
            result = client.start_fetch(from_date=from_date, to_date=to_date, days=days)
            if result["status"] == "ok":
                transactions = result["transactions"]
                logger.info(f"Fetched {len(transactions)} transactions from {account_id} (no TAN)")
                if export_format in ("json", "csv"):
                    return {"status": "ok", "export_format": export_format, "transactions": transactions}
                stats = _forward_to_targets(transactions, account)
                _save_last_sync(account_id)
                return {"status": "ok", **stats}
            _pending_syncs[account_id] = {"client": client, "account": account}
            return {"status": "pending", "message": f"Confirm in banking app, then POST /sync/{account_id}/confirm"}
        else:
            transactions = client.fetch_transactions()
            logger.info(f"Fetched {len(transactions)} transactions from {account_id}")
            if export_format in ("json", "csv"):
                return {"status": "ok", "export_format": export_format, "transactions": transactions}
            stats = _forward_to_targets(transactions, account)
            _save_last_sync(account_id)
            return {"status": "ok", **stats}
    except Exception as e:
        logger.error(f"Error starting sync for {account_id}: {e}")
        return {"status": "error", "message": str(e)}


def complete_sync(account_id: str, dry_run: bool = False, export_format: str = None) -> dict:
    """Confirm TAN and complete the pending sync."""
    pending = _pending_syncs.pop(account_id)
    account = pending["account"]
    client = pending["client"]
    logger.info(f"Completing sync for account: {account_id}")
    try:
        transactions = client.complete_fetch()
        logger.info(f"Fetched {len(transactions)} transactions from {account_id}")
        if dry_run:
            return {"status": "dry_run", "transactions": transactions}
        if export_format in ("json", "csv"):
            return {"status": "ok", "export_format": export_format, "transactions": transactions}
        stats = _forward_to_targets(transactions, account)
        _save_last_sync(account_id)
        return {"status": "ok", **stats}
    except Exception as e:
        logger.error(f"Error completing sync for {account_id}: {e}")
        return {"status": "error", "message": str(e)}


def _save_last_sync(account_id: str) -> None:
    """Persist last_sync_at and ok status for an account in banks.json."""
    try:
        cfg = bank_setup.load_config()
        account = next((a for a in cfg["accounts"] if a["id"] == account_id), None)
        if account:
            account["last_sync_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            account["last_sync_status"] = "ok"
            account.pop("last_sync_error", None)
            bank_setup.save_config(cfg)
            config["accounts"] = cfg["accounts"]
    except Exception as e:
        logger.warning(f"Could not save last_sync_at for {account_id}: {e}")


def _save_last_sync_error(account_id: str, status: str, error: str) -> None:
    """Persist error/timeout sync status for an account in banks.json."""
    try:
        cfg = bank_setup.load_config()
        account = next((a for a in cfg["accounts"] if a["id"] == account_id), None)
        if account:
            account["last_sync_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            account["last_sync_status"] = status
            account["last_sync_error"] = error
            bank_setup.save_config(cfg)
            config["accounts"] = cfg["accounts"]
    except Exception as e:
        logger.warning(f"Could not save last_sync_error for {account_id}: {e}")


def _forward_to_targets(transactions: list, account: dict) -> dict:
    """Forward transactions to all enabled targets. Returns import stats from Firefly (if enabled)."""
    stats = {}
    for target_name, target_config in config["targets"].items():
        if not target_config.get("enabled"):
            continue
        if target_name == "firefly":
            from firefly import FireflyClient
            stats = FireflyClient(target_config).import_transactions(transactions, account)
        elif target_name == "csv":
            import csv as csv_module
            import os
            path = target_config["path"]
            os.makedirs(path, exist_ok=True)
            filename = f"{path}/{account['id']}.csv"
            with open(filename, "w", newline="") as f:
                writer = csv_module.DictWriter(f, fieldnames=["date", "amount", "description", "iban"])
                writer.writeheader()
                writer.writerows(transactions)
            logger.info(f"Wrote {len(transactions)} transactions to {filename}")
    return stats


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
