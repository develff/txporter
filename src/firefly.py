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
        for tx in transactions:
            self._create_transaction(tx, account)

    def _create_transaction(self, tx: dict, account: dict):
        """Create a single transaction in Firefly III."""
        minor_units = tx.get("amount_minor_units", 0)
        tx_type = "withdrawal" if minor_units < 0 else "deposit"
        amount = f"{abs(minor_units) / 100:.2f}"

        split = {
            "type": tx_type,
            "date": tx.get("date", ""),
            "amount": amount,
            "currency_code": tx.get("currency_code", ""),
            "description": _build_description(tx),
            "source_name": account.get("name", ""),
            "external_id": tx.get("external_id", ""),
        }

        if tx.get("valuta_date"):
            split["book_date"] = tx["valuta_date"]
        if tx.get("remote_name"):
            split["destination_name"] = tx["remote_name"]
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

        if response.status_code == 422:
            logger.info(
                "Skipping duplicate transaction external_id=%s: %s",
                tx.get("external_id"), response.text,
            )
            return

        if not response.ok:
            logger.error(
                "Failed to create transaction external_id=%s: %s",
                tx.get("external_id"), response.text,
            )
        else:
            logger.debug("Created transaction external_id=%s", tx.get("external_id"))
