"""Tests for aqbanking CTX parser."""

import pytest
from pathlib import Path
from src.aqbanking import _decode_amount_minor_units, _external_id, _parse_ctx

FIXTURE = Path(__file__).parent / "fixtures" / "dkb_sample.ctx"


class TestDecodeAmountMinorUnits:
    def test_plain_negative(self):
        assert _decode_amount_minor_units("-3776%3AEUR") == (-3776, "EUR")

    def test_plain_positive(self):
        assert _decode_amount_minor_units("1%3AEUR") == (1, "EUR")

    def test_fraction(self):
        # -25/10:EUR → numerator -25
        assert _decode_amount_minor_units("-25%2F10%3AEUR") == (-25, "EUR")

    def test_fraction_large_denominator(self):
        # -3776/100:EUR → numerator -3776
        assert _decode_amount_minor_units("-3776%2F100%3AEUR") == (-3776, "EUR")

    def test_small_negative(self):
        assert _decode_amount_minor_units("-20%3AEUR") == (-20, "EUR")

    def test_already_decoded(self):
        # If somehow passed already-decoded string
        assert _decode_amount_minor_units("-3776:EUR") == (-3776, "EUR")


class TestExternalId:
    def test_with_both_ref_and_primanota(self):
        eid = _external_id("1000000088", "20250401", -3776, "EUR", "REF-0001", "7000")
        assert eid == "aqbanking:fints:1000000088:20250401:-3776:EUR:REF-0001:7000"

    def test_with_ref_only(self):
        eid = _external_id("1000000088", "20250401", -3776, "EUR", "REF-0001", "")
        assert eid == "aqbanking:fints:1000000088:20250401:-3776:EUR:REF-0001"

    def test_with_primanota_only(self):
        eid = _external_id("1000000088", "20250401", -3776, "EUR", "", "7000")
        assert eid == "aqbanking:fints:1000000088:20250401:-3776:EUR:7000"

    def test_primanota_zero_excluded(self):
        eid = _external_id("1000000088", "20250401", -3776, "EUR", "REF-0001", "0")
        assert eid == "aqbanking:fints:1000000088:20250401:-3776:EUR:REF-0001"

    def test_stable(self):
        args = ("1000000088", "20250401", -3776, "EUR", "REF-0001", "7000")
        assert _external_id(*args) == _external_id(*args)

    def test_different_input_different_id(self):
        a = _external_id("1000000088", "20250401", -3776, "EUR", "REF-0001", "7000")
        b = _external_id("1000000088", "20250402", -3776, "EUR", "REF-0002", "7000")
        assert a != b

    def test_no_ref_no_primanota_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="src.aqbanking"):
            eid = _external_id("1000000088", "20250401", -3776, "EUR", "", "")
        assert "aqbanking:fints:1000000088:20250401:-3776:EUR" == eid
        assert "may not be unique" in caplog.text


class TestParseCtx:
    @pytest.fixture(scope="class")
    def transactions(self):
        return _parse_ctx(FIXTURE.read_text())

    def test_transaction_count(self, transactions):
        assert len(transactions) == 28

    def test_first_transaction_fields(self, transactions):
        tx = transactions[0]
        assert tx["date"] == "20250401"
        assert tx["valuta_date"] == "20250401"
        assert tx["amount_minor_units"] == -20
        assert tx["currency_code"] == "EUR"
        assert tx["remote_name"] == "Max Mustermann"
        assert tx["remote_iban"] == "DE00100000000000000001"
        assert tx["remote_bic"] == "MUSTERDEBBXXX"
        assert tx["transaction_text"] == "DAUERAUFTRAG"
        assert tx["transaction_key"] == "STO"
        assert tx["purpose"] == "MusterzweckPURP+RINP"
        assert tx["bank_reference"] == "REF-0001"
        assert tx["primanota"] == "7000"
        assert tx["local_account_number"] == "1000000088"

    def test_first_transaction_external_id(self, transactions):
        tx = transactions[0]
        assert tx["external_id"] == "aqbanking:fints:1000000088:20250401:-20:EUR:REF-0001:7000"

    def test_fraction_amount(self, transactions):
        # Transaction 2: value="-25/10:EUR" → minor_units=-25
        tx = transactions[1]
        assert tx["amount_minor_units"] == -25
        assert tx["currency_code"] == "EUR"

    def test_positive_amount(self, transactions):
        # Transaction 3: value="1:EUR" → deposit
        tx = transactions[2]
        assert tx["amount_minor_units"] == 1
        assert tx["currency_code"] == "EUR"

    def test_large_fraction_amount(self, transactions):
        # Transaction 6: value="-3776/100:EUR" → minor_units=-3776
        tx = transactions[5]
        assert tx["amount_minor_units"] == -3776
        assert tx["currency_code"] == "EUR"

    def test_purpose_url_decoded(self, transactions):
        # purpose="MusterzweckPURP%2BRINP" → "MusterzweckPURP+RINP"
        tx = transactions[0]
        assert "+" in tx["purpose"]

    def test_end_to_end_reference_present(self, transactions):
        # Transaction 2 has endToEndReference
        tx = transactions[1]
        assert tx["end_to_end_reference"] != ""

    def test_end_to_end_reference_absent(self, transactions):
        # Transaction 1 has no endToEndReference
        tx = transactions[0]
        assert tx["end_to_end_reference"] == ""

    def test_stable_parse(self):
        content = FIXTURE.read_text()
        assert _parse_ctx(content) == _parse_ctx(content)

    def test_required_keys_present(self, transactions):
        required = {
            "external_id",
            "type", "sub_type", "command", "status",
            "unique_account_id", "unique_id", "ref_unique_id",
            "id_for_application", "session_id", "group_id", "acknowledge",
            "local_bank_code", "local_account_number",
            "remote_bank_code", "remote_account_number",
            "remote_name", "remote_iban", "remote_bic",
            "date", "valuta_date", "amount_minor_units", "currency_code",
            "transaction_code", "transaction_text", "transaction_key", "text_key",
            "purpose", "bank_reference", "primanota", "end_to_end_reference",
            "sequence", "charge", "period",
            "cycle", "execution_day",
            "estatement_number", "estatement_max_entries",
            "vop_result",
        }
        for tx in transactions:
            assert required <= tx.keys()

    def test_metadata_fields(self, transactions):
        tx = transactions[0]
        assert tx["type"] == "statement"
        assert tx["sub_type"] == "none"
        assert tx["status"] == "unknown"
        assert tx["acknowledge"] == "never"
        assert tx["unique_id"] == "0"
        assert tx["sequence"] == "unknown"
        assert tx["vop_result"] == "none"
        assert tx["cycle"] == "0"
