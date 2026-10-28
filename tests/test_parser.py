import pytest

from app.sms.parser import Keyword, ParseError, Reply, normalize_for_compare, parse_sms

CASES = [
    ("K7P2 5", Reply("K7P2", 5, None, False)),
    ("k7p2 5", Reply("K7P2", 5, None, False)),
    ("  K7P2   3   Choka   iyacha ", Reply("K7P2", 3, "Choka iyacha", False)),
    ("K7P2 Choka yacha", Reply("K7P2", None, "Choka yacha", False)),
    ("K7P2 3 Choka iyacha SHARE", Reply("K7P2", 3, "Choka iyacha", True)),
    ("K7P2 5 share", Reply("K7P2", 5, None, True)),
    ("K7P2 Kidaw’ida ni nicha", Reply("K7P2", None, "Kidaw'ida ni nicha", False)),
    ('K7P2 4 "Choka yacha"', Reply("K7P2", 4, "Choka yacha", False)),
    ("K7P2 “Choka yacha” Share.", Reply("K7P2", None, "Choka yacha", True)),
    ("YES", Keyword("YES")), ("yes.", Keyword("YES")), ("Stop", Keyword("STOP")),
    ("STOP sending me these", Keyword("STOP")), ("help", Keyword("HELP")),
    ("", ParseError("empty")), ("   ", ParseError("empty")),
    ("hello there", ParseError("bad_code")),
    ("K0P2 5", ParseError("bad_code")),   # 0 not in alphabet
    ("KIP2 5", ParseError("bad_code")),   # I not in alphabet
    ("K7P 5", ParseError("bad_code")),
    ("K7P2 7", ParseError("bad_stars")), ("K7P2 0 x", ParseError("bad_stars")),
    ("K7P2 12 x", ParseError("bad_stars")),
    ("K7P2", ParseError("no_content")), ("K7P2 SHARE", ParseError("no_content")),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_parse_table(raw, expected):
    assert parse_sms(raw) == expected


def test_compare_normalization():
    assert normalize_for_compare("Choka  Yacha.") == normalize_for_compare("choka yacha")
    assert normalize_for_compare("Kidaw’ida") == normalize_for_compare("kidaw'ida")
