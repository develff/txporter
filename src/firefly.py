"""
txporter - Firefly III API client
Imports transactions into Firefly III via REST API.
"""

import requests
import logging
from datetime import datetime, timedelta

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

    def import_transactions(self, transactions: list, account: dict) -> dict:
        """Import a list of transactions into Firefly III.

        Returns {"found": N, "imported": Y, "skipped": Z}.
        Skips transactions whose external_id already exists in Firefly III.
        """
        found = len(transactions)
        imported = 0
        skipped = 0

        if not transactions:
            return {"found": 0, "imported": 0, "skipped": 0}

        currency = transactions[0].get("currency_code", "EUR")
        account_name = account.get("name", "")
        firefly_account_id = self._ensure_asset_account(account_name, currency)
        start_date = self._dedup_start_date(transactions)
        existing_ids = self._fetch_existing_external_ids(firefly_account_id, start_date=start_date)
        logger.info(
            "Firefly account '%s': %d existing external_ids loaded (from %s)",
            account_name, len(existing_ids), start_date or "beginning",
        )

        for tx in transactions:
            ext_id = tx.get("external_id", "")
            if ext_id and ext_id in existing_ids:
                logger.debug("Skipping duplicate external_id=%s", ext_id)
                skipped += 1
                continue
            if self._create_transaction(tx, account):
                imported += 1
                if ext_id:
                    existing_ids.add(ext_id)
            else:
                skipped += 1

        logger.info("Import complete: %d found, %d imported, %d skipped", found, imported, skipped)
        return {"found": found, "imported": imported, "skipped": skipped}

    def _ensure_asset_account(self, name: str, currency_code: str) -> str | None:
        """Create the asset account in Firefly III if it does not exist yet.

        Returns the Firefly III account ID, or None on failure.
        """
        response = requests.get(
            f"{self.base_url}/api/v1/accounts",
            headers=self.headers,
            params={"type": "asset", "limit": 100},
        )
        if response.ok:
            for a in response.json().get("data", []):
                if a.get("attributes", {}).get("name") == name:
                    logger.debug("Asset account already exists: %s", name)
                    return a.get("id")

        logger.info("Creating asset account: %s", name)
        payload = {"name": name, "type": "asset", "account_role": "defaultAsset", "currency_code": currency_code}
        resp = requests.post(
            f"{self.base_url}/api/v1/accounts",
            headers=self.headers,
            json=payload,
        )
        if resp.ok:
            logger.info("Created asset account: %s", name)
            return resp.json().get("data", {}).get("id")
        else:
            logger.error("Failed to create asset account %s: %s", name, resp.text)
            return None

    @staticmethod
    def _dedup_start_date(transactions: list, buffer_days: int = 7) -> str | None:
        """Return a Firefly-compatible start date (YYYY-MM-DD) for the dedup query.

        Takes the earliest transaction date in the batch and subtracts buffer_days
        to catch transactions that the bank may report with a slightly earlier date
        than the requested sync window.
        """
        dates = [tx["date"] for tx in transactions if tx.get("date")]
        if not dates:
            return None
        min_date = min(dates)  # YYYYMMDD
        try:
            dt = datetime.strptime(min_date, "%Y%m%d") - timedelta(days=buffer_days)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            logger.warning("Could not parse transaction date '%s', fetching all external_ids", min_date)
            return None

    def _fetch_existing_external_ids(self, firefly_account_id: str | None,
                                     start_date: str | None = None) -> set:
        """Fetch external_ids of existing transactions for the given asset account.

        start_date (YYYY-MM-DD): if provided, only transactions on or after this date
        are queried — sufficient for dedup within a bounded sync window and much faster
        than fetching the full history.
        """
        if firefly_account_id is None:
            return set()
        external_ids = set()
        page = 1
        params_base = {"limit": 100}
        if start_date:
            params_base["start"] = start_date
        while True:
            response = requests.get(
                f"{self.base_url}/api/v1/accounts/{firefly_account_id}/transactions",
                headers=self.headers,
                params={**params_base, "page": page},
            )
            if not response.ok:
                logger.warning("Could not fetch transactions for account %s (page %d): %s",
                               firefly_account_id, page, response.status_code)
                break
            data = response.json()
            rows = data.get("data", [])
            if not rows:
                break
            for row in rows:
                for split in row.get("attributes", {}).get("transactions", []):
                    ext_id = split.get("external_id", "")
                    if ext_id:
                        external_ids.add(ext_id)
            pagination = data.get("meta", {}).get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                break
            page += 1
        return external_ids

    def _create_transaction(self, tx: dict, account: dict) -> bool:
        """Create a single transaction in Firefly III. Returns True on success, False on skip/error."""
        amount_eur = tx.get("amount_eur", 0.0)
        if amount_eur == 0.0:
            logger.debug("Skipping zero-amount transaction external_id=%s", tx.get("external_id"))
            return False
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
            return False
        else:
            logger.debug("Created transaction external_id=%s", tx.get("external_id"))
            return True
