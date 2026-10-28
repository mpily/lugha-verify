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


FIXED = "Osha mikono kwa sabuni kabla ya kula chakula"


def retranslate(w, output):
    for item_id, sw, lang in w.engine.pending_retranslations():
        w.engine.apply_retranslation(item_id, sw, output, "fake")


def test_after_E_corrects_dav_is_retranslated_then_B_and_C_get_it(w):
    sp, items = setup(w)
    w.clock.advance(minutes=3)
    w.reply(sp["E"], f"{w.code_for(items['swh'], sp['E'])} 3 {FIXED}")
    dav = items["dav"]
    assert task(dav, sp["B"]) is None  # waits for the re-translation
    assert w.engine.pending_retranslations() == [(dav.id, FIXED, "dav")]
    w.clock.advance(minutes=31); w.engine.tick()
    assert task(dav, sp["B"]) is None  # no wave while re-translation pending
    retranslate(w, "Choka iyacha")
    assert dav.candidate.text == "Choka iyacha" and dav.retranslate_from is None
    for k in ("B", "C"):
        t = task(dav, sp[k])
        assert t is not None and t.shown_source_lang == "swh"
        assert t.shown_source_text == FIXED and t.shown_candidate_text == "Choka iyacha"
        assert f'SW: "{FIXED}"' in w.outbox.to(sp[k].id)[-1]
        assert 'Machine: "Choka iyacha"' in w.outbox.to(sp[k].id)[-1]
    assert task(dav, sp["D"]).shown_candidate_text == DAV  # D's task unchanged


def test_ratings_compare_the_candidate_each_speaker_saw(w):
    sp, items = setup(w)
    w.clock.advance(minutes=3)
    w.reply(sp["E"], f"{w.code_for(items['swh'], sp['E'])} 3 {FIXED}")
    retranslate(w, "Choka iyacha")
    w.clock.advance(minutes=2)
    w.reply(sp["D"], f"{w.code_for(items['dav'], sp['D'])} 5")   # accepts old
    w.reply(sp["B"], f"{w.code_for(items['dav'], sp['B'])} 5")   # accepts new
    assert items["dav"].outcome == "unverified"


def test_translator_gives_nothing_keeps_old_candidate(w):
    sp, items = setup(w)
    w.clock.advance(minutes=3)
    w.reply(sp["E"], f"{w.code_for(items['swh'], sp['E'])} 3 {FIXED}")
    retranslate(w, None)
    assert items["dav"].candidate.text == DAV
    assert task(items["dav"], sp["B"]) is not None


def test_uploaded_candidates_not_retranslated(w):
    sp = dict(B=w.speaker(langs=["swh", "dav"]), E=w.speaker(langs=["eng", "swh"]))
    items = w.source(EN, {"swh": SW, "dav": DAV})
    items["dav"].candidate.origin = "uploaded"
    w.engine.start_items(list(items.values()))
    w.clock.advance(minutes=3)
    w.reply(sp["E"], f"{w.code_for(items['swh'], sp['E'])} 3 {FIXED}")
    assert w.engine.pending_retranslations() == []
    assert task(items["dav"], sp["B"]) is not None


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
    assert w.engine.pending_retranslations() == []  # unchanged Swahili
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
