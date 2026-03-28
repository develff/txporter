"""
txporter - AqBanking CLI wrapper
Fetches transactions from financial accounts using AqBanking.
"""

import os
import re
import subprocess
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote

logger = logging.getLogger(__name__)

PINFILE = os.environ.get("TXPORTER_PINFILE", "/home/txporter/config/pinfile")


def _decode_amount_eur(raw: str) -> tuple[float, str]:
    """Decode a CTX amount value into (amount_eur: float, currency: str).

    Handles three formats after URL-decoding:
      "32:EUR"       → (32.0, "EUR")    simple: integer EUR value
      "-3776/100:EUR"→ (-37.76, "EUR")  fraction: numerator/denominator in EUR
      "-25/10:EUR"   → (-2.5, "EUR")    fraction: -25/10 = -€2.50
    """
    decoded = unquote(raw)
    colon_idx = decoded.rfind(":")
    if colon_idx == -1:
        logger.warning("CTX amount has no currency suffix, skipping: %s", raw)
        return 0.0, ""
    currency = decoded[colon_idx + 1:]
    amount_part = decoded[:colon_idx]
    if "/" in amount_part:
        numerator, denominator = amount_part.split("/", 1)
        return int(numerator) / int(denominator), currency
    return float(amount_part), currency


def _external_id(local_account: str, date: str, amount_eur: float, currency: str,
                 bank_reference: str, primanota: str) -> str:
    parts = ["aqbanking", "fints", local_account, date, f"{amount_eur:.2f}:{currency}"]
    if bank_reference:
        parts.append(bank_reference)
    if primanota and primanota != "0":
        parts.append(primanota)
    if not bank_reference and (not primanota or primanota == "0"):
        logger.warning(
            "Transaction on %s for %.2f:%s has neither bankReference nor primanota — "
            "external_id may not be unique",
            date, amount_eur, currency,
        )
    return ":".join(parts)


def _parse_ctx(output: str) -> list[dict]:
    """Parse AqBanking CTX output into a list of neutral transaction dicts.

    Each dict contains URL-decoded CTX fields plus a computed external_id.
    Amount is represented as (amount_eur: float, currency_code: str).
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

        amount_eur, currency = _decode_amount_eur(raw_value.group(1))
        local_account = field("localAccountNumber")
        date = field("date")
        bank_reference = field("bankReference")
        primanota = field("primanota")

        tx = {
            "external_id": _external_id(local_account, date, amount_eur, currency,
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
            "amount_eur": amount_eur,
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
        """Fetch transactions for the last N days (non-FinTS accounts only)."""
        account_type = self.account.get("type", "fints")
        if account_type == "paypal":
            return self._fetch_paypal(days)
        raise ValueError(f"Use start_fetch/complete_fetch for account type: {account_type}")

    def start_fetch(self, from_date: str = None, to_date: str = None, days: int = 30):
        """Start a FinTS transaction request; returns a Popen handle waiting for TAN confirmation.

        from_date / to_date: YYYY-MM-DD or YYYYMMDD strings (optional).
        If omitted, defaults to the last `days` days.
        """
        if from_date is None:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        else:
            from_date = from_date.replace("-", "")

        account_id = str(self.account["aqbanking_id"])

        cmd = [
            "aqbanking-cli",
            f"--pinfile={PINFILE}",
            "request",
            f"--aid={account_id}",
            f"--fromdate={from_date}",
            "--transactions",
        ]
        if to_date:
            cmd.insert(-1, f"--todate={to_date.replace('-', '')}")

        logger.info(f"Running: {' '.join(cmd)}")
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def complete_fetch(self, proc, timeout: int = 60) -> list:
        """Confirm TAN approval and wait for aqbanking-cli to return results."""
        stdout, stderr = proc.communicate(input="1\n", timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"aqbanking-cli failed: {stderr}")
        return _parse_ctx(stdout)

    def _fetch_paypal(self, days: int) -> list:
        """Fetch transactions via AqBanking PayPal backend."""
        # TODO: implement PayPal fetch
        raise NotImplementedError("PayPal backend not yet implemented")
