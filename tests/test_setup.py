"""Tests for bank setup REST API and helper functions."""

import json
import os
import pytest
import subprocess
import tempfile
from unittest.mock import patch, MagicMock, call

from src.setup import (
    _write_pin,
    _resolve_user_index,
    _parse_tan_modes,
    _parse_listaccounts,
    SetupSession,
)


# ── _write_pin ─────────────────────────────────────────────────────────────────

class TestWritePin:
    def test_creates_file_with_entry(self, tmp_path):
        pinfile = str(tmp_path / "pinfile")
        _write_pin(pinfile, "12030000", "12345678", "s3cr3t")
        assert open(pinfile).read() == 'PIN_12030000_12345678 = "s3cr3t"\n'

    def test_appends_new_login(self, tmp_path):
        pinfile = str(tmp_path / "pinfile")
        _write_pin(pinfile, "12030000", "user1", "pin1")
        _write_pin(pinfile, "50050222", "user2", "pin2")
        content = open(pinfile).read()
        assert 'PIN_12030000_user1 = "pin1"' in content
        assert 'PIN_50050222_user2 = "pin2"' in content

    def test_overwrites_existing_login(self, tmp_path):
        pinfile = str(tmp_path / "pinfile")
        _write_pin(pinfile, "12030000", "user1", "old")
        _write_pin(pinfile, "12030000", "user1", "new")
        content = open(pinfile).read()
        assert 'PIN_12030000_user1 = "new"' in content
        assert "old" not in content

    def test_creates_missing_parent_dir(self, tmp_path):
        pinfile = str(tmp_path / "subdir" / "pinfile")
        _write_pin(pinfile, "12030000", "user1", "pin1")
        assert os.path.exists(pinfile)


# ── _resolve_user_index ────────────────────────────────────────────────────────

