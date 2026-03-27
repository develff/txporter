"""
txporter - AqBanking CLI wrapper
Fetches transactions from financial accounts using AqBanking.
"""

import re
import subprocess
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote

logger = logging.getLogger(__name__)


def _decode_amount_minor_units(raw: str) -> tuple[int, str]:
    """Decode a CTX amount value into (minor_units: int, currency: str).

    Handles three formats after URL-decoding:
      "-3776:EUR"    → (-3776, "EUR")
      "-25/10:EUR"   → (-25, "EUR")   fraction: numerator is the minor-unit value
      "1:EUR"        → (1, "EUR")
    """
    decoded = unquote(raw)
    colon_idx = decoded.rfind(":")
    currency = decoded[colon_idx + 1:]
    amount_part = decoded[:colon_idx]
    if "/" in amount_part:
        numerator = int(amount_part.split("/")[0])
        return numerator, currency
    return int(amount_part), currency


def _external_id(local_account: str, date: str, minor_units: int, currency: str,
                 bank_reference: str, primanota: str) -> str:
    parts = ["aqbanking", "fints", local_account, date, f"{minor_units}:{currency}"]
    if bank_reference:
        parts.append(bank_reference)
    if primanota and primanota != "0":
        parts.append(primanota)
    if not bank_reference and (not primanota or primanota == "0"):
        logger.warning(
            "Transaction on %s for %s:%s has neither bankReference nor primanota — "
            "external_id may not be unique",
            date, minor_units, currency,
        )
    return ":".join(parts)


def _parse_ctx(output: str) -> list[dict]:
    """Parse AqBanking CTX output into a list of neutral transaction dicts.

    Each dict contains URL-decoded CTX fields plus a computed external_id.
    Amount is represented as (amount_minor_units: int, currency_code: str).
    """
    transactions = []

    for block_match in re.finditer(r"transaction \{(.*?)\} #transaction", output, re.DOTALL):
        block = block_match.group(1)

        def field(name: str) -> str:
            m = re.search(rf'(?:char|int)\s+{re.escape(name)}="([^"]*)"', block)
            return unquote(m.group(1)) if m else ""

        raw_value = re.search(r'char\s+value="([^"]*)"', block)
        if not raw_value:
            continue

        minor_units, currency = _decode_amount_minor_units(raw_value.group(1))
        local_account = field("localAccountNumber")
        date = field("date")
        bank_reference = field("bankReference")
        primanota = field("primanota")

        tx = {
            "external_id": _external_id(local_account, date, minor_units, currency,
                                         bank_reference, primanota),
            "type": field("type"),
            "sub_type": field("subType"),
            "command": field("command"),
            "status": field("status"),
            "unique_account_id": field("uniqueAccountId"),
            "unique_id": field("uniqueId"),
            "ref_unique_id": field("refUniqueId"),
            "id_for_application": field("idForApplication"),
            "session_id": field("sessionId"),
            "group_id": field("groupId"),
            "acknowledge": field("acknowledge"),
            "local_bank_code": field("localBankCode"),
            "local_account_number": local_account,
            "remote_bank_code": field("remoteBankCode"),
            "remote_account_number": field("remoteAccountNumber"),
            "remote_iban": field("remoteIban"),
            "remote_bic": field("remoteBic"),
            "remote_name": field("remoteName"),
            "date": date,
            "valuta_date": field("valutaDate"),
            "amount_minor_units": minor_units,
            "currency_code": currency,
            "transaction_code": field("transactionCode"),
            "transaction_text": field("transactionText"),
            "transaction_key": field("transactionKey"),
            "text_key": field("textKey"),
            "primanota": primanota,
            "purpose": field("purpose"),
            "bank_reference": bank_reference,
            "end_to_end_reference": field("endToEndReference"),
            "sequence": field("sequence"),
            "charge": field("charge"),
            "period": field("period"),
            "cycle": field("cycle"),
            "execution_day": field("executionDay"),
            "estatement_number": field("estatementNumber"),
            "estatement_max_entries": field("estatementMaxEntries"),
            "vop_result": field("vopResult"),
        }
        transactions.append(tx)

    return transactions


class AqBankingClient:
    def __init__(self, account: dict):
        self.account = account

    def fetch_transactions(self, days: int = 30) -> list:
        """Fetch transactions for the last N days."""
        account_type = self.account.get("type", "fints")
        if account_type == "fints":
            return self._fetch_fints(days)
        elif account_type == "paypal":
            return self._fetch_paypal(days)
        else:
            raise ValueError(f"Unsupported account type: {account_type}")

    def _fetch_fints(self, days: int) -> list:
        """Fetch transactions via FinTS using aqbanking-cli."""
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        account_id = self.account["id"]

        cmd = [
            "aqbanking-cli", "request",
            "--account", account_id,
            "--fromdate", from_date,
            "--transactions"
        ]

        logger.info(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"aqbanking-cli failed: {result.stderr}")

        return _parse_ctx(result.stdout)

    def _fetch_paypal(self, days: int) -> list:
        """Fetch transactions via AqBanking PayPal backend."""
        # TODO: implement PayPal fetch
        raise NotImplementedError("PayPal backend not yet implemented")
