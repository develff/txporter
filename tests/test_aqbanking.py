"""Tests for aqbanking CTX parser."""

import hashlib
import pytest
from pathlib import Path
from external_id import build_external_id
from src.aqbanking import _decode_amount_eur, _parse_ctx

FIXTURE = Path(__file__).parent / "fixtures" / "dkb_sample.ctx"


class TestDecodeAmountEur:
    def test_plain_negative(self):
        assert _decode_amount_eur("-3776%3AEUR") == (-3776.0, "EUR")

    def test_plain_positive(self):
        assert _decode_amount_eur("1%3AEUR") == (1.0, "EUR")

    def test_fraction(self):
        # -25/10:EUR → -25/10 = -€2.50
        assert _decode_amount_eur("-25%2F10%3AEUR") == (-2.5, "EUR")

    def test_fraction_large_denominator(self):
        # -3776/100:EUR → -3776/100 = -€37.76
        assert _decode_amount_eur("-3776%2F100%3AEUR") == (-37.76, "EUR")

    def test_small_negative(self):
        assert _decode_amount_eur("-20%3AEUR") == (-20.0, "EUR")

    def test_already_decoded(self):
        assert _decode_amount_eur("-3776:EUR") == (-3776.0, "EUR")


class TestExternalId:
    def test_with_end_to_end_ref(self):
        eid = build_external_id("1000000088", "20250401", -37.76, "EUR", end_to_end_ref="REF-0001")
        assert eid == "txporter:1000000088:20250401:-37.76:EUR:REF-0001"

    def test_notprovided_falls_to_fingerprint(self):
        eid = build_external_id("1000000088", "20250401", -37.76, "EUR",
                                end_to_end_ref="NOTPROVIDED",
                                remote_iban="DE00100000000000000001",
                                remote_name="Shop GmbH", description="Zweck")
        expected = "txporter:1000000088:20250401:-37.76:EUR:" + \
                   hashlib.sha256(b"DE00100000000000000001|Shop GmbH|Zweck").hexdigest()[:8]
        assert eid == expected

    def test_no_ref_uses_fingerprint(self):
        eid = build_external_id("1000000088", "20250401", -37.76, "EUR",
                                remote_iban="DE00100000000000000001",
                                remote_name="Shop GmbH", description="Zweck")
        expected = "txporter:1000000088:20250401:-37.76:EUR:" + \
                   hashlib.sha256(b"DE00100000000000000001|Shop GmbH|Zweck").hexdigest()[:8]
        assert eid == expected

    def test_fingerprint_differs_by_remote_iban(self):
        a = build_external_id("1000000088", "20250401", -37.76, "EUR",
                              remote_iban="DE11111111111111111111", description="Zweck")
        b = build_external_id("1000000088", "20250401", -37.76, "EUR",
                              remote_iban="DE22222222222222222222", description="Zweck")
        assert a != b

    def test_stable(self):
        eid = build_external_id("1000000088", "20250401", -37.76, "EUR", end_to_end_ref="REF-0001")
        assert eid == build_external_id("1000000088", "20250401", -37.76, "EUR", end_to_end_ref="REF-0001")

    def test_different_input_different_id(self):
        a = build_external_id("1000000088", "20250401", -37.76, "EUR", end_to_end_ref="REF-0001")
        b = build_external_id("1000000088", "20250402", -37.76, "EUR", end_to_end_ref="REF-0002")
        assert a != b


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
        assert tx["amount_eur"] == -20.0
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
        # tx[0] has no endToEndReference → fingerprint(remote_iban|remote_name|purpose)
        assert tx["external_id"] == "txporter:DE06120300001000000088:20250401:-20.00:EUR:dc6dc59a"

    def test_fraction_amount(self, transactions):
        # Transaction 2: value="-25/10:EUR" → -€2.50
        tx = transactions[1]
        assert tx["amount_eur"] == -2.5
        assert tx["currency_code"] == "EUR"

    def test_positive_amount(self, transactions):
        # Transaction 3: value="1:EUR" → €1.00 deposit
        tx = transactions[2]
        assert tx["amount_eur"] == 1.0
        assert tx["currency_code"] == "EUR"

    def test_large_fraction_amount(self, transactions):
        # Transaction 6: value="-3776/100:EUR" → -€37.76
        tx = transactions[5]
        assert tx["amount_eur"] == -37.76
        assert tx["currency_code"] == "EUR"

    def test_purpose_url_decoded(self, transactions):
        tx = transactions[0]
        assert "+" in tx["purpose"]

    def test_end_to_end_reference_present(self, transactions):
        tx = transactions[1]
        assert tx["end_to_end_reference"] != ""

    def test_end_to_end_reference_absent(self, transactions):
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
            "date", "valuta_date", "amount_eur", "currency_code",
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
