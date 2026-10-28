import pytest
from pydantic import ValidationError

from app.phones import decrypt_phone, encrypt_phone, hash_phone, normalize_phone
from app.sms.segments import count_segments
from app.sms.templates import task_sms
from tests.conftest import KEY, make_settings


def test_needs_review_default():
    s = make_settings()
    assert not s.needs_review("eng") and not s.needs_review("swh")
    assert s.needs_review("dav") and s.needs_review("luo")


def test_settings_fail_fast():
    with pytest.raises(ValidationError):
        make_settings(phone_enc_key="not-a-key")
    with pytest.raises(ValidationError):
        make_settings(at_shortcode=None)
    with pytest.raises(ValidationError):
        make_settings(payout_per_item_kes=99)


def test_phones():
    assert normalize_phone("0712 345 678") == "+254712345678"
    assert hash_phone("0712345678", "s") == hash_phone("+254712345678", "s")
    assert hash_phone("0712345678", "s") != hash_phone("0712345678", "t")
    assert decrypt_phone(encrypt_phone("0712345678", KEY), KEY) == "+254712345678"
    with pytest.raises(ValueError):
        normalize_phone("12ab")


def test_segments():
    assert count_segments("a" * 160) == ("GSM-7", 1)
    assert count_segments("a" * 161) == ("GSM-7", 2)
    assert count_segments("Kidaw’ida") == ("UCS-2", 1)
    assert count_segments("x" * 71 + "’") == ("UCS-2", 2)
    assert count_segments("{" * 80) == ("GSM-7", 1)


def test_task_sms_fits_and_is_gsm7():
    enc, n = count_segments(task_sms("K7P2", "dav", "Choka yacha", "src"))
    assert enc == "GSM-7" and n == 1
