"""
txporter - Bank setup state machine
Manages the multi-step REST flow for registering new banks via aqhbci-tool4.
"""

import json
import logging
import os
import re
import subprocess
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


def _parse_listaccounts(output: str) -> list:
    """Parse aqhbci-tool4 listaccounts -v output into a list of account dicts.

    Each dict contains at minimum:
      aqbanking_id (int|None), iban (str), bank_code (str), account_number (str)
    """
    accounts = []
    current: Optional[dict] = None
    for line in output.splitlines():
        if re.match(r"Account\s+\d+", line, re.IGNORECASE):
            if current is not None:
                accounts.append(current)
            m = re.search(r"Unique Account Id[=:\s]+(\d+)", line, re.IGNORECASE)
            current = {"aqbanking_id": int(m.group(1)) if m else None}
        elif current is not None:
            m = re.match(r"\s+(.+?)\s*:\s*(.*)", line)
            if m:
                key = m.group(1).strip().lower().replace(" ", "_")
                val = m.group(2).strip()
                current[key] = val
    if current is not None:
        accounts.append(current)
    return accounts


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
        self.proc = None

    def step1_register(self) -> dict:
        """Run adduser → getsysid → listitanmodes.
        If tan_mode is known, also runs setitanmode and starts getaccounts (TAN triggered).
        """
        self._run_adduser()
        self.user_index = _resolve_user_index(self.login)
        self._run_getsysid()
        tan_modes = self._run_listitanmodes()

        if self.tan_mode is not None:
            self._run_setitanmode(self.tan_mode)
            self._start_getaccounts()
            return {
                "setup_id": self.setup_id,
                "status": "pending_confirm",
                "message": f"Confirm TAN in banking app, then POST /setup/{self.setup_id}/confirm",
                "tan_modes": tan_modes,
                "auto_selected_tan_mode": self.tan_mode,
            }
        return {
            "setup_id": self.setup_id,
            "status": "pending_tan_mode",
            "message": f"Select TAN mode, then POST /setup/{self.setup_id}/tanmode",
            "tan_modes": tan_modes,
        }

    def step2_set_tanmode(self, tan_mode: int) -> dict:
        """Set TAN mode and start getaccounts (triggers TAN in banking app)."""
        self._run_setitanmode(tan_mode)
        self._start_getaccounts()
        return {
            "setup_id": self.setup_id,
            "status": "pending_confirm",
            "message": f"Confirm TAN in banking app, then POST /setup/{self.setup_id}/confirm",
        }

    def step3_confirm(self, timeout: int = 120) -> dict:
        """Wait for getaccounts, run getaccsepa, discover aqbanking_id, update banks.json."""
        self._complete_getaccounts(timeout)
        self._run_getaccsepa()
        accounts = self._listaccounts()
        aqbanking_id = self._find_aqbanking_id(accounts)
        iban = self._find_iban(accounts)
        self._write_back(aqbanking_id, iban)
        return {
            "status": "ok",
            "account_id": self.account_id,
            "aqbanking_id": aqbanking_id,
            "iban": iban,
            "accounts": accounts,
        }

    # ── Private helpers ────────────────────────────────────────────────────────

    def _run_adduser(self):
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

    def _run_getsysid(self):
        cmd = ["aqhbci-tool4", "getsysid", "-u", self.user_index]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"getsysid failed: {result.stderr}")

    def _run_listitanmodes(self) -> list:
        cmd = ["aqhbci-tool4", "listitanmodes", "-u", self.user_index]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return _parse_tan_modes(result.stdout)

    def _run_setitanmode(self, tan_mode: int):
        self.tan_mode = tan_mode
        cmd = ["aqhbci-tool4", "setitanmode", "-u", self.user_index, "-m", str(tan_mode)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"setitanmode failed: {result.stderr}")

    def _start_getaccounts(self):
        cmd = ["aqhbci-tool4", "getaccounts", f"--pinfile={PINFILE}", "-u", self.user_index]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _complete_getaccounts(self, timeout: int):
        stdout, stderr = self.proc.communicate(timeout=timeout)
        if self.proc.returncode != 0:
            raise RuntimeError(f"getaccounts failed: {stderr}")

    def _run_getaccsepa(self):
        cmd = ["aqhbci-tool4", "getaccsepa", f"--pinfile={PINFILE}", "-u", self.user_index]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("getaccsepa failed (non-fatal): %s", result.stderr)

    def _listaccounts(self) -> list:
        result = subprocess.run(["aqhbci-tool4", "listaccounts", "-v"], capture_output=True, text=True)
        return _parse_listaccounts(result.stdout)

    def _find_aqbanking_id(self, accounts: list) -> Optional[int]:
        for acc in accounts:
            if acc.get("bank_code") == self.blz:
                return acc.get("aqbanking_id")
        return None

    def _find_iban(self, accounts: list) -> Optional[str]:
        for acc in accounts:
            if acc.get("bank_code") == self.blz:
                return acc.get("iban")
        return None

    def _write_back(self, aqbanking_id: Optional[int], iban: Optional[str]):
        """Write aqbanking_id (and iban if found) back into banks.json."""
        if aqbanking_id is None:
            logger.warning("Could not determine aqbanking_id for account %s", self.account_id)
        config = load_config()
        for acc in config["accounts"]:
            if acc["id"] == self.account_id:
                if aqbanking_id is not None:
                    acc["aqbanking_id"] = aqbanking_id
                if iban:
                    acc["iban"] = iban
                break
        save_config(config)
