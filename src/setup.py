"""
txporter - Bank setup state machine
Manages the multi-step REST flow for registering new banks via aqhbci-tool4.
"""

import fcntl
import json
import logging
import os
import pty
import re
import select
import subprocess
import termios
import time
from typing import Optional

logger = logging.getLogger(__name__)

PINFILE = os.environ.get("TXPORTER_PINFILE", "/home/txporter/config/pinfile")
CONFIG_PATH = os.environ.get("TXPORTER_CONFIG", "/home/txporter/config/banks.json")
PROFILES_PATH = os.environ.get("TXPORTER_PROFILES", "/home/txporter/config/bank_profiles.json")


def load_profiles() -> dict:
    with open(PROFILES_PATH) as f:
        return json.load(f)


def _write_pin(pinfile: str, blz: str, login: str, pin: str):
    """Write or update a PIN entry in the pinfile.

    AqBanking pinfile format: PIN_{BLZ}_{LOGIN} = "PIN"
    """
    key = f"PIN_{blz}_{login}"
    entry = f'{key} = "{pin}"\n'
    lines = []
    try:
        with open(pinfile) as f:
            lines = f.readlines()
    except FileNotFoundError:
        pass
    for i, line in enumerate(lines):
        if line.startswith(f"{key} ") or line.startswith(f"{key}="):
            lines[i] = entry
            break
    else:
        lines.append(entry)
    os.makedirs(os.path.dirname(pinfile) or ".", exist_ok=True)
    with open(pinfile, "w") as f:
        f.writelines(lines)


