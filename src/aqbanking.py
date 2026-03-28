"""
txporter - AqBanking CLI wrapper
Fetches transactions from financial accounts using AqBanking.
"""

import os
import re
import select
import subprocess
import time
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


_PUSH_SIGNALS = (
    b"Please enter your choice:",
    b"(1) Approved",
    b"(1) Yes",
    b"Decoupled",
)

# How long to wait for aqbanking-cli to connect to the bank and signal
# whether a TAN is required.  Most banks respond within 5-15 seconds.
_DRAIN_TIMEOUT = 30


class AqBankingClient:
    def __init__(self, account: dict):
        self.account = account
        self._proc = None
        self._stdout_buf = b""
        self._stderr_buf = b""

    def fetch_transactions(self, days: int = 30) -> list:
        """Fetch transactions for the last N days (non-FinTS accounts only)."""
        account_type = self.account.get("type", "fints")
        if account_type == "paypal":
            return self._fetch_paypal(days)
        raise ValueError(f"Use start_fetch/complete_fetch for account type: {account_type}")

    def start_fetch(self, from_date: str = None, to_date: str = None, days: int = 30) -> dict:
        """Start a FinTS transaction request and wait until we know if a TAN is needed.

        Returns {"status": "ok", "transactions": [...]} if the bank completed
        the request without requiring a TAN (e.g. read-only / SCA-exempt).
        Returns {"status": "pending"} if a push notification was sent and the
        user must confirm in the banking app before calling complete_fetch().

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

        logger.info("Running: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._stdout_buf = b""
        self._stderr_buf = b""

        return self._drain_until_push_or_exit(_DRAIN_TIMEOUT)

    def _drain_until_push_or_exit(self, timeout: float) -> dict:
        """Read stdout/stderr until the process exits or a push-TAN prompt appears.

        - Process exits cleanly  → no TAN required; parse and return transactions inline.
        - Push prompt detected   → push was sent; caller must call complete_fetch() later.
        - Timeout                → assume push was sent (safe fallback).
        """
        deadline = time.monotonic() + timeout
        fds = [self._proc.stdout, self._proc.stderr]

        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                self._drain_pipes()
                if self._proc.returncode != 0:
                    raise RuntimeError(
                        f"aqbanking-cli failed: {self._stderr_buf.decode('utf-8', errors='replace')}"
                    )
                logger.info("aqbanking-cli exited cleanly — no TAN required")
                return {
                    "status": "ok",
                    "transactions": _parse_ctx(self._stdout_buf.decode("utf-8", errors="replace")),
                }

            remaining = deadline - time.monotonic()
            rlist, _, _ = select.select(fds, [], [], min(0.5, remaining))
            for f in rlist:
                try:
                    chunk = os.read(f.fileno(), 4096)
                except OSError:
                    chunk = b""
                if f is self._proc.stdout:
                    self._stdout_buf += chunk
                else:
                    self._stderr_buf += chunk

            combined = self._stdout_buf + self._stderr_buf
            if any(sig in combined for sig in _PUSH_SIGNALS):
                logger.info("Push-TAN prompt detected — returning pending")
                return {"status": "pending"}

        logger.warning("aqbanking-cli drain timed out after %.0fs — assuming push was sent", timeout)
        return {"status": "pending"}

    def _drain_pipes(self):
        """Non-blocking drain of any remaining data in stdout/stderr pipes."""
        for f in (self._proc.stdout, self._proc.stderr):
            while True:
                rlist, _, _ = select.select([f], [], [], 0.1)
                if not rlist:
                    break
                try:
                    chunk = os.read(f.fileno(), 4096)
                except OSError:
                    break
                if not chunk:
                    break
                if f is self._proc.stdout:
                    self._stdout_buf += chunk
                else:
                    self._stderr_buf += chunk

    def complete_fetch(self, timeout: int = 60) -> list:
        """Confirm push-TAN approval and collect transaction results.

        Sends the approval signal to aqbanking-cli and waits for it to finish.
        Must only be called after start_fetch() returned {"status": "pending"}.
        """
        remaining_out, remaining_err = self._proc.communicate(input=b"1\n", timeout=timeout)
        self._stdout_buf += remaining_out
        if self._proc.returncode != 0:
            raise RuntimeError(
                f"aqbanking-cli failed: {remaining_err.decode('utf-8', errors='replace')}"
            )
        return _parse_ctx(self._stdout_buf.decode("utf-8", errors="replace"))

    def _fetch_paypal(self, days: int) -> list:
        """Fetch transactions via AqBanking PayPal backend."""
        # TODO: implement PayPal fetch
        raise NotImplementedError("PayPal backend not yet implemented")
