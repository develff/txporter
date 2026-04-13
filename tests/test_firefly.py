"""Tests for Firefly III transaction mapping."""

import pytest
from unittest.mock import patch, MagicMock
from src.firefly import FireflyClient, _build_description, _build_notes, _german_iban, _iso_date


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


class TestDedupStartDate:
    def test_returns_min_date_minus_buffer(self):
        txs = [make_tx(date="20260301"), make_tx(date="20260215"), make_tx(date="20260310")]
        result = FireflyClient._dedup_start_date(txs, buffer_days=7)
        assert result == "2026-02-08"  # 20260215 - 7 days

    def test_empty_transactions_returns_none(self):
        assert FireflyClient._dedup_start_date([]) is None

    def test_transactions_without_date_returns_none(self):
        assert FireflyClient._dedup_start_date([{"amount_eur": 1.0}]) is None

    def test_default_buffer_is_seven_days(self):
        txs = [make_tx(date="20260101")]
        result = FireflyClient._dedup_start_date(txs)
        assert result == "2025-12-25"  # 20260101 - 7 days

    def test_fetch_uses_start_date_param(self):
        """_fetch_existing_external_ids passes start_date to the Firefly API."""
        client = FireflyClient(CONFIG)
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"data": [], "meta": {"pagination": {"total_pages": 1}}}
        with patch("src.firefly.requests.get", return_value=mock_resp) as mock_get:
            client._fetch_existing_external_ids("42", start_date="2026-02-08")
        call_params = mock_get.call_args.kwargs["params"]
        assert call_params["start"] == "2026-02-08"

    def test_fetch_without_start_date_omits_param(self):
        client = FireflyClient(CONFIG)
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"data": [], "meta": {"pagination": {"total_pages": 1}}}
        with patch("src.firefly.requests.get", return_value=mock_resp) as mock_get:
            client._fetch_existing_external_ids("42")
        call_params = mock_get.call_args.kwargs["params"]
        assert "start" not in call_params


class TestGetTags:
    def _mock_get(self, pages):
        """Build a side_effect list for requests.get from a list of tag-name pages."""
        responses = []
        total = len(pages)
        for i, tags in enumerate(pages):
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {
                "data": [{"attributes": {"tag": t}} for t in tags],
                "meta": {"pagination": {"total_pages": total}},
            }
            responses.append(mock_resp)
        return responses

    def test_returns_sorted_tags(self):
        client = FireflyClient(CONFIG)
        with patch("src.firefly.requests.get", side_effect=self._mock_get([["Zebra", "Alpha", "Mitte"]])):
            tags = client.get_tags()
        assert tags == ["Alpha", "Mitte", "Zebra"]

    def test_paginates_multiple_pages(self):
        client = FireflyClient(CONFIG)
        with patch("src.firefly.requests.get", side_effect=self._mock_get([["A", "B"], ["C"]])):
            tags = client.get_tags()
        assert sorted(tags) == ["A", "B", "C"]

    def test_returns_empty_on_api_error(self):
        client = FireflyClient(CONFIG)
        mock_resp = MagicMock()
        mock_resp.ok = False
        with patch("src.firefly.requests.get", return_value=mock_resp):
            assert client.get_tags() == []

    def test_skips_entries_without_tag_attribute(self):
        client = FireflyClient(CONFIG)
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "data": [{"attributes": {"tag": "Good"}}, {"attributes": {}}],
            "meta": {"pagination": {"total_pages": 1}},
        }
        with patch("src.firefly.requests.get", return_value=mock_resp):
            assert client.get_tags() == ["Good"]


class TestIsoDate:
    def test_converts_yyyymmdd_to_iso(self):
        assert _iso_date("20250401") == "2025-04-01"

    def test_passes_through_iso_format(self):
        assert _iso_date("2025-04-01") == "2025-04-01"

    def test_passes_through_empty_string(self):
        assert _iso_date("") == ""

    def test_passes_through_non_numeric(self):
        assert _iso_date("2025-04-01T00:00:00+00:00") == "2025-04-01T00:00:00+00:00"


class TestCreateTransactionDateFormat:
    """date and book_date fields must be ISO YYYY-MM-DD for Firefly's date filter to work."""

    def _post(self, tx):
        client = FireflyClient(CONFIG)
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        with patch("src.firefly.requests.post", return_value=mock_resp) as mock_post:
            client._create_transaction(tx, ACCOUNT)
        return mock_post.call_args.kwargs["json"]["transactions"][0]

    def test_date_converted_to_iso(self):
        split = self._post(make_tx(date="20250401"))
        assert split["date"] == "2025-04-01"

    def test_book_date_converted_to_iso(self):
        split = self._post(make_tx(valuta_date="20250401"))
        assert split["book_date"] == "2025-04-01"


class TestGermanIban:
    def test_computes_iban_from_blz_and_account_number(self):
        # DKB BLZ 12030000, known account → verify formula produces valid DE IBAN
        iban = _german_iban({"blz": "12030000", "account_number": "1234567890"})
        assert iban is not None
        assert iban.startswith("DE")
        assert len(iban) == 22
        # Verify check digits: re-running the formula on the result should give 1
        digits = iban[4:] + "1314" + iban[2:4]
        assert int(digits) % 97 == 1

    def test_pads_short_account_number(self):
        iban = _german_iban({"blz": "12030000", "account_number": "15788953"})
        assert iban is not None
        assert iban[4:12] == "12030000"
        assert iban[12:] == "0015788953"

    def test_prefers_stored_iban(self):
        assert _german_iban({"iban": "DE89370400440532013000"}) == "DE89370400440532013000"

    def test_returns_none_without_blz(self):
        assert _german_iban({"account_number": "1234567890"}) is None

    def test_returns_none_for_non_numeric_account(self):
        assert _german_iban({"blz": "12030000", "account_number": "ABC123"}) is None

    def test_uses_bank_code_aq_fallback(self):
        iban = _german_iban({"bank_code_aq": "12030000", "account_number": "1234567890"})
        assert iban is not None
        assert iban.startswith("DE")