def _resolve_user_index(login: str) -> str:
    """Find the AqBanking unique user index for the given login from listusers output."""
    result = subprocess.run(["aqhbci-tool4", "listusers"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if login in line:
            m = re.search(r"Unique Id:\s*(\d+)", line)
            if m:
                return m.group(1)
    return "1"


def _parse_tan_modes(output: str) -> list:
    """Parse aqhbci-tool4 listitanmodes output into [{id, description}, ...]."""
    modes = []
    for line in output.splitlines():
        m = re.match(r"\s*(\d+)\s*[:\-]\s*(.*)", line)
        if m:
            modes.append({"id": int(m.group(1)), "description": m.group(2).strip()})
    return modes


_ACCOUNT_TYPE_LABELS = {
    "bank":        "Girokonto",
    "savings":     "Sparkonto",
    "investment":  "Depot",
    "creditcard":  "Kreditkarte",
    "moneymarket": "Tagesgeld",
    "cash":        "Bar",
}


def _parse_listaccounts(output: str) -> list:
    """Parse aqhbci-tool4 listaccounts -v output into a list of account dicts.

    AqBanking 6.9.x emits one line per account:
      Account N: Bank: BBBBBBBB Account Number: NNNN Name: X ... Account Type: Y LocalUniqueId: Z

    Each returned dict contains:
      aqbanking_id (int|None), bank_code (str|None), account_number (str|None),
      iban (str|None), account_type (str|None), account_type_label (str|None),
      owner_name (str|None)
    """
    accounts = []
    for line in output.splitlines():
        if not re.match(r"Account\s+\d+", line, re.IGNORECASE):
            continue
        acc: dict = {}
        m = re.search(r"LocalUniqueId:\s*(\d+)", line)
        acc["aqbanking_id"] = int(m.group(1)) if m else None
        m = re.search(r"Bank:\s*(\d+)", line)
        acc["bank_code"] = m.group(1) if m else None
        m = re.search(r"Account Number:\s*(\S+)", line)
        acc["account_number"] = m.group(1) if m else None
        m = re.search(r"IBAN:\s*(\S+)", line)
        acc["iban"] = m.group(1) if m else None
        m = re.search(r"Account Type:\s*(\S+)", line)
        acc["account_type"] = m.group(1) if m else None
        acc["account_type_label"] = _ACCOUNT_TYPE_LABELS.get(
            acc["account_type"].lower(), acc["account_type"]
        ) if acc["account_type"] else None
        m = re.search(r"\bName:\s*([^,\n]+?)(?=\s+\w+:|$)", line)
        acc["owner_name"] = m.group(1).strip() if m else None
        accounts.append(acc)
    return accounts


def _parse_cert_info(output: str) -> dict:
    """Extract certificate details from GWEN's cert acceptance prompt output."""
    field_map = {
        "Name": "name",
        "Organisation": "organisation",
        "Country": "country",
        "City": "city",
        "Valid after": "valid_after",
        "Valid until": "valid_until",
        "Hash (MD5)": "hash_md5",
        "Hash (SHA1)": "hash_sha1",
        "Status": "status",
    }
    cert = {}
    for line in output.splitlines():
        for field, key in field_map.items():
            if line.strip().startswith(field):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    cert[key] = parts[1].strip()
    return cert


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


class SetupSession:
    def __init__(self, setup_id: str, account_id: str, login: str,
                 blz: str, url: str, hbci_version: int,
                 tan_mode: Optional[int], name: str):
        self.setup_id = setup_id
        self.account_id = account_id
        self.login = login
        self.blz = blz
        self.url = url
        self.hbci_version = hbci_version
        self.tan_mode = tan_mode
        self.name = name
        self.user_index: Optional[str] = None
        self.cert_proc = None  # getsysid Popen, waiting for certificate acceptance
        self.proc = None       # getaccounts Popen, waiting for TAN confirmation
        self._pending_accounts: list = []  # set when user must select account
        # Snapshot of aqbanking_ids that already existed before this setup started.
        # Lets _listaccounts_for_user return only newly added accounts.
        try:
            existing = subprocess.run(
                ["aqhbci-tool4", "listaccounts", "-v"], capture_output=True, text=True
            )
            self._pre_existing_ids: set = {
                a["aqbanking_id"] for a in _parse_listaccounts(existing.stdout)
                if "aqbanking_id" in a
            }
        except Exception:
            self._pre_existing_ids = set()

    def step1_register(self) -> dict:
        """Run adduser, then start getsysid in a PTY.

        Waits up to 15 s for the certificate prompt to appear and returns the
        cert details so the caller can verify before accepting.
        If the cert is already trusted getsysid exits without prompting and the
        flow continues directly to listitanmodes (status: pending_tan_mode /
        pending_confirm).
        """
        self._run_adduser()
        self.user_index = _resolve_user_index(self.login)
        logger.info("Resolved user_index=%s for login=%s", self.user_index, self.login)
        self._start_getsysid()
        cert_info = self._wait_for_cert_prompt(timeout=15)

        if self.cert_proc.poll() is not None:
            # Cert was already trusted; getsysid completed without prompting.
            self._check_getsysid_returncode()
            return self._after_getsysid()

        return {
            "setup_id": self.setup_id,
            "status": "pending_cert",
            "message": f"Bank server presented a certificate. Accept with POST /setup/{self.setup_id}/acceptcert",
            "certificate": cert_info,
        }

    def step1b_accept_cert(self, accept: bool) -> dict:
        """Send 1 (yes) or 2 (no) to the waiting getsysid process, then continue."""
        self._complete_getsysid(accept)
        return self._after_getsysid()

    def _after_getsysid(self) -> dict:
        """Run listitanmodes (+ optional setitanmode/getaccounts) after getsysid."""
        tan_modes = self._run_listitanmodes()
        if not tan_modes:
            # Some banks (e.g. Consorsbank) don't include TAN modes in the BPD
            # returned by getsysid — query them explicitly from the bank.
            self._run_getitanmodes()
            tan_modes = self._run_listitanmodes()

        if self.tan_mode is not None:
            self._run_setitanmode(self.tan_mode)
            self._start_getaccounts()  # Sends request to bank → triggers push notification now
            self._drain_acc_prompts_briefly()
            return {
                "setup_id": self.setup_id,
                "status": "pending_confirm",
                "message": f"Confirm TAN in banking app, then POST /setup/{self.setup_id}/confirm",
                "tan_modes": tan_modes,
                "auto_selected_tan_mode": self.tan_mode,
            }
        if not tan_modes:
            # Still no modes — bank uses One-Step TAN; proceed directly.
            self._start_getaccounts()
            self._drain_acc_prompts_briefly()
            return {
                "setup_id": self.setup_id,
                "status": "pending_confirm",
                "message": f"Confirm TAN in banking app (or wait), then POST /setup/{self.setup_id}/confirm",
                "tan_modes": [],
            }
        return {
            "setup_id": self.setup_id,
            "status": "pending_tan_mode",
            "message": f"Select TAN mode, then POST /setup/{self.setup_id}/tanmode",
            "tan_modes": tan_modes,
        }

    def step2_set_tanmode(self, tan_mode: int) -> dict:
        """Set TAN mode and start getaccounts (triggers push notification immediately)."""
        self._run_setitanmode(tan_mode)
        self._start_getaccounts()
        self._drain_acc_prompts_briefly()
        return {
            "setup_id": self.setup_id,
            "status": "pending_confirm",
            "message": f"Confirm TAN in banking app, then POST /setup/{self.setup_id}/confirm",
        }

    def step3_confirm(self, timeout: int = 120) -> dict:
        """Wait for getaccounts to complete; returns pending_tan if TAN entry required.

        getaccounts is normally pre-started in _after_getsysid / step2_set_tanmode
        so that the push notification reaches the banking app before the user
        has to click Done. The start here is a fallback only.
        """
        if self.proc is None:
            self._start_getaccounts()
        challenge = self._wait_for_tan_prompt_or_exit(timeout)
        if challenge is not None:
            return {
                "setup_id": self.setup_id,
                "status": "pending_tan",
                "message": f"Enter TAN from your banking app, then POST /setup/{self.setup_id}/tan",
                "challenge": challenge,
            }
        return self._finalize_setup()

    def step3b_submit_tan(self, tan: str, timeout: int = 60) -> dict:
        """Submit TAN for banks that require explicit TAN entry (e.g. Consorsbank)."""
        try:
            os.write(self._acc_master_fd, f"{tan}\n".encode())
        except OSError:
            pass

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            rlist, _, _ = select.select([self._acc_master_fd], [], [], min(0.5, remaining))
            if rlist:
                try:
                    self._acc_output += os.read(self._acc_master_fd, 4096)
                except OSError:
                    break

        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise RuntimeError("getaccounts timed out after TAN entry")
        finally:
            try:
                os.close(self._acc_master_fd)
            except OSError:
                pass

        if self.proc.returncode != 0:
            raise RuntimeError(
                f"getaccounts failed: {self._acc_output.decode('utf-8', errors='replace')}"
            )
        return self._finalize_setup()

    # ── Private helpers ────────────────────────────────────────────────────────

    def _run_adduser(self):
        # Remove any stale users with the same login at the same bank to avoid
        # duplicates from previously failed setup attempts.
        self._delete_existing_users()
        cmd = [
            "aqhbci-tool4", "adduser",
            "-t", "pintan",
            "--context=1",
            "-b", self.blz,
            "-u", self.login,
            "-s", self.url,
            "-N", self.name,
            f"--hbciversion={self.hbci_version}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"adduser failed: {result.stderr}")

    def _delete_existing_users(self):
        """Delete any existing AqBanking users matching this login+bank to avoid duplicates."""
        result = subprocess.run(["aqhbci-tool4", "listusers"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if self.blz in line and self.login in line:
                m = re.search(r"Unique Id:\s*(\d+)", line)
                if m:
                    uid = m.group(1)
                    logger.info("Deleting stale user uid=%s for login=%s blz=%s", uid, self.login, self.blz)
                    subprocess.run(["aqhbci-tool4", "deluser", "-u", uid],
                                   capture_output=True, text=True)

    def _start_getsysid(self):
        """Start getsysid inside a PTY so GWEN's interactive cert prompt works.

        GWEN's GUI layer requires a terminal to display the cert acceptance
        dialog. Without a PTY it silently fails with 'Could not connect (-43)'.
        --pinfile is passed as a global option since getsysid rejects it as a
        subcommand-level flag.
        """
        master_fd, slave_fd = pty.openpty()
        cmd = ["aqhbci-tool4", f"--pinfile={PINFILE}", "getsysid", "-u", self.user_index]
        self.cert_proc = subprocess.Popen(
            cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True
        )
        os.close(slave_fd)
        self._cert_master_fd = master_fd
        self._cert_output = b""

    def _wait_for_cert_prompt(self, timeout: int = 15) -> dict:
        """Read PTY output until GWEN's cert prompt appears; return parsed cert info.

        Returns an empty dict if the process exits before prompting (cert already trusted).
        Stale lock prompts are handled automatically by selecting 'Remove Lock'.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cert_proc.poll() is not None:
                return {}  # already exited — cert was pre-trusted
            remaining = deadline - time.monotonic()
            rlist, _, _ = select.select([self._cert_master_fd], [], [], min(0.5, remaining))
            if rlist:
                try:
                    self._cert_output += os.read(self._cert_master_fd, 4096)
                except OSError:
                    break
            if b"Please enter your choice:" in self._cert_output:
                if b"Possible Stale Lock" in self._cert_output:
                    logger.warning("Stale AqBanking lock detected during getsysid — removing automatically")
                    try:
                        os.write(self._cert_master_fd, b"2\n")
                    except OSError:
                        pass
                    self._cert_output = b""
                    deadline += 10  # allow extra time for getsysid to continue
                    continue
                return _parse_cert_info(self._cert_output.decode("utf-8", errors="replace"))
        return {}

    def _complete_getsysid(self, accept: bool, timeout: int = 30):
        """Send 1 (accept) or 2 (reject) to the PTY and wait for getsysid to finish."""
        if self.cert_proc.poll() is not None:
            self._check_getsysid_returncode()
            if not accept:
                raise RuntimeError("Certificate rejected by user")
            return

        answer = b"1\n" if accept else b"2\n"
        try:
            os.write(self._cert_master_fd, answer)
        except OSError:
            pass

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cert_proc.poll() is not None:
                break
            rlist, _, _ = select.select([self._cert_master_fd], [], [], 0.5)
            if rlist:
                try:
                    self._cert_output += os.read(self._cert_master_fd, 4096)
                except OSError:
                    break

        try:
            self.cert_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.cert_proc.kill()
            raise RuntimeError("getsysid timed out waiting for bank response")
        finally:
            try:
                os.close(self._cert_master_fd)
            except OSError:
                pass

        if not accept:
            raise RuntimeError("Certificate rejected by user")
        self._check_getsysid_returncode()

    def _check_getsysid_returncode(self):
        if self.cert_proc.returncode != 0:
            output = self._cert_output.decode("utf-8", errors="replace")
            raise RuntimeError(f"getsysid failed: {output}")

    def _run_getitanmodes(self):
        """Query the bank for supported iTAN modes and cache them locally."""
        cmd = ["aqhbci-tool4", f"--pinfile={PINFILE}", "getitanmodes", "-u", self.user_index]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("getitanmodes failed (non-fatal): %s", result.stderr)

    def _run_listitanmodes(self) -> list:
        cmd = ["aqhbci-tool4", "listitanmodes", "-u", self.user_index]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return _parse_tan_modes(result.stdout)

    def _run_setitanmode(self, tan_mode: int):
        self.tan_mode = tan_mode
        cmd = ["aqhbci-tool4", "setitanmode", "-u", self.user_index, "-m", str(tan_mode)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        logger.info("setitanmode(%s, %s) rc=%s stdout=%r stderr=%r",
                    self.user_index, tan_mode, result.returncode, result.stdout, result.stderr)
        if result.returncode != 0:
            raise RuntimeError(f"setitanmode failed: {result.stderr}")

    def _start_getaccounts(self):
        """Start getaccounts in a PTY to allow TAN entry if the bank requires it.

        setsid + TIOCSCTTY makes the PTY slave the controlling terminal so that
        GWEN's TAN entry dialog (which opens /dev/tty) can interact with it.
        """
        master_fd, slave_fd = pty.openpty()
        cmd = ["aqhbci-tool4", f"--pinfile={PINFILE}", "getaccounts", "-u", self.user_index]

        def make_ctty():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # fd 0 == stdin == slave PTY

        self.proc = subprocess.Popen(
            cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True, preexec_fn=make_ctty
        )
        os.close(slave_fd)
        self._acc_master_fd = master_fd
        self._acc_output = b""

    def _wait_for_tan_prompt_or_exit(self, timeout: int) -> Optional[str]:
        """Read getaccounts PTY output until TAN prompt appears or process exits.

        Returns the challenge string if TAN entry is required, None if the process
        exits cleanly (e.g. pushTAN — user confirmed in banking app).
        """
        deadline = time.monotonic() + timeout
        pty_eof = False
        last_log = time.monotonic()
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                # Drain any remaining PTY output before deciding.
                self._drain_acc_pty()
                try:
                    os.close(self._acc_master_fd)
                except OSError:
                    pass
                output_text = self._acc_output.decode("utf-8", errors="replace")
                if b"Input:" in self._acc_output:
                    return self._extract_tan_challenge()
                if self.proc.returncode != 0:
                    raise RuntimeError(f"getaccounts failed: {output_text}")
                return None

            remaining = deadline - time.monotonic()
            rlist, _, _ = select.select([self._acc_master_fd], [], [], min(0.5, remaining))
            if rlist:
                try:
                    self._acc_output += os.read(self._acc_master_fd, 4096)
                except OSError:
                    pty_eof = True
                    break

            if time.monotonic() - last_log >= 15:
                elapsed = timeout - (deadline - time.monotonic())
                logger.info("getaccounts still running after %.0fs, output so far: %r",
                            elapsed, self._acc_output[-500:])
                last_log = time.monotonic()

            if b"Please enter your choice:" in self._acc_output:
                if b"Possible Stale Lock" in self._acc_output:
                    logger.warning("Stale AqBanking lock detected during getaccounts — removing automatically")
                    try:
                        os.write(self._acc_master_fd, b"2\n")
                    except OSError:
                        pass
                    self._acc_output = b""
                    deadline += 10
                    continue
                if b"(1) Approved" in self._acc_output or b"Decoupled Mode" in self._acc_output:
                    logger.info("Decoupled Mode approval prompt detected — sending approval (1)")
                    try:
                        os.write(self._acc_master_fd, b"1\n")
                    except OSError:
                        pass
                    self._acc_output = b""
                    continue

            if b"Input:" in self._acc_output:
                return self._extract_tan_challenge()

        if pty_eof:
            # PTY slave closed — process has exited; collect exit code.
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            try:
                os.close(self._acc_master_fd)
            except OSError:
                pass
            output_text = self._acc_output.decode("utf-8", errors="replace")
            if b"Input:" in self._acc_output:
                return self._extract_tan_challenge()
            if self.proc.returncode != 0:
                raise RuntimeError(f"getaccounts failed: {output_text}")
            return None

        self.proc.kill()
        try:
            os.close(self._acc_master_fd)
        except OSError:
            pass
        output_text = self._acc_output.decode("utf-8", errors="replace")
        logger.error("getaccounts timed out after %ds. Full output: %r", timeout, output_text)
        raise RuntimeError("getaccounts timed out")

    def _drain_acc_prompts_briefly(self, timeout: float = 8.0):
        """Read getaccounts PTY output for a short window, resolving stale lock prompts.

        Called immediately after _start_getaccounts() so that any stale lock dialog
        is answered before the HTTP response is returned to the caller.  This ensures
        getaccounts has connected to the bank (and triggered the push notification)
        before the user sees the "Confirm in banking app" screen.
        """
        t0 = time.monotonic()
        deadline = t0 + timeout
        logger.info("getaccounts PTY drain started")
        stop_reason = "timeout"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                stop_reason = "process_exited"
                break

            remaining = deadline - time.monotonic()
            rlist, _, _ = select.select([self._acc_master_fd], [], [], min(0.5, remaining))
            if not rlist:
                continue
            try:
                chunk = os.read(self._acc_master_fd, 4096)
                self._acc_output += chunk
            except OSError:
                stop_reason = "pty_eof"
                break

            if b"Please enter your choice:" in self._acc_output:
                if b"Possible Stale Lock" in self._acc_output:
                    logger.warning("Stale AqBanking lock detected (drain) — removing automatically")
                    try:
                        os.write(self._acc_master_fd, b"2\n")
                    except OSError:
                        pass
                    self._acc_output = b""
                    deadline = time.monotonic() + 10
                    continue
                stop_reason = "interactive_prompt"
                break

            if b"Input:" in self._acc_output:
                stop_reason = "tan_input_prompt"
                break
        logger.info("getaccounts PTY drain finished after %.1fs, reason=%s",
                    time.monotonic() - t0, stop_reason)

    def _drain_acc_pty(self):
        """Read all immediately available data from the getaccounts PTY master fd."""
        while True:
            rlist, _, _ = select.select([self._acc_master_fd], [], [], 0.1)
            if not rlist:
                break
            try:
                self._acc_output += os.read(self._acc_master_fd, 4096)
            except OSError:
                break

    def _extract_tan_challenge(self) -> str:
        text = self._acc_output.decode("utf-8", errors="replace")
        m = re.search(
            r"The server provided the following challenge:\s*(.*?)\s*Input:", text, re.DOTALL
        )
        if m:
            return m.group(1).strip()
        return "Please enter your TAN."

    def _finalize_setup(self) -> dict:
        self._run_getaccsepa()
        all_bank_accounts = self._listaccounts_for_user()
        accounts = self._filter_unconfigured(all_bank_accounts)

        if not accounts:
            # All accounts for this bank are already configured.
            self._remove_placeholder()
            return {
                "status": "all_configured",
                "message": "All accounts for this bank are already configured in banks.json.",
                "configured_count": len(all_bank_accounts),
            }

        aqbanking_id = self._find_aqbanking_id(accounts)
        if aqbanking_id is None:
            # Heuristic could not identify the right account — let the user pick.
            self._pending_accounts = accounts
            return {
                "setup_id": self.setup_id,
                "status": "pending_account_select",
                "accounts": accounts,
            }

        selected = self._find_account(accounts)
        iban = selected.get("iban") if selected else None
        account_number = selected.get("account_number") if selected else None
        bank_code = selected.get("bank_code") if selected else None
        account_type_label = selected.get("account_type_label") if selected else None
        self._write_back(aqbanking_id, iban, account_number, bank_code, account_type_label)
        return {
            "status": "ok",
            "account_id": self.account_id,
            "aqbanking_id": aqbanking_id,
            "iban": iban,
            "account_number": account_number,
            "bank_code": bank_code,
            "accounts": accounts,
        }

    def _check_duplicate(self, account_number: Optional[str], bank_code: Optional[str]) -> Optional[str]:
        """Return the existing account id if this account_number+bank_code is already configured."""
        if not account_number or not bank_code:
            return None
        cfg = load_config()
        for a in cfg["accounts"]:
            if (a.get("account_number") == account_number
                    and a.get("bank_code_aq") == bank_code
                    and a.get("id") != self.account_id):
                return a["id"]
        return None

    def _remove_placeholder(self):
        """Remove the incomplete placeholder entry added at POST /setup time."""
        cfg = load_config()
        cfg["accounts"] = [a for a in cfg["accounts"] if a["id"] != self.account_id]
        save_config(cfg)

    def select_account(self, aqbanking_id: int) -> dict:
        """Finalise setup with an explicitly chosen aqbanking_id."""
        accounts = self._pending_accounts or self._filter_unconfigured(self._listaccounts_for_user())
        selected = next((a for a in accounts if a.get("aqbanking_id") == aqbanking_id), None)
        if selected is None:
            raise ValueError(f"No account with aqbanking_id {aqbanking_id} found")
        iban = selected.get("iban")
        account_number = selected.get("account_number")
        bank_code = selected.get("bank_code")
        account_type_label = selected.get("account_type_label")
        self._write_back(aqbanking_id, iban, account_number, bank_code, account_type_label)
        return {
            "status": "ok",
            "account_id": self.account_id,
            "aqbanking_id": aqbanking_id,
            "iban": iban,
            "account_number": account_number,
            "bank_code": bank_code,
            "account_type_label": account_type_label,
            "accounts": accounts,
        }

    def _run_getaccsepa(self):
        cmd = ["aqhbci-tool4", f"--pinfile={PINFILE}", "getaccsepa", "-u", self.user_index]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("getaccsepa failed (non-fatal): %s", result.stderr)

    def _listaccounts(self) -> list:
        result = subprocess.run(["aqhbci-tool4", "listaccounts", "-v"], capture_output=True, text=True)
        return _parse_listaccounts(result.stdout)

    def _listaccounts_for_user(self) -> list:
        """List accounts relevant to this setup session.

        Priority:
        1. Snapshot diff — accounts whose aqbanking_id did not exist before setup.
           Catches all accounts registered during getaccounts (even if AqBanking
           assigned them to different internal user entries).
        2. BLZ filter — all AqBanking accounts whose bank_code matches self.blz.
           Used on retries where the snapshot diff is empty because AqBanking reused
           existing IDs.  Returns ALL accounts for this bank (including already-
           configured ones); _filter_unconfigured handles exclusion later.
        3. -u user_index — last resort for banks whose sub-accounts use a different BLZ.
        """
        all_accounts = self._listaccounts()

        new_accounts = [
            a for a in all_accounts
            if a.get("aqbanking_id") not in self._pre_existing_ids
        ]
        if new_accounts:
            logger.info("_listaccounts_for_user: %d new account(s) via snapshot diff",
                        len(new_accounts))
            return new_accounts

        by_blz = [a for a in all_accounts if a.get("bank_code") == self.blz]
        if by_blz:
            logger.info("_listaccounts_for_user: snapshot empty, returning %d account(s) by BLZ %s",
                        len(by_blz), self.blz)
            return by_blz

        logger.info("_listaccounts_for_user: falling back to -u %s", self.user_index)
        cmd = ["aqhbci-tool4", "listaccounts", "-v", "-u", self.user_index]
        result = subprocess.run(cmd, capture_output=True, text=True)
        accounts = _parse_listaccounts(result.stdout)
        return accounts if accounts else all_accounts

    def _filter_unconfigured(self, accounts: list) -> list:
        """Remove accounts already present in banks.json (by account_number + bank_code).

        The current setup placeholder (self.account_id) is excluded from the 'existing'
        check so that it is not accidentally treated as a pre-existing duplicate.
        """
        cfg = load_config()
        existing_keys = {
            (a.get("account_number"), a.get("bank_code_aq"))
            for a in cfg["accounts"]
            if a.get("account_number") and a.get("bank_code_aq")
            and a.get("id") != self.account_id
        }
        return [
            a for a in accounts
            if (a.get("account_number"), a.get("bank_code")) not in existing_keys
        ]

    def _find_account(self, accounts: list) -> Optional[dict]:
        """Find the most relevant account from listaccounts output.

        Heuristic used when the user has not explicitly selected an account
        (i.e. no UI). Prefers login-prefix match over BLZ match because some
        brokers (e.g. Consorsbank) register their Verrechnungskonto under a
        different BLZ than the one used for setup (Consorsbank: setup BLZ
        76030080, Verrechnungskonto BLZ 70120400 / HypoVereinsbank).

        """
        for acc in accounts:
            number = acc.get("account_number", "")
            if number and self.login.startswith(number):
                return acc
        for acc in accounts:
            if acc.get("bank_code") == self.blz:
                return acc
        return None

    def _find_aqbanking_id(self, accounts: list) -> Optional[int]:
        acc = self._find_account(accounts)
        return acc.get("aqbanking_id") if acc else None

    def _find_iban(self, accounts: list) -> Optional[str]:
        acc = self._find_account(accounts)
        return acc.get("iban") if acc else None

    def _write_back(self, aqbanking_id: Optional[int], iban: Optional[str],
                    account_number: Optional[str] = None, bank_code: Optional[str] = None,
                    account_type_label: Optional[str] = None):
        """Write aqbanking_id and resolved account fields back into banks.json."""
        if aqbanking_id is None:
            logger.warning("Could not determine aqbanking_id for account %s", self.account_id)
        config = load_config()
        for acc in config["accounts"]:
            if acc["id"] == self.account_id:
                if aqbanking_id is not None:
                    acc["aqbanking_id"] = aqbanking_id
                if iban:
                    acc["iban"] = iban
                if account_number:
                    acc["account_number"] = account_number
                if bank_code:
                    acc["bank_code_aq"] = bank_code
                if account_type_label:
                    acc["account_type_label"] = account_type_label
                break
        save_config(config)
