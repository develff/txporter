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


def _german_iban(account: dict) -> str | None:
    """Derive the German IBAN from BLZ + Kontonummer if not already stored.

    Works for all German bank accounts (DE IBANs).
    Returns None if required fields are missing or account number is too long.
    """
    iban = (account.get("iban") or "").strip()
    if iban:
        return iban
    blz = (account.get("blz") or account.get("bank_code_aq") or "").strip()
    acct_nr = (account.get("account_number") or "").strip()
    if not blz or not acct_nr or len(blz) != 8 or not blz.isdigit() or not acct_nr.isdigit():
        return None
    acct_nr = acct_nr.zfill(10)
    if len(acct_nr) > 10:
        return None
    # Standard DE IBAN: move "DE00" to end, replace letters with digits (D=13, E=14)
    numeric_str = blz + acct_nr + "1314" + "00"
    check_digits = 98 - int(numeric_str) % 97
    return f"DE{check_digits:02d}{blz}{acct_nr}"


def _iso_date(date_str: str) -> str:
    """Convert YYYYMMDD to YYYY-MM-DD; pass through any other format unchanged."""
    if date_str and len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    return date_str


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

    def get_tags(self) -> list:
        """Fetch all tag names from Firefly III (sorted)."""
        tags = []
        page = 1
        while True:
            response = requests.get(
                f"{self.base_url}/api/v1/tags",
                headers=self.headers,
                params={"limit": 100, "page": page},
            )
            if not response.ok:
                break
            data = response.json()
            for item in data.get("data", []):
                tag = item.get("attributes", {}).get("tag")
                if tag:
                    tags.append(tag)
            meta = data.get("meta", {}).get("pagination", {})
            if page >= meta.get("total_pages", 1):
                break
            page += 1
        return sorted(tags)

    def import_transactions(self, transactions: list, account: dict) -> dict:
        """Import a list of transactions into Firefly III.

        Returns {"found": N, "imported": Y, "skipped": Z, "errors": E}.
        Skips transactions whose external_id already exists in Firefly III.
        """
        found = len(transactions)
        imported = 0
        skipped = 0
        errors = 0

        if not transactions:
            return {"found": 0, "imported": 0, "skipped": 0, "errors": 0}

        currency = transactions[0].get("currency_code", "EUR")
        account_name = account.get("name", "")
        firefly_account_id = self._ensure_asset_account(account_name, currency, account)
        existing_ids = self._fetch_existing_external_ids(firefly_account_id)
        logger.info(
            "Firefly account '%s': %d existing external_ids loaded",
            account_name, len(existing_ids),
        )

        for tx in transactions:
            ext_id = tx.get("external_id", "")
            if ext_id and ext_id in existing_ids:
                logger.debug("Skipping duplicate external_id=%s", ext_id)
                skipped += 1
                continue
            result = self._create_transaction(tx, account, firefly_account_id)
            if result is True:
                imported += 1
                if ext_id:
                    existing_ids.add(ext_id)
            elif result is None:
                skipped += 1
            else:
                errors += 1

        logger.info(
            "Import complete: %d found, %d imported, %d skipped, %d errors",
            found, imported, skipped, errors,
        )
        return {"found": found, "imported": imported, "skipped": skipped, "errors": errors}

    def _ensure_asset_account(self, name: str, currency_code: str, account: dict) -> str | None:
        """Find or create the matching asset account in Firefly III.

        Matching priority (stable identifiers first, name last):
          1. IBAN — if the txporter account has one
          2. account_number — for depot/savings accounts without IBAN
          3. name — fallback for legacy accounts

        Returns the Firefly III account ID, or None on failure.
        """
        iban = _german_iban(account) or ""
        account_number = (account.get("account_number") or "").strip()

        response = requests.get(
            f"{self.base_url}/api/v1/accounts",
            headers=self.headers,
            params={"type": "asset", "limit": 100},
        )
        if response.ok:
            for a in response.json().get("data", []):
                attrs = a.get("attributes", {})
                if iban and attrs.get("iban") == iban:
                    logger.debug("Asset account matched by IBAN %s: %s", iban, name)
                    return a.get("id")
                if account_number and attrs.get("account_number") == account_number:
                    logger.debug("Asset account matched by account_number %s: %s", account_number, name)
                    return a.get("id")
                if attrs.get("name") == name:
                    logger.debug("Asset account matched by name: %s", name)
                    account_id = a.get("id")
                    if iban and not attrs.get("iban"):
                        self._backfill_iban(account_id, iban, account_number)
                    return account_id

        logger.info("Creating asset account: %s", name)
        payload = {
            "name": name,
            "type": "asset",
            "account_role": "defaultAsset",
            "currency_code": currency_code,
        }
        if iban:
            payload["iban"] = iban
        if account_number:
            payload["account_number"] = account_number
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

    def _backfill_iban(self, account_id: str, iban: str, account_number: str) -> None:
        """Write computed IBAN (and account_number) back to an existing Firefly account.

        One-time enrichment: after this, future lookups match by IBAN, not name,
        so renaming the account in txporter has no effect.
        """
        payload = {"iban": iban}
        if account_number:
            payload["account_number"] = account_number
        resp = requests.put(
            f"{self.base_url}/api/v1/accounts/{account_id}",
            headers=self.headers,
            json=payload,
        )
        if resp.ok:
            logger.info("Backfilled IBAN %s on Firefly account %s", iban, account_id)
        else:
            logger.warning("Could not backfill IBAN on Firefly account %s: %s", account_id, resp.text)

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

    def _create_transaction(self, tx: dict, account: dict, firefly_account_id: str | None = None) -> bool | None:
        """Create a single transaction in Firefly III.

        Returns True on success, None if skipped (zero amount), False on API error.
        """
        amount_eur = tx.get("amount_eur", 0.0)
        if not amount_eur:
            logger.debug("Skipping zero-amount transaction external_id=%s", tx.get("external_id"))
            return None
        is_withdrawal = amount_eur < 0
        tx_type = "withdrawal" if is_withdrawal else "deposit"
        amount = f"{abs(amount_eur):.2f}"
        account_name = account.get("name", "")
        remote_name = tx.get("remote_name") or None

        split = {
            "type": tx_type,
            "date": _iso_date(tx.get("date", "")),
            "amount": amount,
            "currency_code": tx.get("currency_code", ""),
            "description": tx.get("description") or _build_description(tx) or "(kein Verwendungszweck)",
            "external_id": tx.get("external_id", ""),
        }

        # For withdrawals: source = asset account, destination = expense account (auto-created by name)
        # For deposits: source = revenue account (auto-created by name), destination = asset account
        # Use firefly_account_id (stable) when available; fall back to name for new accounts.
        if is_withdrawal:
            if firefly_account_id:
                split["source_id"] = firefly_account_id
            else:
                split["source_name"] = account_name
            if remote_name:
                split["destination_name"] = remote_name
        else:
            if firefly_account_id:
                split["destination_id"] = firefly_account_id
            else:
                split["destination_name"] = account_name
            if remote_name:
                split["source_name"] = remote_name

        if tx.get("valuta_date"):
            split["book_date"] = _iso_date(tx["valuta_date"])
        if tx.get("end_to_end_reference"):
            split["sepa_ct_id"] = tx["end_to_end_reference"]
        if tx.get("primanota") and tx.get("primanota") != "0":
            split["internal_reference"] = tx["primanota"]

        if tx.get("category_name"):
            split["category_name"] = tx["category_name"]
        if tx.get("budget_name"):
            split["budget_name"] = tx["budget_name"]
        if tx.get("tags"):
            raw_tags = tx["tags"]
            split["tags"] = (
                [t.strip() for t in raw_tags.split(",") if t.strip()]
                if isinstance(raw_tags, str) else list(raw_tags)
            )
        if tx.get("foreign_amount"):
            split["foreign_amount"] = f"{abs(float(tx['foreign_amount'])):.8f}".rstrip("0").rstrip(".")
            if tx.get("foreign_currency_code"):
                split["foreign_currency_code"] = tx["foreign_currency_code"]

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
