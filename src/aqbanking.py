"""
txporter - AqBanking CLI wrapper
Fetches transactions from financial accounts using AqBanking.
"""

import glob as _glob
import os
import re
import select
import subprocess
import threading
import time
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote

logger = logging.getLogger(__name__)

PINFILE = os.environ.get("TXPORTER_PINFILE", "/home/txporter/config/pinfile")
_AQBANKING_DIR = os.path.expanduser("~/.aqbanking")

# aqbanking-cli uses a file lock on its config directory, so only one process
# can run at a time.  We track the running process rather than holding a Lock
# across two method calls, so a crashed/abandoned process never blocks future
# syncs — if the process is dead, poll() is not None and we allow a new one.
_running_proc: "subprocess.Popen | None" = None
_running_proc_guard = threading.Lock()  # guards _running_proc only


def aqbanking_is_busy() -> bool:
    """Return True if an aqbanking-cli process is currently running."""
    with _running_proc_guard:
        return _running_proc is not None and _running_proc.poll() is None


def _clear_stale_locks() -> None:
    """Remove gwenhywfar .lck files left by previously crashed aqbanking-cli processes.

    Only call this when no aqbanking process is running (already guaranteed by
    the _running_proc check in start_fetch).  In Docker, PIDs are frequently
    reused, so gwenhywfar can mistake a live unrelated process for the original
    lock holder and refuse to start for up to ~2 minutes.
    """
    for lck in _glob.glob(os.path.join(_AQBANKING_DIR, "**", "*.lck"), recursive=True):
        try:
            os.remove(lck)
            logger.info("Removed stale aqbanking lock file: %s", lck)
        except OSError as e:
            logger.warning("Could not remove stale lock file %s: %s", lck, e)


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
    parts = ["txporter", local_account, date, f"{amount_eur:.2f}:{currency}"]
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
# whether a TAN is required.  Most banks respond within 5-15 seconds;
# 90 s gives headroom for slow connections or large date ranges.
_DRAIN_TIMEOUT = 90


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
        if not from_date:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        else:
            from_date = from_date.replace("-", "")

        account_id = str(self.account["aqbanking_id"])

        cmd = [
            "stdbuf", "-o0",   # force unbuffered stdout so push prompts are visible immediately
            "aqbanking-cli",
            f"--pinfile={PINFILE}",
            "request",
            f"--aid={account_id}",
            f"--fromdate={from_date}",
            "--transactions",
        ]
        if to_date:
            cmd.insert(-1, f"--todate={to_date.replace('-', '')}")

        global _running_proc
        with _running_proc_guard:
            if _running_proc is not None and _running_proc.poll() is None:
                raise RuntimeError(
                    "Another aqbanking-cli process is already running — please wait and retry"
                )
            _clear_stale_locks()
            logger.info("Running: %s", " ".join(cmd))
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self._stdout_buf = b""
            self._stderr_buf = b""
            _running_proc = self._proc

        try:
            result = self._drain_until_push_or_exit(_DRAIN_TIMEOUT)
            if result["status"] == "ok":
                with _running_proc_guard:
                    _running_proc = None
            # "pending": _running_proc stays set until complete_fetch() clears it.
            return result
        except Exception:
            with _running_proc_guard:
                _running_proc = None
            raise

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

        logger.warning("aqbanking-cli drain timed out after %.0fs — unknown state", timeout)
        stderr_so_far = self._stderr_buf.decode("utf-8", errors="replace").strip()
        if stderr_so_far:
            logger.info("aqbanking stderr at timeout:\n%s", stderr_so_far)
        else:
            logger.info("aqbanking stderr at timeout: (empty)")
        push_seen = any(sig in (self._stdout_buf + self._stderr_buf) for sig in _PUSH_SIGNALS)
        if push_seen:
            logger.info("Push signal found in buffered output — treating as pending")
        else:
            logger.warning("No push signal in buffered output — TAN state unclear; returning pending anyway")
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

    def complete_fetch(self, timeout: int = 120) -> list:
        """Confirm push-TAN approval and collect transaction results.

        Must only be called after start_fetch() returned {"status": "pending"}.

        Only sends the "1\\n" approval signal to aqbanking-cli if a push-TAN
        prompt was actually detected in the buffered output.  If the drain
        timed out without seeing any push signal (aqbanking was still
        connecting or no TAN is needed), we wait for the process to finish
        naturally without sending any input — sending "1\\n" into a process
        that isn't waiting for it produces wrong results.
        """
        combined = self._stdout_buf + self._stderr_buf
        push_seen = any(sig in combined for sig in _PUSH_SIGNALS)
        if push_seen:
            logger.info("complete_fetch: push signal in buffer — sending approval")
            stdin_input = b"1\n"
        else:
            logger.info("complete_fetch: no push signal in buffer — waiting for process without input")
            stdin_input = None
        global _running_proc
        try:
            remaining_out, remaining_err = self._proc.communicate(input=stdin_input, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.communicate()
            raise RuntimeError(f"aqbanking-cli timed out after {timeout}s waiting for completion")
        finally:
            with _running_proc_guard:
                _running_proc = None
        self._stdout_buf += remaining_out
        stderr_text = (self._stderr_buf + remaining_err).decode("utf-8", errors="replace").strip()
        if stderr_text:
            logger.info("aqbanking stderr:\n%s", stderr_text)
        if self._proc.returncode != 0:
            raise RuntimeError(f"aqbanking-cli failed (rc={self._proc.returncode}): {stderr_text}")
        return _parse_ctx(self._stdout_buf.decode("utf-8", errors="replace"))

    def _fetch_paypal(self, days: int) -> list:
        """Fetch transactions via AqBanking PayPal backend."""
        raise NotImplementedError("PayPal backend not yet implemented")
