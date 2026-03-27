"""
txporter - Firefly III API client
Imports transactions into Firefly III via REST API.
"""

import requests
import logging

logger = logging.getLogger(__name__)

_NOTES_FIELDS = [
    "type", "sub_type", "command", "status", "unique_account_id", "unique_id",
    "ref_unique_id", "id_for_application", "session_id", "group_id", "acknowledge",
    "local_bank_code", "local_account_number", "remote_bank_code", "remote_account_number",
    "remote_iban", "remote_bic", "transaction_code", "transaction_key", "text_key",
    "bank_reference", "sequence", "charge", "period", "cycle", "execution_day",
    "estatement_number", "estatement_max_entries", "vop_result",
]


def _build_description(tx: dict) -> str:
    text = tx.get("transaction_text", "")
    purpose = tx.get("purpose", "")
    if text and purpose:
        return f"{text} – {purpose}"
    return text or purpose or ""


def _build_notes(tx: dict) -> str:
    lines = []
    for key in _NOTES_FIELDS:
        value = tx.get(key, "")
        if value and value != "0":
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


class FireflyClient:
    def __init__(self, config: dict):
        self.base_url = config["url"].rstrip("/")
        self.token = config["token"]
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def import_transactions(self, transactions: list, account: dict):
        """Import a list of transactions into Firefly III."""
        if transactions:
            currency = transactions[0].get("currency_code", "EUR")
            self._ensure_asset_account(account.get("name", ""), currency)
        for tx in transactions:
            self._create_transaction(tx, account)

    def _ensure_asset_account(self, name: str, currency_code: str):
        """Create the asset account in Firefly III if it does not exist yet."""
        response = requests.get(
            f"{self.base_url}/api/v1/accounts",
            headers=self.headers,
            params={"type": "asset", "limit": 100},
        )
        if response.ok:
            accounts = response.json().get("data", [])
            for a in accounts:
                if a.get("attributes", {}).get("name") == name:
                    logger.debug("Asset account already exists: %s", name)
                    return

        logger.info("Creating asset account: %s", name)
        payload = {"name": name, "type": "asset", "account_role": "defaultAsset", "currency_code": currency_code}
        resp = requests.post(
            f"{self.base_url}/api/v1/accounts",
            headers=self.headers,
            json=payload,
        )
        if resp.ok:
            logger.info("Created asset account: %s", name)
        else:
            logger.error("Failed to create asset account %s: %s", name, resp.text)

    def _create_transaction(self, tx: dict, account: dict):
        """Create a single transaction in Firefly III."""
        amount_eur = tx.get("amount_eur", 0.0)
        if amount_eur == 0.0:
            logger.debug("Skipping zero-amount transaction external_id=%s", tx.get("external_id"))
            return
        is_withdrawal = amount_eur < 0
        tx_type = "withdrawal" if is_withdrawal else "deposit"
        amount = f"{abs(amount_eur):.2f}"
        account_name = account.get("name", "")
        remote_name = tx.get("remote_name") or None

        split = {
            "type": tx_type,
            "date": tx.get("date", ""),
            "amount": amount,
            "currency_code": tx.get("currency_code", ""),
            "description": _build_description(tx),
            "external_id": tx.get("external_id", ""),
        }

        # For withdrawals: source = asset account, destination = expense account (auto-created)
        # For deposits: source = revenue account (auto-created), destination = asset account
        if is_withdrawal:
            split["source_name"] = account_name
            if remote_name:
                split["destination_name"] = remote_name
        else:
            split["destination_name"] = account_name
            if remote_name:
                split["source_name"] = remote_name

        if tx.get("valuta_date"):
            split["book_date"] = tx["valuta_date"]
        if tx.get("end_to_end_reference"):
            split["sepa_ct_id"] = tx["end_to_end_reference"]
        if tx.get("primanota") and tx.get("primanota") != "0":
            split["internal_reference"] = tx["primanota"]

        notes = _build_notes(tx)
        if notes:
            split["notes"] = notes

        payload = {"transactions": [split]}
        response = requests.post(
            f"{self.base_url}/api/v1/transactions",
            headers=self.headers,
            json=payload,
        )

        if not response.ok:
            data = response.json() if response.content else {}
            message = data.get("message", response.text)
            logger.error(
                "Failed to create transaction external_id=%s status=%s: %s",
                tx.get("external_id"), response.status_code, message,
            )
        else:
            logger.debug("Created transaction external_id=%s", tx.get("external_id"))
