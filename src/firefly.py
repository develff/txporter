"""
txporter - Firefly III API client
Imports transactions into Firefly III via REST API.
"""

import requests
import logging
import time
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
    _TIMEOUT = 30

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
                timeout=self._TIMEOUT,
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

        Returns {"found": N, "imported": Y, "skipped": Z, "errors": E, "rows": [...]}.
        rows contains one entry per transaction with all fields plus "firefly_status"
        (imported / skipped / error) for use in the import report CSV.
        """
        found = len(transactions)
        imported = 0
        skipped = 0
        errors = 0
        rows = []

        if not transactions:
            return {"found": 0, "imported": 0, "skipped": 0, "potential_duplicates": 0, "errors": 0, "rows": []}

        currency = transactions[0].get("currency_code", "EUR")
        account_name = account.get("name", "")
        t_start = time.monotonic()
        t0 = t_start
        firefly_account_id = self._ensure_asset_account(account_name, currency, account)
        logger.info("_ensure_asset_account took %.1fs", time.monotonic() - t0)
        start_date = self._dedup_start_date(transactions)
        t0 = time.monotonic()
        existing_ids, aq_date_amounts = self._fetch_existing_data(firefly_account_id, start_date)
        logger.info(
            "_fetch_existing_data took %.1fs — account '%s': %d external_ids, %d aq pairs (since %s)",
            time.monotonic() - t0, account_name, len(existing_ids), len(aq_date_amounts), start_date or "all time",
        )

        potential_duplicates = 0
        for tx in transactions:
            if not tx.get("description"):
                tx["description"] = _build_description(tx) or "(kein Verwendungszweck)"
            ext_id = tx.get("external_id", "")
            if ext_id and ext_id in existing_ids:
                logger.debug("Skipping duplicate external_id=%s", ext_id)
                skipped += 1
                rows.append({**tx, "firefly_status": "skipped"})
                continue
            # Secondary check: same date + same absolute amount already imported via txporter
            tx_date = _iso_date(tx.get("date", ""))[:10]
            tx_amt = f"{abs(tx.get('amount_eur', 0)):.2f}"
            if aq_date_amounts and (tx_date, tx_amt) in aq_date_amounts:
                logger.info("Potential duplicate (date+amount match): %s %s %s",
                            tx_date, tx_amt, ext_id)
                potential_duplicates += 1
                rows.append({**tx, "firefly_status": "potential_duplicate"})
                continue
            result = self._create_transaction(tx, account, firefly_account_id)
            if result is True:
                imported += 1
                logger.info("Imported: %s  %s  %.2f %s",
                            tx.get("date", ""), ext_id,
                            tx.get("amount_eur", 0), tx.get("currency_code", ""))
                if ext_id:
                    existing_ids.add(ext_id)
                rows.append({**tx, "firefly_status": "imported"})
            elif result is None:
                skipped += 1
                rows.append({**tx, "firefly_status": "skipped"})
            else:
                errors += 1
                rows.append({**tx, "firefly_status": "error"})

        logger.info(
            "Import complete: %d found, %d imported, %d skipped, %d potential duplicates, %d errors",
            found, imported, skipped, potential_duplicates, errors,
        )
        logger.info("Total import time: %.1fs", time.monotonic() - t_start)
        return {"found": found, "imported": imported, "skipped": skipped,
                "potential_duplicates": potential_duplicates, "errors": errors, "rows": rows}

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
            timeout=self._TIMEOUT,
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
            timeout=self._TIMEOUT,
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
            timeout=self._TIMEOUT,
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
        than the requested sync window. Accepts both YYYYMMDD and YYYY-MM-DD formats.
        """
        dates = [tx["date"] for tx in transactions if tx.get("date")]
        if not dates:
            return None
        min_date = min(dates)
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(min_date, fmt) - timedelta(days=buffer_days)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        logger.warning("Could not parse transaction date '%s', fetching all external_ids", min_date)
        return None

    def _fetch_existing_data(self, firefly_account_id: str | None,
                              start_date: str | None = None) -> tuple[set, set]:
        """Fetch external_ids and (date, abs_amount) pairs of existing transactions.

        Uses the global transactions endpoint so that transactions Firefly reclassifies
        to a different account (e.g. PayPal-Transit) are still found and deduplicated.

        Returns:
          external_ids — set of all existing external_id strings
          aq_date_amounts — set of (YYYY-MM-DD, abs_amount_str) for transactions
                            that were imported via txporter (external_id starts with
                            'txporter:'). Used as a secondary duplicate signal.

        start_date (YYYY-MM-DD): if provided, only transactions on or after this date
        are queried — sufficient for dedup within a bounded sync window.
        """
        if firefly_account_id is None:
            return set(), set()
        external_ids = set()
        aq_date_amounts = set()
        page = 1
        params_base = {"limit": 100}
        if start_date:
            params_base["start"] = start_date
        while True:
            response = requests.get(
                f"{self.base_url}/api/v1/transactions",
                headers=self.headers,
                params={**params_base, "page": page},
                timeout=self._TIMEOUT,
            )
            if not response.ok:
                logger.warning("Could not fetch global transactions (page %d): %s",
                               page, response.status_code)
                break
            data = response.json()
            rows = data.get("data", [])
            if not rows:
                break
            for row in rows:
                for split in row.get("attributes", {}).get("transactions", []):
                    self._collect_split_data(split, external_ids, aq_date_amounts)
            pagination = data.get("meta", {}).get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                break
            page += 1
        return external_ids, aq_date_amounts

    @staticmethod
    def _collect_split_data(split: dict, external_ids: set, aq_date_amounts: set) -> None:
        ext_id = split.get("external_id", "")
        if ext_id:
            external_ids.add(ext_id)
        if not ext_id.startswith("txporter:"):
            return
        date = (split.get("date") or "")[:10]
        try:
            amt = f"{abs(float(split.get('amount', 0))):.2f}"
        except (ValueError, TypeError):
            amt = ""
        if date and amt:
            aq_date_amounts.add((date, amt))

    @staticmethod
    def _build_account_routing(is_withdrawal: bool, account_name: str,
                                firefly_account_id: str | None, remote_name: str | None) -> dict:
        """Return source/destination fields for a transaction split.

        Withdrawals: asset account is source, remote party is destination.
        Deposits: remote party is source, asset account is destination.
        Uses firefly_account_id (stable internal ID) when available, falls back to name.
        """
        own_id_key = "source_id" if is_withdrawal else "destination_id"
        own_name_key = "source_name" if is_withdrawal else "destination_name"
        remote_key = "destination_name" if is_withdrawal else "source_name"
        routing = {}
        if firefly_account_id:
            routing[own_id_key] = firefly_account_id
        else:
            routing[own_name_key] = account_name
        if remote_name:
            routing[remote_key] = remote_name
        return routing

    @staticmethod
    def _apply_optional_fields(split: dict, tx: dict) -> None:
        """Add optional fields to split in-place."""
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

    def _create_transaction(self, tx: dict, account: dict, firefly_account_id: str | None = None) -> bool | None:
        """Create a single transaction in Firefly III.

        Returns True on success, None if skipped (zero amount), False on API error.
        """
        amount_eur = tx.get("amount_eur", 0.0)
        if not amount_eur:
            logger.debug("Skipping zero-amount transaction external_id=%s", tx.get("external_id"))
            return None
        is_withdrawal = amount_eur < 0
        split = {
            "type": "withdrawal" if is_withdrawal else "deposit",
            "date": _iso_date(tx.get("date", "")),
            "amount": f"{abs(amount_eur):.2f}",
            "currency_code": tx.get("currency_code", ""),
            "description": tx.get("description") or _build_description(tx) or "(kein Verwendungszweck)",
            "external_id": tx.get("external_id", ""),
        }
        split.update(self._build_account_routing(
            is_withdrawal, account.get("name", ""), firefly_account_id, tx.get("remote_name") or None,
        ))
        self._apply_optional_fields(split, tx)

        response = requests.post(
            f"{self.base_url}/api/v1/transactions",
            headers=self.headers,
            json={"transactions": [split]},
            timeout=self._TIMEOUT,
        )
        if not response.ok:
            data = response.json() if response.content else {}
            message = data.get("message", response.text)
            logger.error(
                "Failed to create transaction external_id=%s status=%s: %s",
                tx.get("external_id"), response.status_code, message,
            )
            return False
        logger.debug("Created transaction external_id=%s", tx.get("external_id"))
        return True
