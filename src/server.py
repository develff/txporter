"""
txporter - REST API server
Exposes endpoints to trigger transaction sync from financial accounts.
"""

import uuid

from flask import Flask, jsonify, request
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


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/sync", methods=["POST"])
def sync_all():
    """Start sync for all configured accounts."""
    results = {}
    for account in config["accounts"]:
        results[account["id"]] = start_sync(account)
    return jsonify(results)


@app.route("/sync/<account_id>", methods=["POST"])
def sync_one(account_id):
    """Start sync for a single account by ID."""
    account = next((a for a in config["accounts"] if a["id"] == account_id), None)
    if not account:
        return jsonify({"error": f"Account '{account_id}' not found"}), 404
    return jsonify(start_sync(account))


@app.route("/sync/<account_id>/confirm", methods=["POST"])
def confirm_one(account_id):
    """Confirm TAN approval for a pending sync."""
    if account_id not in _pending_syncs:
        return jsonify({"error": f"No pending sync for '{account_id}'"}), 404
    return jsonify(complete_sync(account_id))


@app.route("/accounts", methods=["GET"])
def list_accounts():
    """List all configured accounts."""
    cfg = bank_setup.load_config()
    return jsonify([
        {
            "id": a["id"],
            "name": a["name"],
            "type": a["type"],
            **({"aqbanking_id": a["aqbanking_id"]} if "aqbanking_id" in a else {}),
            **({"iban": a["iban"]} if "iban" in a else {}),
        }
        for a in cfg["accounts"]
    ])


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

    if result.get("status") == "pending_tan":
        _pending_setups[setup_id] = session  # keep alive for /tan submission
        return jsonify(result), 202

    return jsonify(result)


@app.route("/status", methods=["GET"])
def status():
    """Return last sync status for all accounts."""
    # TODO: implement persistent status tracking
    return jsonify({"status": "not implemented yet"})


def start_sync(account: dict) -> dict:
    """Start fetching transactions; for FinTS returns pending (awaiting TAN confirmation)."""
    account_id = account["id"]
    logger.info(f"Starting sync for account: {account_id}")
    try:
        client = AqBankingClient(account)
        if account.get("type") == "fints":
            proc = client.start_fetch()
            _pending_syncs[account_id] = {"proc": proc, "client": client, "account": account}
            return {"status": "pending", "message": f"Confirm in banking app, then POST /sync/{account_id}/confirm"}
        else:
            transactions = client.fetch_transactions()
            logger.info(f"Fetched {len(transactions)} transactions from {account_id}")
            _forward_to_targets(transactions, account)
            return {"status": "ok", "transactions": len(transactions)}
    except Exception as e:
        logger.error(f"Error starting sync for {account_id}: {e}")
        return {"status": "error", "message": str(e)}


def complete_sync(account_id: str) -> dict:
    """Confirm TAN and complete the pending sync."""
    pending = _pending_syncs.pop(account_id)
    account = pending["account"]
    client = pending["client"]
    proc = pending["proc"]
    logger.info(f"Completing sync for account: {account_id}")
    try:
        transactions = client.complete_fetch(proc)
        logger.info(f"Fetched {len(transactions)} transactions from {account_id}")
        _forward_to_targets(transactions, account)
        return {"status": "ok", "transactions": len(transactions)}
    except Exception as e:
        logger.error(f"Error completing sync for {account_id}: {e}")
        return {"status": "error", "message": str(e)}


def _forward_to_targets(transactions: list, account: dict):
    for target_name, target_config in config["targets"].items():
        if not target_config.get("enabled"):
            continue
        if target_name == "firefly":
            from firefly import FireflyClient
            FireflyClient(target_config).import_transactions(transactions, account)
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
