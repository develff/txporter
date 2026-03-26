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


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/sync", methods=["POST"])
def sync_all():
    """Sync all configured accounts."""
    results = {}
    for account in config["accounts"]:
        results[account["id"]] = sync_account(account)
    return jsonify(results)


@app.route("/sync/<account_id>", methods=["POST"])
def sync_one(account_id):
    """Sync a single account by ID."""
    account = next((a for a in config["accounts"] if a["id"] == account_id), None)
    if not account:
        return jsonify({"error": f"Account '{account_id}' not found"}), 404
    result = sync_account(account)
    return jsonify(result)


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


def sync_account(account: dict) -> dict:
    """Fetch transactions from an account and forward to configured targets."""
    logger.info(f"Syncing account: {account['id']}")
    try:
        client = AqBankingClient(account)
        transactions = client.fetch_transactions()
        logger.info(f"Fetched {len(transactions)} transactions from {account['id']}")

        # Forward to targets
        for target_name, target_config in config["targets"].items():
            if not target_config.get("enabled"):
                continue
            forward_to_target(target_name, target_config, transactions, account)

        return {"status": "ok", "transactions": len(transactions)}
    except Exception as e:
        logger.error(f"Error syncing {account['id']}: {e}")
        return {"status": "error", "message": str(e)}


def forward_to_target(target_name: str, target_config: dict, transactions: list, account: dict):
    """Forward transactions to a specific target."""
    if target_name == "firefly":
        from firefly import FireflyClient
        client = FireflyClient(target_config)
        client.import_transactions(transactions, account)
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
