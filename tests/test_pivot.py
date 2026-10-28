"""Swahili pivot: English-only source, machine Swahili reviewed first and
then shown to reviewers who don't read English (user's A/B/C/D/E example)."""
from sqlalchemy import select

from app.models import Payout

EN = "Wash your hands with soap before eating food"
SW = "Nawa mikono kwa sabuni kabla ya kula chakula"
DAV = "Choka yacha"


def setup(w):
    sp = dict(B=w.speaker(langs=["swh", "dav"]), C=w.speaker(langs=["swh", "dav"]),
              D=w.speaker(langs=["eng", "swh", "dav"]), E=w.speaker(langs=["eng", "swh"]))
    items = w.source(EN, {"swh": SW, "dav": DAV})
    w.engine.start_items(list(items.values()))
    return sp, items


def task(item, sp):
    return next((t for t in item.tasks if t.speaker_id == sp.id), None)


def test_first_wave_goes_to_D_and_E(w):
    sp, items = setup(w)
    assert {t.speaker_id for t in items["dav"].tasks} == {sp["D"].id}
    assert {t.speaker_id for t in items["swh"].tasks} == {sp["E"].id}
    assert task(items["dav"], sp["D"]).shown_source_lang == "eng"
    assert 'EN: "Wash' in w.outbox.to(sp["D"].id)[0]
    assert w.outbox.to(sp["B"].id) == [] and w.outbox.to(sp["C"].id) == []


def test_after_E_corrects_B_and_C_get_Es_swahili(w):
    sp, items = setup(w)
    w.clock.advance(minutes=3)
    fixed = "Osha mikono kwa sabuni kabla ya kula chakula"
    w.reply(sp["E"], f"{w.code_for(items['swh'], sp['E'])} 3 {fixed}")
    dav = items["dav"]
    for k in ("B", "C"):
        t = task(dav, sp[k])
        assert t is not None and t.shown_source_lang == "swh"
        assert t.shown_source_text == fixed
        assert f'SW: "{fixed}"' in w.outbox.to(sp[k].id)[-1]


def test_E_accepts_machine_swahili_is_shown(w):
    sp, items = setup(w)
    w.clock.advance(minutes=3)
    w.reply(sp["E"], f"{w.code_for(items['swh'], sp['E'])} 5")
    assert task(items["dav"], sp["B"]).shown_source_text == SW


def test_bare_low_rating_does_not_unlock_swahili(w):
    sp, items = setup(w)
    w.clock.advance(minutes=3)
    w.reply(sp["E"], f"{w.code_for(items['swh'], sp['E'])} 2")
    assert task(items["dav"], sp["B"]) is None


def test_end_to_end_D_and_B_agree(w):
    sp, items = setup(w)
    w.clock.advance(minutes=3)
    w.reply(sp["E"], f"{w.code_for(items['swh'], sp['E'])} 5")
    w.clock.advance(minutes=2)
    w.reply(sp["D"], f"{w.code_for(items['dav'], sp['D'])} 4")
    w.reply(sp["B"], f"{w.code_for(items['dav'], sp['B'])} 5")
    assert items["dav"].outcome == "verified"
    assert "is filled" in w.outbox.to(sp["C"].id)[-1]
    paid = {(p.speaker_id, p.amount_kes) for p in w.db.scalars(select(Payout))}
    assert paid == {(sp["D"].id, 50), (sp["B"].id, 50)}


def test_customer_swahili_needs_no_swahili_review(w):
    sp = dict(B=w.speaker(langs=["swh", "dav"]))
    items = w.source(EN, {"swh": SW, "dav": DAV}, swahili_text="Nawa mikono")
    assert set(items) == {"dav"}
    w.engine.start_items(list(items.values()))
    t = task(items["dav"], sp["B"])
    assert t.shown_source_lang == "swh" and t.shown_source_text == "Nawa mikono"


def test_preferred_source_language_order(w):
    a = w.speaker(langs=["swh", "eng", "dav"])
    items = w.source(EN, {"dav": DAV}, swahili_text="Nawa mikono")
    w.engine.start_items(list(items.values()))
    assert task(items["dav"], a).shown_source_lang == "swh"


def test_machine_swahili_delivery_needs_review(w):
    s = w.settings
    assert s.needs_review_for("swh", customer_swahili=False)
    assert not s.needs_review_for("swh", customer_swahili=True)
    assert not s.needs_review_for("eng", customer_swahili=False)
