"""Tests for bank setup REST API and helper functions."""

import json
import os
import pytest
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
    SAMPLE = (
        "Account 0 (Unique Account Id=1):\n"
        "  Bank Code         : 12030000\n"
        "  IBAN              : DE12300120001234567890\n"
        "  Account Number    : 1234567890\n"
        "Account 1 (Unique Account Id=2):\n"
        "  Bank Code         : 50050222\n"
        "  IBAN              : DE98500502221234567890\n"
        "  Account Number    : 9876543210\n"
    )

    def test_returns_two_accounts(self):
        accounts = _parse_listaccounts(self.SAMPLE)
        assert len(accounts) == 2

    def test_aqbanking_id_parsed(self):
        accounts = _parse_listaccounts(self.SAMPLE)
        assert accounts[0]["aqbanking_id"] == 1
        assert accounts[1]["aqbanking_id"] == 2

    def test_iban_parsed(self):
        accounts = _parse_listaccounts(self.SAMPLE)
        assert accounts[0]["iban"] == "DE12300120001234567890"

    def test_bank_code_parsed(self):
        accounts = _parse_listaccounts(self.SAMPLE)
        assert accounts[0]["bank_code"] == "12030000"

    def test_empty_output(self):
        assert _parse_listaccounts("") == []


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


class TestSetupSessionStep1:
    def test_returns_pending_confirm_when_tan_mode_known(self):
        session = _make_session(tan_mode=7940)
        session.user_index = "1"
        with patch("src.setup.subprocess.run", return_value=_ok_run()), \
             patch("src.setup._resolve_user_index", return_value="1"), \
             patch("src.setup.subprocess.Popen") as mock_popen:
            result = session.step1_register()
        assert result["status"] == "pending_confirm"
        assert result["auto_selected_tan_mode"] == 7940
        mock_popen.assert_called_once()

    def test_returns_pending_tan_mode_when_tan_mode_unknown(self):
        session = _make_session(tan_mode=None)
        tan_output = "  7940 : DKB App\n"
        with patch("src.setup.subprocess.run", side_effect=[
            _ok_run(),                       # adduser
            _ok_run(),                       # getsysid
            _ok_run(stdout=tan_output),      # listitanmodes
        ]), patch("src.setup._resolve_user_index", return_value="1"):
            result = session.step1_register()
        assert result["status"] == "pending_tan_mode"
        assert result["tan_modes"] == [{"id": 7940, "description": "DKB App"}]
        assert "auto_selected_tan_mode" not in result

    def test_raises_on_adduser_failure(self):
        session = _make_session()
        with patch("src.setup.subprocess.run", return_value=_ok_run(returncode=1, stderr="fail")), \
             patch("src.setup._resolve_user_index", return_value="1"):
            with pytest.raises(RuntimeError, match="adduser failed"):
                session.step1_register()

    def test_raises_on_getsysid_failure(self):
        session = _make_session()
        with patch("src.setup.subprocess.run", side_effect=[
            _ok_run(),                              # adduser ok
            _ok_run(returncode=1, stderr="fail"),   # getsysid fails
        ]), patch("src.setup._resolve_user_index", return_value="1"):
            with pytest.raises(RuntimeError, match="getsysid failed"):
                session.step1_register()


class TestSetupSessionStep2:
    def test_sets_tan_mode_and_starts_getaccounts(self):
        session = _make_session(tan_mode=None)
        session.user_index = "1"
        with patch("src.setup.subprocess.run", return_value=_ok_run()), \
             patch("src.setup.subprocess.Popen") as mock_popen:
            result = session.step2_set_tanmode(6903)
        assert result["status"] == "pending_confirm"
        assert session.tan_mode == 6903
        mock_popen.assert_called_once()

    def test_raises_on_setitanmode_failure(self):
        session = _make_session(tan_mode=None)
        session.user_index = "1"
        with patch("src.setup.subprocess.run", return_value=_ok_run(returncode=1, stderr="fail")):
            with pytest.raises(RuntimeError, match="setitanmode failed"):
                session.step2_set_tanmode(6903)


class TestSetupSessionStep3:
    LISTACCOUNTS_OUTPUT = (
        "Account 0 (Unique Account Id=1):\n"
        "  Bank Code         : 12030000\n"
        "  IBAN              : DE12300120001234567890\n"
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
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        session.proc = mock_proc

        with patch("src.setup.CONFIG_PATH", config_path), \
             patch("src.setup.subprocess.run", side_effect=[
                 _ok_run(),                                  # getaccsepa
                 _ok_run(stdout=self.LISTACCOUNTS_OUTPUT),   # listaccounts
             ]):
            result = session.step3_confirm()

        assert result["status"] == "ok"
        assert result["aqbanking_id"] == 1
        assert result["iban"] == "DE12300120001234567890"
        saved = json.loads(open(config_path).read())
        assert saved["accounts"][0]["aqbanking_id"] == 1
        assert saved["accounts"][0]["iban"] == "DE12300120001234567890"

    def test_raises_on_getaccounts_failure(self):
        session = _make_session()
        session.user_index = "1"
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "error!")
        mock_proc.returncode = 1
        session.proc = mock_proc
        with pytest.raises(RuntimeError, match="getaccounts failed"):
            session.step3_confirm()


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
        step1_result = {"setup_id": "uuid-1", "status": "pending_confirm",
                        "message": "Confirm TAN...", "tan_modes": [], "auto_selected_tan_mode": 7940}
        with self._mock_step1(step1_result), \
             patch("src.server.bank_setup._write_pin"):
            resp = client.post("/setup", json={"bank": "dkb", "login": "12345678", "pin": "1234"})
        assert resp.status_code == 202
        assert resp.get_json()["status"] == "pending_confirm"

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
    def test_deletes_existing_account(self, client, tmp_path):
        import src.server as server_mod
        setup_mod = server_mod.bank_setup
        config = {
            "accounts": [
                {"id": "dkb", "name": "DKB", "type": "fints",
                 "blz": "12030000", "url": "u", "login": "l", "hbci_version": 300, "aqbanking_id": 1},
            ],
            "targets": {},
        }
        with open(setup_mod.CONFIG_PATH, "w") as f:
            json.dump(config, f)
        resp = client.delete("/accounts/1")
        assert resp.status_code == 200
        saved = json.loads(open(setup_mod.CONFIG_PATH).read())
        assert saved["accounts"] == []

    def test_returns_404_for_unknown_id(self, client):
        resp = client.delete("/accounts/999")
        assert resp.status_code == 404
