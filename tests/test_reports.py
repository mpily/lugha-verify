import math

import pytest

from app.reports import campaign_report, chrf, mean_ci, report_csv, wilson


@pytest.mark.parametrize("hyp,ref", [
    ("Choka yacha", "Choka iyacha"), ("abc", "abc"), ("a", "xyz"),
    ("Nawa mikono kwa sabuni", "Osha mikono kwa sabuni kabla ya kula"),
    ("ab", "abcdefgh"), ("Kidaw'ida", "kidawida")])
def test_chrf_matches_sacrebleu(hyp, ref):
    sacrebleu = pytest.importorskip("sacrebleu")
    want = sacrebleu.sentence_chrf(hyp, [ref]).score
    assert math.isclose(chrf(hyp, ref), want, abs_tol=1e-6)


def test_wilson_known_values():
    w = wilson(8, 10)
    assert w["rate"] == 0.8
    assert math.isclose(w["ci95"][0], 0.4902, abs_tol=1e-3)
    assert math.isclose(w["ci95"][1], 0.9433, abs_tol=1e-3)
    assert wilson(0, 0)["rate"] is None


def test_mean_ci_t():
    m = mean_ci([4.0, 5.0, 3.0])
    assert m["mean"] == 4.0 and math.isclose(m["ci95"][1] - 4.0, 4.303 * 1 / math.sqrt(3), rel_tol=1e-6)
    assert mean_ci([5.0])["ci95"] is None and mean_ci([])["mean"] is None


def test_report_from_reviews(w):
    ss = [w.speaker() for _ in range(4)]
    results = []
    for k, (a, b) in enumerate([("5", "4"), ("2 Choka iyacha", "3 Choka iyacha"),
                                ("5", "2 Choka iyacha")]):
        it = w.item(source=f"Sentence number {k}")
        it.candidate.origin, it.candidate.model_ref = "default_api", "space:git-1"
        w.engine.start_item(it)
        w.clock.advance(minutes=2)
        w.reply(ss[0], f"{w.code_for(it, ss[0])} {a}")
        w.reply(ss[1], f"{w.code_for(it, ss[1])} {b}")
        results.append(it.outcome)
    assert results == ["verified", "verified", "unverified"]
    rep = campaign_report(w.db, it.candidate.source.campaign_id)
    # every item has its own campaign in the World helper: check the last one
    (row,) = rep["rows"]
    assert row["n_items"] == 1 and row["low_n"] and row["model_ref"] == "space:git-1"
    assert row["stars"]["n"] == 2 and row["stars"]["mean"] == 3.5
    assert row["accept_unchanged"]["k"] == 1 and row["agreement"] == {
        "n": 1, "k": 0, "rate": 0.0, "ci95": row["agreement"]["ci95"]}
    assert row["chrf"]["n"] == 2
    assert "stars_mean" in report_csv(rep).splitlines()[0]


def test_report_through_api_and_portal():
    import re
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.payouts import FakeAirtimeClient
    from app.sms.at_client import FakeSmsClient
    from app.translators.base import FakeTranslator
    from tests.conftest import Clock, make_settings
    sms, clock = FakeSmsClient(), Clock()
    app = create_app(make_settings(), sms_client=sms, clock=clock, start_scheduler=False,
                     translator=FakeTranslator(), airtime_client=FakeAirtimeClient())
    c = TestClient(app)
    h = {"Authorization": "Bearer t"}
    phones = ["+254711000021", "+254711000022"]
    for p in phones:
        c.post("/api/speakers", headers=h, json={"phone": p, "languages": ["eng", "dav"]})
        c.post("/at/sms/inbound", data={"from": p, "text": "YES", "id": p})
    cid = c.post("/api/campaigns", headers=h, json={"sources": [
        {"text": f"Line {i}", "candidates": {"dav": {"text": f"D{i}", "model_ref": "m1"}}}
        for i in range(6)]}).json()["id"]
    c.post(f"/api/campaigns/{cid}/start", headers=h)
    clock.advance(minutes=2)
    for p in phones:
        for m in sms.to(p):
            code = re.match(r"([A-Z2-9]{4}) Rate", m)
            if code:
                c.post("/at/sms/inbound", data={"from": p, "text": f"{code[1]} 5",
                                                "id": p + code[1]})
    rep = c.get(f"/api/campaigns/{cid}/report", headers=h).json()
    (row,) = rep["rows"]
    assert row["n_items"] == 6 and not row["low_n"] and row["stars"]["mean"] == 5.0
    assert row["agreement"]["rate"] == 1.0 and row["chrf"]["mean"] == 100.0
    assert c.get(f"/api/campaigns/{cid}/report?format=csv", headers=h).text.count("\n") == 2
    assert c.get("/api/campaigns/99/report", headers=h).status_code == 404
    c.post("/portal/login", data={"token": "t"})
    page = c.get(f"/portal/campaigns/{cid}").text
    assert "Machine draft scores" in page and "dav · m1" in page and "n=12" in page
    assert c.get(f"/portal/campaigns/{cid}/export.report.csv").text.startswith("lang,origin")
