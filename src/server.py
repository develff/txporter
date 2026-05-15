"""
txporter - REST API server
Exposes endpoints to trigger transaction sync from financial accounts.
"""

import csv as csv_module
import json
import os
import re
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests as _requests
from flask import Flask, jsonify, render_template, request, send_file
from aqbanking import AqBankingClient, aqbanking_is_busy
from config import load_config, ensure_config_exists
import setup as bank_setup
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB cap for CSV uploads
ensure_config_exists()
config = load_config()

_ISO_DATETIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

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
    except KeyError:
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
            "timestamp": datetime.now(timezone.utc).strftime(_ISO_DATETIME_FMT),
        }, timeout=10)
        logger.info(f"Webhook fired for {account_id}: {error}")
    except Exception:
        logger.exception(f"Webhook POST failed for {account_id}")


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
    if aqbanking_is_busy():
        logger.warning("Scheduled sync skipped — aqbanking-cli already running")
        return
    logger.info("Running scheduled sync")
    sched_cfg = get_scheduler_config()
    webhook_url = sched_cfg.get("webhook_url", "")
    for account in config["accounts"]:
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
        except Exception:
            logger.exception("Scheduler check error")
        _time.sleep(30)


threading.Thread(target=_scheduler_loop, daemon=True, name="txporter-scheduler").start()


@app.route("/", methods=["GET"])
def index():
    """Serve the web UI."""
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/config", methods=["GET"])
def config_get():
    """Return current target configuration (Firefly + CSV)."""
    cfg = bank_setup.load_config()
    targets = cfg.get("targets", {})
    firefly = targets.get("firefly", {})
    csv_target = targets.get("csv", {})
    return jsonify({
        "firefly": {
            "url": firefly.get("url", ""),
            "token": firefly.get("token", ""),
            "browser_url": firefly.get("browser_url", ""),
        },
        "csv": {
            "path": csv_target.get("path", "/home/txporter/output"),
        },
    })


@app.route("/config/firefly", methods=["POST"])
def config_firefly_post():
    """Save Firefly URL and token."""
    body = request.get_json(force=True, silent=True) or {}
    cfg = bank_setup.load_config()
    targets = cfg.setdefault("targets", {})
    firefly = targets.setdefault("firefly", {})
    if "url" in body:
        firefly["url"] = str(body["url"]).strip()
    if "token" in body:
        firefly["token"] = str(body["token"]).strip()
    if "browser_url" in body:
        firefly["browser_url"] = str(body["browser_url"]).strip()
    bank_setup.save_config(cfg)
    config["targets"] = cfg["targets"]
    return jsonify({"ok": True})


@app.route("/config/csv", methods=["POST"])
def config_csv_post():
    """Save CSV target settings."""
    body = request.get_json(force=True, silent=True) or {}
    cfg = bank_setup.load_config()
    targets = cfg.setdefault("targets", {})
    csv_target = targets.setdefault("csv", {})
    if "path" in body:
        csv_target["path"] = str(body["path"]).strip()
    bank_setup.save_config(cfg)
    config["targets"] = cfg["targets"]
    return jsonify({"ok": True})


