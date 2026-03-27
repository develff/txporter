"""Tests for Firefly III transaction mapping."""

import pytest
from unittest.mock import patch, MagicMock
from src.firefly import FireflyClient, _build_description, _build_notes


ACCOUNT = {"name": "DKB Girokonto"}
CONFIG = {"url": "https://firefly.example.com", "token": "secret"}


def make_tx(**overrides) -> dict:
    base = {
        "external_id": "aqbanking:fints:1000000088:20250401:-20.00:EUR:REF-0001:7000",
        "type": "statement",
        "sub_type": "none",
        "command": "none",
        "status": "unknown",
        "unique_account_id": "",
        "unique_id": "0",
        "ref_unique_id": "",
        "id_for_application": "",
        "session_id": "",
        "group_id": "",
        "acknowledge": "never",
        "local_bank_code": "12030000",
        "local_account_number": "1000000088",
        "remote_bank_code": "",
        "remote_account_number": "",
        "remote_iban": "DE00100000000000000001",
        "remote_bic": "MUSTERDEBBXXX",
        "remote_name": "Max Mustermann",
        "date": "20250401",
        "valuta_date": "20250401",
        "amount_eur": -20.0,
        "currency_code": "EUR",
        "transaction_code": "",
        "transaction_text": "DAUERAUFTRAG",
        "transaction_key": "STO",
        "text_key": "",
        "primanota": "7000",
        "purpose": "Miete April",
        "bank_reference": "REF-0001",
        "end_to_end_reference": "",
        "sequence": "unknown",
        "charge": "",
        "period": "",
        "cycle": "0",
        "execution_day": "",
        "estatement_number": "",
        "estatement_max_entries": "",
        "vop_result": "none",
    }
    base.update(overrides)
    return base


class TestBuildDescription:
    def test_both_present(self):
        tx = make_tx(transaction_text="DAUERAUFTRAG", purpose="Miete April")
        assert _build_description(tx) == "DAUERAUFTRAG – Miete April"

    def test_text_only(self):
        tx = make_tx(transaction_text="LASTSCHRIFT", purpose="")
        assert _build_description(tx) == "LASTSCHRIFT"

    def test_purpose_only(self):
        tx = make_tx(transaction_text="", purpose="Miete April")
        assert _build_description(tx) == "Miete April"

    def test_both_empty(self):
        tx = make_tx(transaction_text="", purpose="")
        assert _build_description(tx) == ""


class TestBuildNotes:
    def test_skips_empty_values(self):
        tx = make_tx(transaction_code="", id_for_application="")
        notes = _build_notes(tx)
        assert "transaction_code" not in notes
        assert "id_for_application" not in notes

    def test_skips_zero_values(self):
        tx = make_tx(unique_id="0", cycle="0")
        notes = _build_notes(tx)
        assert "unique_id" not in notes
        assert "cycle" not in notes

    def test_includes_non_empty_fields(self):
        tx = make_tx(local_bank_code="12030000", acknowledge="never", sequence="unknown")
        notes = _build_notes(tx)
        assert "local_bank_code: 12030000" in notes
        assert "acknowledge: never" in notes
        assert "sequence: unknown" in notes

    def test_vop_result_none_included(self):
        # "none" is not empty and not "0", so it should be included
        tx = make_tx(vop_result="none")
        notes = _build_notes(tx)
        assert "vop_result: none" in notes


class TestFireflyClientCreateTransaction:
    def _make_client(self):
        return FireflyClient(CONFIG)

    def _post(self, client, tx, status_code=200, json_body=None):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.ok = status_code < 400
        mock_resp.text = str(json_body or "")
        with patch("src.firefly.requests.post", return_value=mock_resp) as mock_post:
            client._create_transaction(tx, ACCOUNT)
        return mock_post

    def test_withdrawal_type(self):
        client = self._make_client()
        tx = make_tx(amount_eur=-20.0)
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["transactions"][0]["type"] == "withdrawal"

    def test_deposit_type(self):
        client = self._make_client()
        tx = make_tx(amount_eur=1.0)
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["transactions"][0]["type"] == "deposit"

    def test_amount_formatting(self):
        client = self._make_client()
        tx = make_tx(amount_eur=-37.76)
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["transactions"][0]["amount"] == "37.76"

    def test_amount_is_positive_for_withdrawal(self):
        client = self._make_client()
        tx = make_tx(amount_eur=-20.0)
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        split = payload["transactions"][0]
        assert float(split["amount"]) > 0

    def test_amount_is_positive_for_deposit(self):
        client = self._make_client()
        tx = make_tx(amount_eur=1.0)
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        split = payload["transactions"][0]
        assert float(split["amount"]) > 0

    def test_external_id_passed_through(self):
        client = self._make_client()
        tx = make_tx()
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        split = payload["transactions"][0]
        assert split["external_id"] == tx["external_id"]

    def test_description_combined(self):
        client = self._make_client()
        tx = make_tx(transaction_text="DAUERAUFTRAG", purpose="Miete April")
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["transactions"][0]["description"] == "DAUERAUFTRAG – Miete April"

    def test_source_name_from_account(self):
        client = self._make_client()
        tx = make_tx()
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["transactions"][0]["source_name"] == "DKB Girokonto"

    def test_destination_name_from_remote_name(self):
        client = self._make_client()
        tx = make_tx(remote_name="Max Mustermann")
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["transactions"][0]["destination_name"] == "Max Mustermann"

    def test_end_to_end_reference_mapped_to_sepa_ct_id(self):
        client = self._make_client()
        tx = make_tx(end_to_end_reference="E2EREF-001")
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["transactions"][0]["sepa_ct_id"] == "E2EREF-001"

    def test_no_sepa_ct_id_when_end_to_end_empty(self):
        client = self._make_client()
        tx = make_tx(end_to_end_reference="")
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        assert "sepa_ct_id" not in payload["transactions"][0]

    def test_primanota_mapped_to_internal_reference(self):
        client = self._make_client()
        tx = make_tx(primanota="7000")
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["transactions"][0]["internal_reference"] == "7000"

    def test_notes_contains_unmapped_fields(self):
        client = self._make_client()
        tx = make_tx(local_bank_code="12030000", remote_iban="DE00100000000000000001")
        mock_post = self._post(client, tx)
        payload = mock_post.call_args.kwargs["json"]
        notes = payload["transactions"][0]["notes"]
        assert "local_bank_code: 12030000" in notes
        assert "remote_iban: DE00100000000000000001" in notes

    def test_error_logged_on_422(self, caplog):
        import logging
        client = self._make_client()
        tx = make_tx()
        with caplog.at_level(logging.ERROR, logger="src.firefly"):
            self._post(client, tx, status_code=422)
        assert "Failed to create transaction" in caplog.text

    def test_error_logged_on_failure(self, caplog):
        import logging
        client = self._make_client()
        tx = make_tx()
        with caplog.at_level(logging.ERROR, logger="src.firefly"):
            self._post(client, tx, status_code=500)
        assert "Failed to create transaction" in caplog.text

    def test_zero_amount_is_skipped(self, caplog):
        import logging
        client = self._make_client()
        tx = make_tx(amount_eur=0.0)
        with caplog.at_level(logging.DEBUG, logger="src.firefly"):
            mock_post = self._post(client, tx)
        mock_post.assert_not_called()
        assert "zero-amount" in caplog.text
