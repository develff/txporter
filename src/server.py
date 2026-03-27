"""
txporter - REST API server
Exposes endpoints to trigger transaction sync from financial accounts.
"""

from flask import Flask, jsonify, request
from aqbanking import AqBankingClient
from config import load_config
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
config = load_config()

# Stores running aqbanking processes waiting for TAN confirmation
# { account_id: {"proc": Popen, "client": AqBankingClient, "account": dict} }
_pending_syncs = {}


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
    return jsonify([
        {"id": a["id"], "name": a["name"], "type": a["type"]}
        for a in config["accounts"]
    ])


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