@app.route("/config/firefly/test", methods=["POST"])
def config_firefly_test():
    """Test Firefly connectivity. Uses URL+token from request body, falls back to saved config."""
    body = request.get_json(force=True, silent=True) or {}
    if "url" in body or "token" in body:
        url = str(body.get("url", "")).strip().rstrip("/")
        token = str(body.get("token", "")).strip()
    else:
        cfg = bank_setup.load_config()
        firefly_cfg = cfg.get("targets", {}).get("firefly", {})
        url = firefly_cfg.get("url", "").rstrip("/")
        token = firefly_cfg.get("token", "")
    if not url:
        return jsonify({"ok": False, "error": "No URL configured"}), 400
    if not token:
        return jsonify({"ok": False, "error": "No token configured"}), 400
    try:
        from firefly import FireflyClient
        FireflyClient({"url": url, "token": token}).get_tags()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


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

    if webhook_url and not webhook_url.startswith(("http://", "https://")):
        return jsonify({"error": "webhook_url must start with http:// or https://"}), 400

    if enabled and time_str:
        if not re.match(r"^\d{2}:\d{2}$", time_str):
            return jsonify({"error": "time must be in HH:MM format"}), 400
        h, m = map(int, time_str.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return jsonify({"error": "time out of range"}), 400
    try:
        ZoneInfo(timezone_name)
    except KeyError:
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

    Optional body: { "dry_run": true, "export_format": "json"|"csv" }
      dry_run       — return raw parsed transactions, no forwarding to targets
      export_format — return raw transactions as JSON or CSV (for browser download)
    """
    if account_id not in _pending_syncs:
        return jsonify({"error": f"No pending sync for '{account_id}'"}), 404
    body = request.get_json(force=True, silent=True) or {}
    dry_run = bool(body.get("dry_run", False))
    export_format = (body.get("export_format") or "").lower() or None
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

    # Pre-create account entry in config.json (without aqbanking_id)
    if profile_key:
        new_account = {
            "id": account_id,
            "name": name,
            "catalog_ref": profile_key,
            "login": login,
        }
    else:
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
        logger.exception("Setup step 1 failed for %s", account_id)
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
        logger.exception("Certificate acceptance failed for %s", setup_id)
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
        logger.exception("Setup step 2 failed for %s", setup_id)
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
        logger.exception("TAN submission failed for %s", setup_id)
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
        logger.exception("Setup step 3 failed for %s", setup_id)
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
        logger.exception("Account selection failed for %s", setup_id)
        return jsonify({"error": str(e)}), 500

    config["accounts"] = bank_setup.load_config()["accounts"]
    return jsonify(result)


@app.route("/csv/fields", methods=["GET"])
def csv_fields():
    """List all Firefly fields available for CSV column mapping."""
    from csv_import import FIREFLY_FIELDS
    return jsonify(FIREFLY_FIELDS)


@app.route("/csv/preview", methods=["POST"])
def csv_preview():
    """Upload a CSV file and return its headers + first 5 data rows.

    Form fields: file (required), delimiter, encoding, skip_rows, join_multiline
    """
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    delimiter = request.form.get("delimiter", ",")
    encoding = request.form.get("encoding", "utf-8")
    join_multiline = request.form.get("join_multiline", "false").lower() == "true"
    try:
        skip_rows = int(request.form.get("skip_rows", 0))
    except ValueError:
        return jsonify({"error": "skip_rows must be an integer"}), 400
    from csv_import import preview_csv
    try:
        result = preview_csv(file.read(), delimiter=delimiter, encoding=encoding,
                             skip_rows=skip_rows, join_multiline=join_multiline)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/csv/import", methods=["POST"])
def csv_import_route():
    """Upload a CSV file and import transactions using the provided mapping.

    Form fields: file (required), mapping (JSON string, required)
    The mapping can be a full profile object or reference an existing mapping by id.
    """
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    mapping_json = request.form.get("mapping")
    if not mapping_json:
        return jsonify({"error": "Field 'mapping' (JSON) is required"}), 400
    try:
        mapping = json.loads(mapping_json)
    except ValueError:
        return jsonify({"error": "Field 'mapping' is not valid JSON"}), 400

    from csv_import import parse_and_map
    try:
        transactions = parse_and_map(file.read(), mapping)
    except Exception as e:
        logger.exception("CSV parse error")
        return jsonify({"error": str(e)}), 400

    account = {"name": mapping.get("account_name", "")}
    stats = _forward_to_targets(transactions, account)
    return jsonify({"status": "ok", "found": len(transactions), **stats})


@app.route("/csv/mappings", methods=["GET"])
def csv_mappings_list():
    """List all saved CSV mapping profiles."""
    from csv_import import load_mappings
    return jsonify(load_mappings())


@app.route("/csv/mappings", methods=["POST"])
def csv_mappings_save():
    """Save or update a CSV mapping profile.

    Body must include 'id' and 'name'. Replaces any existing profile with the same id.
    """
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("id") or not body.get("name"):
        return jsonify({"error": "Fields 'id' and 'name' are required"}), 400
    from csv_import import load_mappings, save_mappings
    mappings = load_mappings()
    mappings = [body if m["id"] == body["id"] else m for m in mappings]
    if not any(m["id"] == body["id"] for m in mappings):
        mappings.append(body)
    save_mappings(mappings)
    return jsonify(body)


@app.route("/csv/mappings/<mapping_id>", methods=["DELETE"])
def csv_mappings_delete(mapping_id):
    """Delete a saved CSV mapping profile by id."""
    from csv_import import load_mappings, save_mappings
    mappings = load_mappings()
    updated = [m for m in mappings if m["id"] != mapping_id]
    if len(updated) == len(mappings):
        return jsonify({"error": f"No mapping found for '{mapping_id}'"}), 404
    save_mappings(updated)
    return jsonify({"status": "ok"})


@app.route("/tags", methods=["GET"])
def get_tags():
    """Return all tag names from the configured Firefly III instance."""
    firefly_cfg = config.get("targets", {}).get("firefly")
    if not firefly_cfg or not (firefly_cfg.get("url") and firefly_cfg.get("token")):
        return jsonify([])
    from firefly import FireflyClient
    try:
        return jsonify(FireflyClient(firefly_cfg).get_tags())
    except Exception as e:
        logger.warning("Could not fetch tags from Firefly: %s", e)
        return jsonify([])


@app.route("/status", methods=["GET"])
def status():
    """Return last sync status for all accounts."""
    result = {}
    for account in config["accounts"]:
        account_id = account["id"]
        entry = {"id": account_id, "name": account.get("name", account_id)}
        if account.get("last_sync_at"):
            entry["last_sync_at"] = account["last_sync_at"]
            entry["last_sync_status"] = account.get("last_sync_status")
            if account.get("last_sync_error"):
                entry["last_sync_error"] = account["last_sync_error"]
        if account_id in _pending_syncs:
            entry["pending"] = True
        result[account_id] = entry
    return jsonify(result)


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
        logger.exception(f"Error starting sync for {account_id}")
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
        logger.exception(f"Error completing sync for {account_id}")
        return {"status": "error", "message": str(e)}


def _save_last_sync(account_id: str) -> None:
    """Persist last_sync_at and ok status for an account in banks.json."""
    try:
        cfg = bank_setup.load_config()
        account = next((a for a in cfg["accounts"] if a["id"] == account_id), None)
        if account:
            account["last_sync_at"] = datetime.now(timezone.utc).strftime(_ISO_DATETIME_FMT)
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
            account["last_sync_at"] = datetime.now(timezone.utc).strftime(_ISO_DATETIME_FMT)
            account["last_sync_status"] = status
            account["last_sync_error"] = error
            bank_setup.save_config(cfg)
            config["accounts"] = cfg["accounts"]
    except Exception as e:
        logger.warning(f"Could not save last_sync_error for {account_id}: {e}")


def _target_is_active(name: str, cfg: dict) -> bool:
    if name == "firefly":
        return bool(cfg.get("url") and cfg.get("token"))
    if name == "csv":
        return bool(cfg.get("path"))
    return False


def _forward_to_targets(transactions: list, account: dict) -> dict:
    """Forward transactions to all configured targets. Returns import stats from Firefly (if active)."""
    stats = {}
    for target_name, target_config in config["targets"].items():
        if not _target_is_active(target_name, target_config):
            continue
        if target_name == "firefly":
            from firefly import FireflyClient
            stats = FireflyClient(target_config).import_transactions(transactions, account)
            report_url = _write_import_report(stats.pop("rows", []), account)
            if report_url:
                stats["report_url"] = report_url
        elif target_name == "csv":
            path = target_config["path"]
            os.makedirs(path, exist_ok=True)
            filename = f"{path}/{account['id']}.csv"
            with open(filename, "w", newline="") as f:
                writer = csv_module.DictWriter(f, fieldnames=["date", "amount", "description", "iban"], extrasaction="ignore")
                writer.writeheader()
                writer.writerows(transactions)
            logger.info(f"Wrote {len(transactions)} transactions to {filename}")
    return stats


_REPORT_FIELDS = [
    "date", "valuta_date", "amount_eur", "currency_code",
    "description", "remote_name", "remote_iban", "remote_account_number",
    "external_id", "end_to_end_reference", "primanota",
    "category_name", "budget_name", "tags",
    "foreign_amount", "foreign_currency_code",
    "firefly_status",
]
_REPORT_DIR = "/home/txporter/output/reports"


def _write_import_report(rows: list, account: dict) -> str | None:
    """Write per-transaction import report CSV. Returns the download URL or None on failure."""
    if not rows:
        return None
    try:
        os.makedirs(_REPORT_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"import_{account.get('id', 'unknown')}_{ts}.csv"
        path = os.path.join(_REPORT_DIR, filename)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv_module.DictWriter(f, fieldnames=_REPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        logger.info("Wrote import report: %s", path)
        return f"/import-report/{filename}"
    except Exception:
        logger.exception("Failed to write import report")
        return None


@app.route("/import-report/<filename>", methods=["GET"])
def download_import_report(filename):
    """Download a previously generated import report CSV."""
    if not re.match(r'^import_[\w\-]+\.csv$', filename):
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(_REPORT_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"error": "Report not found"}), 404
    return send_file(path, mimetype="text/csv", as_attachment=True, download_name=filename)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8090)