class TestResolveUserIndex:
    def test_finds_unique_id(self):
        output = "User 0:\n  User Id: 12345678  Unique Id: 3\n"
        with patch("src.setup.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=output, returncode=0)
            assert _resolve_user_index("12345678") == "3"

    def test_falls_back_to_1_when_not_found(self):
        with patch("src.setup.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            assert _resolve_user_index("unknown") == "1"

    def test_returns_first_match(self):
        output = (
            "User 0:\n  User Id: 11111111  Unique Id: 1\n"
            "User 1:\n  User Id: 22222222  Unique Id: 2\n"
        )
        with patch("src.setup.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=output, returncode=0)
            assert _resolve_user_index("22222222") == "2"


# ── _parse_tan_modes ───────────────────────────────────────────────────────────

class TestParseTanModes:
    def test_parses_standard_format(self):
        output = "  7940 : DKB App (pushTAN)\n  6903 : 1822TAN+\n"
        modes = _parse_tan_modes(output)
        assert modes == [
            {"id": 7940, "description": "DKB App (pushTAN)"},
            {"id": 6903, "description": "1822TAN+"},
        ]

    def test_ignores_non_matching_lines(self):
        output = "Available iTAN modes:\n  7940 : DKB App\nEnd of list\n"
        modes = _parse_tan_modes(output)
        assert len(modes) == 1
        assert modes[0]["id"] == 7940

    def test_empty_output(self):
        assert _parse_tan_modes("") == []


# ── _parse_listaccounts ────────────────────────────────────────────────────────

class TestParseListaccounts:
    # AqBanking 6.9.x single-line format
    SAMPLE = (
        "Account 0: Bank: 12030000 Account Number: 1234567890"
        "  SubAccountId: abc  Account Type: bank LocalUniqueId: 1\n"
        "Account 1: Bank: 50050222 Account Number: 9876543210"
        "  SubAccountId: EUR  Account Type: bank LocalUniqueId: 2\n"
    )
    SAMPLE_WITH_IBAN = (
        "Account 0: Bank: 12030000 Account Number: 1234567890"
        "  IBAN: DE12300120001234567890  LocalUniqueId: 1\n"
    )

    def test_returns_two_accounts(self):
        accounts = _parse_listaccounts(self.SAMPLE)
        assert len(accounts) == 2

    def test_aqbanking_id_parsed(self):
        accounts = _parse_listaccounts(self.SAMPLE)
        assert accounts[0]["aqbanking_id"] == 1
        assert accounts[1]["aqbanking_id"] == 2

    def test_iban_parsed(self):
        accounts = _parse_listaccounts(self.SAMPLE_WITH_IBAN)
        assert accounts[0]["iban"] == "DE12300120001234567890"

    def test_iban_none_when_absent(self):
        accounts = _parse_listaccounts(self.SAMPLE)
        assert accounts[0]["iban"] is None

    def test_bank_code_parsed(self):
        accounts = _parse_listaccounts(self.SAMPLE)
        assert accounts[0]["bank_code"] == "12030000"

    def test_empty_output(self):
        assert _parse_listaccounts("") == []

    def test_account_type_parsed(self):
        accounts = _parse_listaccounts(self.SAMPLE)
        assert accounts[0]["account_type"] == "bank"
        assert accounts[0]["account_type_label"] == "Girokonto"

    def test_account_type_investment(self):
        line = "Account 0: Bank: 12030000 Account Number: 123  Account Type: investment LocalUniqueId: 1\n"
        accounts = _parse_listaccounts(line)
        assert accounts[0]["account_type_label"] == "Depot"

    def test_account_type_none_when_absent(self):
        accounts = _parse_listaccounts(self.SAMPLE_WITH_IBAN)
        assert accounts[0]["account_type"] is None
        assert accounts[0]["account_type_label"] is None


# ── SetupSession._find_aqbanking_id / _find_iban ───────────────────────────────

class TestFindAccount:
    # DKB: login is unrelated to account number → BLZ match
    ACCOUNTS_DKB = [
        {"bank_code": "12030000", "account_number": "1234567890", "aqbanking_id": 1, "iban": "DE11"},
    ]
    # Consorsbank: Verrechnungskonto has a different BLZ (70120400), but
    # account_number is a prefix of the login ("7163657005001" → "7163657005").
    ACCOUNTS_CONSORS = [
        {"bank_code": "76030080", "account_number": "418407408",  "aqbanking_id": 10, "iban": None},
        {"bank_code": "70120400", "account_number": "7163657005", "aqbanking_id": 9,  "iban": "DE99"},
    ]

    def _session(self, blz, login):
        return SetupSession("id", "acc", login, blz, "https://x", 300, None, "name")

    def test_dkb_matches_by_blz(self):
        s = self._session("12030000", "12345678")
        assert s._find_aqbanking_id(self.ACCOUNTS_DKB) == 1

    def test_consorsbank_prefers_login_prefix_over_blz(self):
        """Verrechnungskonto (different BLZ) wins because account_number is
        a prefix of the login; the depot account (matching BLZ) is skipped."""
        s = self._session("76030080", "7163657005001")
        assert s._find_aqbanking_id(self.ACCOUNTS_CONSORS) == 9

    def test_returns_none_when_no_match(self):
        s = self._session("99999999", "000")
        assert s._find_aqbanking_id(self.ACCOUNTS_CONSORS) is None

    def test_find_iban_dkb(self):
        s = self._session("12030000", "12345678")
        assert s._find_iban(self.ACCOUNTS_DKB) == "DE11"

    def test_find_iban_consorsbank_fallback(self):
        s = self._session("76030080", "7163657005001")
        assert s._find_iban(self.ACCOUNTS_CONSORS) == "DE99"

    def test_find_iban_returns_none_when_no_match(self):
        s = self._session("99999999", "000")
        assert s._find_iban(self.ACCOUNTS_CONSORS) is None


# ── SetupSession ───────────────────────────────────────────────────────────────

def _make_session(tan_mode=7940) -> SetupSession:
    return SetupSession(
        setup_id="test-uuid",
        account_id="dkb",
        login="12345678",
        blz="12030000",
        url="https://fints.dkb.de/fints",
        hbci_version=300,
        tan_mode=tan_mode,
        name="dkb",
    )


def _ok_run(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _make_session_with_cert_proc(tan_mode=7940, returncode=0, already_exited=True):
    """Helper: session with cert_proc already completed (simulates pre-trusted cert)."""
    session = _make_session(tan_mode=tan_mode)
    session.user_index = "1"
    mock_proc = MagicMock()
    mock_proc.poll.return_value = returncode if already_exited else None
    mock_proc.returncode = returncode
    session.cert_proc = mock_proc
    session._cert_master_fd = 99  # dummy fd
    session._cert_output = b""
    return session


def _patch_pty_getsysid(cert_output=b"", proc_poll_returns=None):
    """Context manager: mock pty.openpty + Popen for getsysid tests."""
    mock_proc = MagicMock()
    # poll() returns None (running) until we've read enough, then 0 (done)
    if proc_poll_returns is None:
        proc_poll_returns = [None, None, None, 0]
    mock_proc.poll.side_effect = proc_poll_returns + [0] * 10
    mock_proc.returncode = 0
    mock_proc.wait.return_value = 0

    import contextlib
    @contextlib.contextmanager
    def ctx():
        with patch("src.setup.pty.openpty", return_value=(99, 100)), \
             patch("src.setup.subprocess.Popen", return_value=mock_proc), \
             patch("src.setup.os.close"), \
             patch("src.setup.os.read", return_value=cert_output), \
             patch("src.setup.os.write"), \
             patch("src.setup.select.select", return_value=([99], [], [])):
            yield mock_proc
    return ctx()


CERT_PROMPT_OUTPUT = (
    b"===== Certificate Received =====\r\n"
    b"Name         : brokerage-hbci.consorsbank.de\r\n"
    b"Organisation : BNP PARIBAS SA\r\n"
    b"Country      : FR\r\n"
    b"Valid after  : 2025/04/15 00:00:00\r\n"
    b"Valid until  : 2026/04/14 23:59:59\r\n"
    b"Hash (SHA1)  : 98:48:F9:9B:82:8C:BE:00\r\n"
    b"Status       : The certificate is valid\r\n"
    b"Do you wish to accept this certificate?\r\n"
    b"(1) Yes  (2) No\r\n"
    b"Please enter your choice: "
)


class TestSetupSessionStep1:
    def test_returns_pending_cert_with_certificate_details(self):
        session = _make_session(tan_mode=7940)
        with patch("src.setup.subprocess.run", return_value=_ok_run()), \
             patch("src.setup._resolve_user_index", return_value="1"), \
             _patch_pty_getsysid(cert_output=CERT_PROMPT_OUTPUT):
            result = session.step1_register()
        assert result["status"] == "pending_cert"
        assert "acceptcert" in result["message"]
        assert result["certificate"]["name"] == "brokerage-hbci.consorsbank.de"
        assert result["certificate"]["organisation"] == "BNP PARIBAS SA"

    def test_skips_cert_step_when_already_trusted(self):
        """If cert is pre-trusted, getsysid exits without prompting → skip to listitanmodes."""
        session = _make_session(tan_mode=7940)
        tan_output = "  7940 : DKB App\n"
        with patch("src.setup.subprocess.run", side_effect=[
            _ok_run(),                          # listusers (in _delete_existing_users)
            _ok_run(),                          # adduser
            _ok_run(stdout=tan_output),         # listitanmodes
            _ok_run(),                          # setitanmode
        ]), patch("src.setup._resolve_user_index", return_value="1"), \
           _patch_pty_getsysid(cert_output=b"", proc_poll_returns=[0]):
            # _patch_pty_getsysid's Popen mock handles both getsysid and getaccounts
            result = session.step1_register()
        assert result["status"] == "pending_confirm"

    def test_raises_on_adduser_failure(self):
        session = _make_session()
        with patch("src.setup.subprocess.run", return_value=_ok_run(returncode=1, stderr="fail")), \
             patch("src.setup._resolve_user_index", return_value="1"):
            with pytest.raises(RuntimeError, match="adduser failed"):
                session.step1_register()


class TestAfterGetsysidGetitanmodes:
    """Tests for the Consorsbank path: getsysid returns no TAN modes in BPD,
    so getitanmodes is called explicitly to query them from the bank."""

    def test_calls_getitanmodes_when_listitanmodes_empty_then_finds_modes(self):
        """If listitanmodes is empty after getsysid, getitanmodes is called,
        and then listitanmodes is retried — if it then returns modes, auto-select proceeds."""
        session = _make_session_with_cert_proc(tan_mode=6900)
        tan_modes_output = "  6900 : pushTAN\n"
        with patch("src.setup.subprocess.run", side_effect=[
            _ok_run(stdout=""),             # listitanmodes (empty)
            _ok_run(),                      # getitanmodes
            _ok_run(stdout=tan_modes_output),  # listitanmodes (retry, finds modes)
            _ok_run(),                      # setitanmode
        ]), _patch_pty_getaccounts():
            result = session._after_getsysid()
        assert result["status"] == "pending_confirm"
        assert result["auto_selected_tan_mode"] == 6900

    def test_calls_getitanmodes_when_listitanmodes_empty_remains_empty(self):
        """If getitanmodes is called but listitanmodes still returns nothing,
        we proceed without a TAN mode (one-step TAN fallback)."""
        session = _make_session_with_cert_proc(tan_mode=None)
        with patch("src.setup.subprocess.run", side_effect=[
            _ok_run(stdout=""),  # listitanmodes (empty)
            _ok_run(),           # getitanmodes
            _ok_run(stdout=""),  # listitanmodes (retry, still empty)
        ]), _patch_pty_getaccounts():
            result = session._after_getsysid()
        assert result["status"] == "pending_confirm"
        assert result["tan_modes"] == []

    def test_getitanmodes_failure_is_non_fatal(self):
        """getitanmodes failure only logs a warning; setup continues."""
        session = _make_session_with_cert_proc(tan_mode=None)
        with patch("src.setup.subprocess.run", side_effect=[
            _ok_run(stdout=""),                        # listitanmodes (empty)
            _ok_run(returncode=1, stderr="timeout"),   # getitanmodes (fails)
            _ok_run(stdout=""),                        # listitanmodes (retry)
        ]), _patch_pty_getaccounts():
            result = session._after_getsysid()
        assert result["status"] == "pending_confirm"


class TestSetupSessionStep1b:
    def test_returns_pending_confirm_when_tan_mode_known(self):
        session = _make_session_with_cert_proc(tan_mode=7940)
        tan_output = "  7940 : DKB App\n"
        with patch("src.setup.subprocess.run", return_value=_ok_run(stdout=tan_output)), \
             patch("src.setup.os.close"), \
             patch("src.setup.select.select", return_value=([], [], [])), \
             _patch_pty_getaccounts():
            result = session.step1b_accept_cert(True)
        assert result["status"] == "pending_confirm"
        assert result["auto_selected_tan_mode"] == 7940

    def test_returns_pending_tan_mode_when_tan_mode_unknown(self):
        session = _make_session_with_cert_proc(tan_mode=None)
        tan_output = "  7940 : DKB App\n"
        with patch("src.setup.subprocess.run", return_value=_ok_run(stdout=tan_output)), \
             patch("src.setup.os.close"), \
             patch("src.setup.select.select", return_value=([], [], [])):
            result = session.step1b_accept_cert(True)
        assert result["status"] == "pending_tan_mode"
        assert result["tan_modes"] == [{"id": 7940, "description": "DKB App"}]

    def test_raises_on_reject(self):
        session = _make_session_with_cert_proc()
        with patch("src.setup.os.close"), \
             patch("src.setup.select.select", return_value=([], [], [])):
            with pytest.raises(RuntimeError, match="rejected"):
                session.step1b_accept_cert(False)

    def test_raises_on_getsysid_failure(self):
        session = _make_session_with_cert_proc(returncode=1)
        with patch("src.setup.os.close"), \
             patch("src.setup.select.select", return_value=([], [], [])):
            with pytest.raises(RuntimeError, match="getsysid failed"):
                session.step1b_accept_cert(True)


class TestSetupSessionStep2:
    def test_sets_tan_mode_returns_pending_confirm(self):
        session = _make_session(tan_mode=None)
        session.user_index = "1"
        with patch("src.setup.subprocess.run", return_value=_ok_run()), \
             _patch_pty_getaccounts():
            result = session.step2_set_tanmode(6903)
        assert result["status"] == "pending_confirm"
        assert session.tan_mode == 6903

    def test_raises_on_setitanmode_failure(self):
        session = _make_session(tan_mode=None)
        session.user_index = "1"
        with patch("src.setup.subprocess.run", return_value=_ok_run(returncode=1, stderr="fail")):
            with pytest.raises(RuntimeError, match="setitanmode failed"):
                session.step2_set_tanmode(6903)


TAN_PROMPT_OUTPUT = (
    b"===== TAN Entry =====\r\n"
    b"Please enter the TAN for user 7163657005001 at BNP Paribas.\r\n"
    b"The server provided the following challenge:\r\n"
    b"Bitte TAN eingeben.\r\n"
    b"Input: "
)


import contextlib

def _patch_pty_getaccounts(acc_output=b"", proc_poll_returns=None, proc_returncode=0):
    """Context manager: mock pty.openpty + Popen for getaccounts PTY tests."""
    mock_proc = MagicMock()
    if proc_poll_returns is None:
        proc_poll_returns = [proc_returncode]
    mock_proc.poll.side_effect = proc_poll_returns + [proc_returncode] * 10
    mock_proc.returncode = proc_returncode
    mock_proc.wait.return_value = proc_returncode

    select_result = ([98], [], []) if acc_output else ([], [], [])

    @contextlib.contextmanager
    def ctx():
        with patch("src.setup.pty.openpty", return_value=(98, 99)), \
             patch("src.setup.subprocess.Popen", return_value=mock_proc), \
             patch("src.setup.os.close"), \
             patch("src.setup.os.read", return_value=acc_output), \
             patch("src.setup.os.write"), \
             patch("src.setup.select.select", return_value=select_result):
            yield mock_proc
    return ctx()


class TestSetupSessionStep3:
    LISTACCOUNTS_OUTPUT = (
        "Account 0: Bank: 12030000 Account Number: 1234567890"
        "  SubAccountId: abc  Account Type: bank LocalUniqueId: 1\n"
    )

    def test_returns_ok_with_aqbanking_id(self, tmp_path):
        config = {
            "accounts": [{"id": "dkb", "name": "dkb", "type": "fints",
                          "blz": "12030000", "url": "u", "login": "l", "hbci_version": 300}],
            "targets": {},
        }
        config_path = str(tmp_path / "banks.json")
        with open(config_path, "w") as f:
            json.dump(config, f)

        session = _make_session()
        session.user_index = "1"

        with patch("src.setup.CONFIG_PATH", config_path), \
             patch("src.setup.subprocess.run", side_effect=[
                 _ok_run(),                                  # getaccsepa
                 _ok_run(stdout=self.LISTACCOUNTS_OUTPUT),   # listaccounts
             ]), \
             _patch_pty_getaccounts(acc_output=b"", proc_returncode=0):
            result = session.step3_confirm()

        assert result["status"] == "ok"
        assert result["aqbanking_id"] == 1
        saved = json.loads(open(config_path).read())
        assert saved["accounts"][0]["aqbanking_id"] == 1

    def test_returns_pending_tan_when_challenge_detected(self):
        session = _make_session()
        session.user_index = "1"
        with _patch_pty_getaccounts(acc_output=TAN_PROMPT_OUTPUT, proc_poll_returns=[None] * 5):
            result = session.step3_confirm()

        assert result["status"] == "pending_tan"
        assert "challenge" in result
        assert "Bitte TAN eingeben" in result["challenge"]
        assert "/tan" in result["message"]

    def test_raises_on_getaccounts_failure(self):
        session = _make_session()
        session.user_index = "1"
        with _patch_pty_getaccounts(acc_output=b"", proc_returncode=1):
            with pytest.raises(RuntimeError, match="getaccounts failed"):
                session.step3_confirm()


class TestSetupSessionStep3b:
    LISTACCOUNTS_OUTPUT = (
        "Account 0: Bank: 76030080 Account Number: 7163657005"
        "  SubAccountId: EUR  Account Type: bank LocalUniqueId: 1\n"
    )

    def _make_consors_session_with_tan_prompt(self):
        """Session with getaccounts PTY already at TAN prompt (proc still running)."""
        session = SetupSession(
            setup_id="test-uuid", account_id="consorsbank", login="7163657005001",
            blz="76030080", url="https://brokerage-hbci.consorsbank.de/hbci",
            hbci_version=300, tan_mode=6900, name="Consorsbank",
        )
        session.user_index = "1"
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [0] + [0] * 10
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0
        session.proc = mock_proc
        session._acc_master_fd = 98
        session._acc_output = TAN_PROMPT_OUTPUT
        return session

    def test_returns_ok_after_tan_entry(self, tmp_path):
        config = {
            "accounts": [{"id": "consorsbank", "name": "Consorsbank", "type": "fints",
                          "blz": "76030080", "url": "u", "login": "l", "hbci_version": 300}],
            "targets": {},
        }
        config_path = str(tmp_path / "banks.json")
        with open(config_path, "w") as f:
            json.dump(config, f)
        session = self._make_consors_session_with_tan_prompt()
        with patch("src.setup.CONFIG_PATH", config_path), \
             patch("src.setup.os.write"), \
             patch("src.setup.os.close"), \
             patch("src.setup.select.select", return_value=([], [], [])), \
             patch("src.setup.subprocess.run", side_effect=[
                 _ok_run(),                                   # getaccsepa
                 _ok_run(stdout=self.LISTACCOUNTS_OUTPUT),    # listaccounts
             ]):
            result = session.step3b_submit_tan("123456")

        assert result["status"] == "ok"
        assert result["aqbanking_id"] == 1

    def test_raises_on_timeout(self):
        session = self._make_consors_session_with_tan_prompt()
        session.proc.poll.side_effect = [None] * 20
        session.proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 5)
        with patch("src.setup.os.write"), \
             patch("src.setup.os.close"), \
             patch("src.setup.select.select", return_value=([], [], [])):
            with pytest.raises(RuntimeError, match="timed out"):
                session.step3b_submit_tan("123456", timeout=0)


# ── Server endpoints ───────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path):
    """Flask test client with a temporary banks.json and profiles.

    Patching is done via server_mod.bank_setup (the bare-import alias used by server.py)
    rather than src.setup, to ensure both references point to the same module object.
    """
    initial_config = {
        "accounts": [],
        "targets": {"firefly": {"enabled": False}},
    }
    config_path = str(tmp_path / "banks.json")
    profiles_path = str(tmp_path / "bank_profiles.json")
    pinfile_path = str(tmp_path / "pinfile")
    profiles = {
        "dkb": {"blz": "12030000", "url": "https://fints.dkb.de/fints", "hbci_version": 300, "tan_mode": 7940},
        "1822direkt": {"blz": "50050222", "url": "https://fints.1822direkt.com/fints/hbci", "hbci_version": 300, "tan_mode": 6903},
        "consorsbank": {"blz": "76030080", "url": "https://brokerage-hbci.consorsbank.de/hbci", "hbci_version": 300, "tan_mode": 6900},
    }
    with open(config_path, "w") as f:
        json.dump(initial_config, f)
    with open(profiles_path, "w") as f:
        json.dump(profiles, f)

    import src.server as server_mod

    # server.py imports `setup as bank_setup` (bare), so patch via that reference
    setup_mod = server_mod.bank_setup

    orig_config = setup_mod.CONFIG_PATH
    orig_profiles = setup_mod.PROFILES_PATH
    orig_pinfile = setup_mod.PINFILE
    setup_mod.CONFIG_PATH = config_path
    setup_mod.PROFILES_PATH = profiles_path
    setup_mod.PINFILE = pinfile_path
    server_mod._pending_setups.clear()
    server_mod.config["accounts"] = []

    yield server_mod.app.test_client()

    setup_mod.CONFIG_PATH = orig_config
    setup_mod.PROFILES_PATH = orig_profiles
    setup_mod.PINFILE = orig_pinfile
    server_mod._pending_setups.clear()


class TestSetupProfilesEndpoint:
    def test_returns_profiles(self, client):
        resp = client.get("/setup/profiles")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "dkb" in data
        assert data["dkb"]["blz"] == "12030000"


class TestSetupStartEndpoint:
    def _mock_step1(self, result):
        return patch("src.server.bank_setup.SetupSession.step1_register", return_value=result)

    def test_missing_login_returns_400(self, client):
        resp = client.post("/setup", json={"bank": "dkb", "pin": "1234"})
        assert resp.status_code == 400

    def test_missing_pin_returns_400(self, client):
        resp = client.post("/setup", json={"bank": "dkb", "login": "12345678"})
        assert resp.status_code == 400

    def test_unknown_profile_returns_400(self, client):
        resp = client.post("/setup", json={"bank": "unknown", "login": "u", "pin": "p"})
        assert resp.status_code == 400

    def test_missing_blz_without_profile_returns_400(self, client):
        resp = client.post("/setup", json={"login": "u", "pin": "p", "url": "http://x"})
        assert resp.status_code == 400

    def test_successful_setup_with_profile(self, client):
        step1_result = {"setup_id": "uuid-1", "status": "pending_cert",
                        "message": "Accept cert via POST /setup/uuid-1/acceptcert"}
        with self._mock_step1(step1_result), \
             patch("src.server.bank_setup._write_pin"):
            resp = client.post("/setup", json={"bank": "dkb", "login": "12345678", "pin": "1234"})
        assert resp.status_code == 202
        assert resp.get_json()["status"] == "pending_cert"

    def test_account_written_to_config(self, client, tmp_path):
        import src.server as server_mod
        setup_mod = server_mod.bank_setup
        step1_result = {"setup_id": "uuid-1", "status": "pending_confirm",
                        "message": "Confirm TAN...", "tan_modes": [], "auto_selected_tan_mode": 7940}
        with self._mock_step1(step1_result), \
             patch("src.server.bank_setup._write_pin"):
            client.post("/setup", json={"bank": "dkb", "login": "12345678", "pin": "1234"})
        saved = json.loads(open(setup_mod.CONFIG_PATH).read())
        assert any(a["id"] == "dkb" for a in saved["accounts"])

    def test_duplicate_account_id_returns_409(self, client, tmp_path):
        import src.server as server_mod
        setup_mod = server_mod.bank_setup
        config = json.loads(open(setup_mod.CONFIG_PATH).read())
        config["accounts"].append({"id": "dkb", "name": "dkb", "type": "fints",
                                    "blz": "12030000", "url": "u", "login": "x", "hbci_version": 300})
        with open(setup_mod.CONFIG_PATH, "w") as f:
            json.dump(config, f)
        with patch("src.server.bank_setup._write_pin"):
            resp = client.post("/setup", json={"bank": "dkb", "login": "12345678", "pin": "1234"})
        assert resp.status_code == 409

    def test_step1_failure_rolls_back_config(self, client, tmp_path):
        import src.server as server_mod
        setup_mod = server_mod.bank_setup
        with patch("src.server.bank_setup.SetupSession.step1_register", side_effect=RuntimeError("fail")), \
             patch("src.server.bank_setup._write_pin"):
            resp = client.post("/setup", json={"bank": "dkb", "login": "12345678", "pin": "1234"})
        assert resp.status_code == 500
        saved = json.loads(open(setup_mod.CONFIG_PATH).read())
        assert not any(a["id"] == "dkb" for a in saved["accounts"])

    def test_manual_setup_without_profile(self, client):
        step1_result = {"setup_id": "uuid-1", "status": "pending_tan_mode",
                        "message": "Select TAN mode...", "tan_modes": []}
        with self._mock_step1(step1_result), \
             patch("src.server.bank_setup._write_pin"):
            resp = client.post("/setup", json={
                "blz": "99999999", "url": "https://bank.example.com/fints",
                "login": "user1", "pin": "1234", "name": "My Bank", "hbci_version": 300,
            })
        assert resp.status_code == 202

    def test_consorsbank_profile_setup(self, client):
        """Consorsbank profile is resolved correctly; login format is preserved."""
        step1_result = {"setup_id": "uuid-c", "status": "pending_cert",
                        "message": "Accept cert via POST /setup/uuid-c/acceptcert",
                        "certificate": {"name": "brokerage-hbci.consorsbank.de"}}
        with self._mock_step1(step1_result), \
             patch("src.server.bank_setup._write_pin"):
            resp = client.post("/setup", json={
                "bank": "consorsbank", "login": "9001234560001", "pin": "1234"
            })
        assert resp.status_code == 202
        assert resp.get_json()["status"] == "pending_cert"
        import src.server as server_mod
        session = list(server_mod._pending_setups.values())[0]
        assert session.blz == "76030080"
        assert session.login == "9001234560001"
        assert session.tan_mode == 6900

    def test_profile_tan_mode_overridable(self, client):
        step1_result = {"setup_id": "uuid-1", "status": "pending_confirm",
                        "message": "Confirm TAN...", "tan_modes": [], "auto_selected_tan_mode": 9999}
        with self._mock_step1(step1_result) as mock_step, \
             patch("src.server.bank_setup._write_pin"):
            client.post("/setup", json={"bank": "dkb", "login": "12345678", "pin": "1234", "tan_mode": 9999})
        # The session was created with tan_mode=9999 (override)
        import src.server as server_mod
        session = list(server_mod._pending_setups.values())[0]
        assert session.tan_mode == 9999


class TestAcceptcertEndpoint:
    def test_unknown_setup_id_returns_404(self, client):
        resp = client.post("/setup/nonexistent/acceptcert", json={"accept": True})
        assert resp.status_code == 404

    def test_missing_accept_field_returns_400(self, client):
        import src.server as server_mod
        session = _make_session()
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        resp = client.post("/setup/s1/acceptcert", json={})
        assert resp.status_code == 400

    def test_accept_true_returns_202(self, client):
        import src.server as server_mod
        session = _make_session()
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        result = {"setup_id": "s1", "status": "pending_confirm", "message": "Confirm TAN..."}
        with patch("src.server.bank_setup.SetupSession.step1b_accept_cert", return_value=result):
            resp = client.post("/setup/s1/acceptcert", json={"accept": True})
        assert resp.status_code == 202
        assert resp.get_json()["status"] == "pending_confirm"

    def test_reject_removes_session(self, client):
        import src.server as server_mod
        session = _make_session()
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        with patch("src.server.bank_setup.SetupSession.step1b_accept_cert",
                   side_effect=RuntimeError("Certificate rejected by user")):
            resp = client.post("/setup/s1/acceptcert", json={"accept": False})
        assert resp.status_code == 500
        assert "s1" not in server_mod._pending_setups


class TestSetupTanmodeEndpoint:
    def test_unknown_setup_id_returns_404(self, client):
        resp = client.post("/setup/nonexistent/tanmode", json={"tan_mode": 7940})
        assert resp.status_code == 404

    def test_missing_tan_mode_returns_400(self, client):
        import src.server as server_mod
        session = _make_session(tan_mode=None)
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        resp = client.post("/setup/s1/tanmode", json={})
        assert resp.status_code == 400

    def test_sets_tanmode_and_returns_202(self, client):
        import src.server as server_mod
        session = _make_session(tan_mode=None)
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        step2_result = {"setup_id": "s1", "status": "pending_confirm", "message": "Confirm..."}
        with patch("src.server.bank_setup.SetupSession.step2_set_tanmode", return_value=step2_result):
            resp = client.post("/setup/s1/tanmode", json={"tan_mode": 7940})
        assert resp.status_code == 202
        assert resp.get_json()["status"] == "pending_confirm"


class TestSetupConfirmEndpoint:
    def test_unknown_setup_id_returns_404(self, client):
        resp = client.post("/setup/nonexistent/confirm")
        assert resp.status_code == 404

    def test_successful_confirm(self, client):
        import src.server as server_mod
        session = _make_session()
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        step3_result = {"status": "ok", "account_id": "dkb", "aqbanking_id": 1,
                        "iban": "DE12300120001234567890", "accounts": []}
        with patch("src.server.bank_setup.SetupSession.step3_confirm", return_value=step3_result):
            resp = client.post("/setup/s1/confirm")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert "s1" not in server_mod._pending_setups

    def test_confirm_failure_restores_session(self, client):
        import src.server as server_mod
        session = _make_session()
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        with patch("src.server.bank_setup.SetupSession.step3_confirm", side_effect=RuntimeError("fail")):
            resp = client.post("/setup/s1/confirm")
        assert resp.status_code == 500
        assert "s1" in server_mod._pending_setups

    def test_pending_tan_keeps_session_alive(self, client):
        import src.server as server_mod
        session = _make_session()
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        step3_result = {"status": "pending_tan", "setup_id": "s1",
                        "challenge": "Bitte TAN eingeben.", "message": "Enter TAN..."}
        with patch("src.server.bank_setup.SetupSession.step3_confirm", return_value=step3_result):
            resp = client.post("/setup/s1/confirm")
        assert resp.status_code == 202
        assert resp.get_json()["status"] == "pending_tan"
        assert "s1" in server_mod._pending_setups


class TestSetupTanEndpoint:
    def test_unknown_setup_id_returns_404(self, client):
        resp = client.post("/setup/nonexistent/tan", json={"tan": "123456"})
        assert resp.status_code == 404

    def test_missing_tan_returns_400(self, client):
        import src.server as server_mod
        session = _make_session()
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        resp = client.post("/setup/s1/tan", json={})
        assert resp.status_code == 400

    def test_successful_tan_returns_ok(self, client):
        import src.server as server_mod
        session = _make_session()
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        step3b_result = {"status": "ok", "account_id": "dkb", "aqbanking_id": 1,
                         "iban": "DE12300120001234567890", "accounts": []}
        with patch("src.server.bank_setup.SetupSession.step3b_submit_tan", return_value=step3b_result):
            resp = client.post("/setup/s1/tan", json={"tan": "123456"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert "s1" not in server_mod._pending_setups

    def test_tan_failure_restores_session(self, client):
        import src.server as server_mod
        session = _make_session()
        session.user_index = "1"
        server_mod._pending_setups["s1"] = session
        with patch("src.server.bank_setup.SetupSession.step3b_submit_tan",
                   side_effect=RuntimeError("wrong TAN")):
            resp = client.post("/setup/s1/tan", json={"tan": "000000"})
        assert resp.status_code == 500
        assert "s1" in server_mod._pending_setups


class TestAccountsEndpoint:
    def test_lists_accounts_with_aqbanking_id(self, client, tmp_path):
        import src.server as server_mod
        setup_mod = server_mod.bank_setup
        config = {
            "accounts": [
                {"id": "dkb", "name": "DKB", "type": "fints",
                 "blz": "12030000", "url": "u", "login": "l", "hbci_version": 300,
                 "aqbanking_id": 1, "iban": "DE12300120001234567890"},
            ],
            "targets": {},
        }
        with open(setup_mod.CONFIG_PATH, "w") as f:
            json.dump(config, f)
        resp = client.get("/accounts")
        assert resp.status_code == 200
        accounts = resp.get_json()
        assert accounts[0]["aqbanking_id"] == 1
        assert accounts[0]["iban"] == "DE12300120001234567890"

    def test_lists_accounts_without_aqbanking_id(self, client, tmp_path):
        import src.server as server_mod
        setup_mod = server_mod.bank_setup
        config = {
            "accounts": [
                {"id": "dkb", "name": "DKB", "type": "fints",
                 "blz": "12030000", "url": "u", "login": "l", "hbci_version": 300},
            ],
            "targets": {},
        }
        with open(setup_mod.CONFIG_PATH, "w") as f:
            json.dump(config, f)
        resp = client.get("/accounts")
        data = resp.get_json()
        assert "aqbanking_id" not in data[0]


class TestDeleteAccountEndpoint:
    def _write_config(self, setup_mod, accounts):
        cfg = {"accounts": accounts, "targets": {}}
        with open(setup_mod.CONFIG_PATH, "w") as f:
            json.dump(cfg, f)

    def test_deletes_by_aqbanking_id(self, client, tmp_path):
        import src.server as server_mod
        setup_mod = server_mod.bank_setup
        self._write_config(setup_mod, [
            {"id": "dkb", "name": "DKB", "type": "fints",
             "blz": "12030000", "url": "u", "login": "l", "hbci_version": 300, "aqbanking_id": 1},
        ])
        resp = client.delete("/accounts/1")
        assert resp.status_code == 200
        assert json.loads(open(setup_mod.CONFIG_PATH).read())["accounts"] == []

    def test_deletes_by_string_account_id(self, client, tmp_path):
        """Incomplete registrations (no aqbanking_id) can be removed by string id."""
        import src.server as server_mod
        setup_mod = server_mod.bank_setup
        self._write_config(setup_mod, [
            {"id": "76030080", "name": "Consorsbank", "type": "fints",
             "blz": "76030080", "url": "u", "login": "l", "hbci_version": 300},
        ])
        resp = client.delete("/accounts/76030080")
        assert resp.status_code == 200
        assert json.loads(open(setup_mod.CONFIG_PATH).read())["accounts"] == []

    def test_returns_404_for_unknown_id(self, client):
        resp = client.delete("/accounts/999")
        assert resp.status_code == 404

    def test_returns_404_for_unknown_string_id(self, client):
        resp = client.delete("/accounts/nonexistent")
        assert resp.status_code == 404
