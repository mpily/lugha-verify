"""State machine scenarios (CLAUDE.md §12) on a fake clock."""
from sqlalchemy import select

from app.models import Payout, Translation


def payouts(w):
    return {(p.speaker_id, p.amount_kes) for p in w.db.scalars(select(Payout))}


def translation(w, item):
    return w.db.scalar(select(Translation).where(
        Translation.source_id == item.candidate.source_id))


def test_trusted_replies_in_time(w):
    tr = w.speaker(trusted=["dav"]); other = w.speaker()
    it = w.item(); w.engine.start_item(it)
    assert it.state == "trusted" and len(it.tasks) == 1 and w.outbox.to(other.id) == []
    w.clock.advance(minutes=5)
    w.reply(tr, f"{w.code_for(it, tr)} 3 Choka iyacha")
    assert it.state == "closed" and it.outcome == "verified"
    assert translation(w, it).final_text == "Choka iyacha"
    assert translation(w, it).status == "verified"
    assert payouts(w) == {(tr.id, 100)}


def test_trusted_accepts_unchanged(w):
    tr = w.speaker(trusted=["dav"])
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=1)
    w.reply(tr, f"{w.code_for(it, tr)} 5")
    assert translation(w, it).final_text == "Choka yacha" and it.outcome == "verified"


def test_trusted_timeout_then_fanout(w):
    tr = w.speaker(trusted=["dav"]); ss = [w.speaker() for _ in range(7)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=14); w.engine.tick()
    assert it.state == "trusted"
    w.clock.advance(minutes=1); w.engine.tick()
    assert it.state == "fanout"
    wave1 = [t for t in it.tasks if t.wave == 1]
    assert len(wave1) == 5 and tr.id not in {t.speaker_id for t in wave1}


def test_first_two_agree(w):
    ss = [w.speaker() for _ in range(5)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(ss[3], f"{w.code_for(it, ss[3])} 3 Choka iyacha")
    assert it.state == "fanout"
    w.reply(ss[1], f"{w.code_for(it, ss[1])} 4 choka iyacha.")
    assert it.state == "closed" and it.outcome == "verified"
    assert payouts(w) == {(ss[3].id, 50), (ss[1].id, 50)}
    for s in (ss[0], ss[2], ss[4]):
        assert any("is filled" in m for m in w.outbox.to(s.id))
    assert translation(w, it).status == "verified"


def test_both_accept_candidate(w):
    ss = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5")
    w.reply(ss[1], f"{w.code_for(it, ss[1])} 4")
    assert it.outcome == "verified" and translation(w, it).final_text == "Choka yacha"


def test_first_two_disagree(w):
    ss = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5")
    w.reply(ss[1], f"{w.code_for(it, ss[1])} 2 Choka iyacha")
    assert it.outcome == "unverified"
    assert translation(w, it).status == "speaker_corrected"
    assert payouts(w) == {(ss[0].id, 50), (ss[1].id, 50)}


def test_one_reply_only(w):
    ss = [w.speaker() for _ in range(2)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=3)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 4")
    w.clock.advance(minutes=200); w.engine.tick()
    assert it.state == "closed" and it.outcome == "unverified"
    assert payouts(w) == {(ss[0].id, 50)}
    assert translation(w, it).status == "speaker_corrected"


def test_no_replies(w):
    ss = [w.speaker() for _ in range(2)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=121); w.engine.tick()
    assert it.outcome == "no_reply" and payouts(w) == set()
    assert translation(w, it).status == "machine"
    assert any("closed" in m for m in w.outbox.to(ss[0].id))


def test_trusted_replies_late_during_stage2(w):
    tr = w.speaker(trusted=["dav"]); ss = [w.speaker() for _ in range(5)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=16); w.engine.tick()
    w.clock.advance(minutes=2)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 3 Choka iyacha")
    assert it.state == "fanout"
    w.reply(tr, f"{w.code_for(it, tr)} 3 Choka iyacha")
    assert it.outcome == "verified"
    assert payouts(w) == {(ss[0].id, 50), (tr.id, 50)}  # not a trusted override


def test_trusted_reply_due_before_tick_counts_as_stage2(w):
    """Timeout passed but the scheduler has not ticked yet."""
    tr = w.speaker(trusted=["dav"]); ss = [w.speaker() for _ in range(5)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=20)
    w.reply(tr, f"{w.code_for(it, tr)} 5")
    assert it.state == "fanout" and payouts(w) == set()


def test_third_reply_after_close_is_late_unpaid_notified(w):
    ss = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5")
    w.reply(ss[1], f"{w.code_for(it, ss[1])} 5")
    w.reply(ss[2], f"{w.code_for(it, ss[2])} 2 Choka iyacha SHARE")
    t3 = next(t for t in it.tasks if t.speaker_id == ss[2].id)
    assert t3.status == "late" and t3.corrected_text == "Choka iyacha" and t3.share_consent
    assert ss[2].id not in {sid for sid, _ in payouts(w)}
    assert "not paid" in w.outbox.to(ss[2].id)[-1]


def test_wave_exhaustion(w):
    ss = [w.speaker() for _ in range(6)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=30); w.engine.tick()
    assert {t.wave for t in it.tasks} == {1, 2} and len(it.tasks) == 6
    w.clock.advance(minutes=30); w.engine.tick()
    assert len(it.tasks) == 6  # pool exhausted, nobody re-tasked
    assert "expired" in w.outbox.to(ss[0].id)[-1]


def test_next_wave_only_when_needed(w):
    ss = [w.speaker() for _ in range(10)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=5)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5")
    w.clock.advance(minutes=26); w.engine.tick()
    assert len([t for t in it.tasks if t.wave == 2]) == 5  # still < 2 replies


def test_speaker_opts_out_mid_task(w):
    ss = [w.speaker() for _ in range(6)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(ss[0], "STOP")
    assert ss[0].opted_out_at is not None
    t0 = next(t for t in it.tasks if t.speaker_id == ss[0].id)
    assert t0.status == "expired"
    n_before = len(w.outbox.to(ss[0].id))
    w.reply(ss[1], f"{w.code_for(it, ss[1])} 5")
    w.reply(ss[2], f"{w.code_for(it, ss[2])} 5")
    assert it.state == "closed"
    assert len(w.outbox.to(ss[0].id)) == n_before  # nothing sent after STOP


def test_opted_out_trusted_skips_to_fanout(w):
    tr = w.speaker(trusted=["dav"]); ss = [w.speaker() for _ in range(2)]
    it = w.item(); w.engine.start_item(it)
    w.reply(tr, "STOP")
    assert it.state == "fanout"


def test_too_fast_reply_invalid(w):
    ss = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(seconds=4)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5")
    t = next(t for t in it.tasks if t.speaker_id == ss[0].id)
    assert t.status == "invalid" and "too fast" in w.outbox.to(ss[0].id)[-1]


def test_short_sentence_fast_reply_ok(w):
    ss = [w.speaker() for _ in range(3)]
    it = w.item(source="Snake comes"); w.engine.start_item(it)
    w.clock.advance(seconds=4)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5")
    assert next(t for t in it.tasks if t.speaker_id == ss[0].id).status == "replied"


def test_unknown_and_someone_elses_code(w):
    a, b = w.speaker(), w.speaker()
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(a, "ZZZZ 5")
    assert "no open task ZZZZ" in w.outbox.to(a.id)[-1]
    w.reply(a, f"{w.code_for(it, b)} 5")
    assert "no open task" in w.outbox.to(a.id)[-1]
    assert next(t for t in it.tasks if t.speaker_id == b.id).replied_at is None


def test_duplicate_reply_and_payout_idempotent(w):
    ss = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    c0 = w.code_for(it, ss[0])
    w.reply(ss[0], f"{c0} 5"); w.reply(ss[0], f"{c0} 5")
    assert it.state == "fanout"  # duplicate did not count as the 2nd reply
    w.reply(ss[1], f"{w.code_for(it, ss[1])} 5"); w.reply(ss[1], f"{w.code_for(it, ss[1])} 5")
    assert len(list(w.db.scalars(select(Payout)))) == 2


def test_parse_error_answered_once(w):
    a = w.speaker()
    w.reply(a, "hello?"); w.reply(a, "hello??")
    assert len(w.outbox.to(a.id)) == 1


def test_no_candidate_needs_text(w):
    ss = [w.speaker() for _ in range(2)]
    it = w.item(candidate=None); w.engine.start_item(it)
    assert "Translate into" in w.outbox.to(ss[0].id)[0]
    w.clock.advance(minutes=2)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5")
    assert next(t for t in it.tasks if t.speaker_id == ss[0].id).replied_at is None
    w.reply(ss[0], f"{w.code_for(it, ss[0])} Choka yacha")
    w.reply(ss[1], f"{w.code_for(it, ss[1])} choka yacha")
    assert it.outcome == "verified" and translation(w, it).final_text == "Choka yacha"


def test_priority_customer_first_and_accept_rate(w):
    net = [w.speaker(source="network") for _ in range(5)]
    bad = w.speaker(accept=1, reject=9)
    cust = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    ids = {t.speaker_id for t in it.tasks}
    assert {c.id for c in cust} <= ids and bad.id not in ids and len(ids) == 5


def test_not_opted_in_gets_no_task(w):
    a = w.speaker(opted_in=False); b = w.speaker()
    it = w.item(); w.engine.start_item(it)
    assert {t.speaker_id for t in it.tasks} == {b.id}
    w.reply(a, "YES")
    w.clock.advance(minutes=31); w.engine.tick()
    assert a.id in {t.speaker_id for t in it.tasks}


def test_translation_memory_reuses_verified(w):
    ss = [w.speaker() for _ in range(2)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5"); w.reply(ss[1], f"{w.code_for(it, ss[1])} 5")
    n_msgs = len(w.outbox.sent)
    it2 = w.item(); w.engine.start_item(it2)
    assert it2.outcome == "verified" and it2.tasks == []
    assert len(w.outbox.sent) == n_msgs


def test_budget_never_exceeded(w):
    tr = w.speaker(trusted=["dav"]); ss = [w.speaker() for _ in range(9)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=16); w.engine.tick()
    w.clock.advance(minutes=2)
    for s in [tr] + ss[:5]:
        w.reply(s, f"{w.code_for(it, s)} 5")
    assert sum(p.amount_kes for p in w.db.scalars(select(Payout))) <= 100


def test_expired_task_reply_not_paid_not_counted(w):
    ss = [w.speaker() for _ in range(6)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=31); w.engine.tick()
    assert "expired" in w.outbox.to(ss[0].id)[-1]
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5")
    t0 = next(t for t in it.tasks if t.speaker_id == ss[0].id)
    assert t0.status == "late" and t0.stars == 5  # stored
    assert "not paid" in w.outbox.to(ss[0].id)[-1]
    w.reply(ss[5], f"{w.code_for(it, ss[5])} 5")
    assert it.state == "fanout" and payouts(w) == set()


def test_expired_before_tick_still_unpaid(w):
    ss = [w.speaker() for _ in range(2)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=45)  # scheduler has not ticked
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5")
    assert next(t for t in it.tasks if t.speaker_id == ss[0].id).status == "late"


def test_low_stars_without_correction_unpaid_then_correction_paid(w):
    ss = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    c0 = w.code_for(it, ss[0])
    w.reply(ss[0], f"{c0} 2")
    t0 = next(t for t in it.tasks if t.speaker_id == ss[0].id)
    assert t0.status == "no_correction" and t0.stars == 2
    w.reply(ss[1], f"{w.code_for(it, ss[1])} 2 Choka iyacha")
    assert it.state == "fanout"  # the bare rating did not count
    w.reply(ss[0], f"{c0} 2 Choka iyacha")
    assert it.outcome == "verified"
    assert payouts(w) == {(ss[0].id, 50), (ss[1].id, 50)}


def test_trusted_low_stars_without_correction_goes_to_fanout(w):
    tr = w.speaker(trusted=["dav"]); ss = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(tr, f"{w.code_for(it, tr)} 1")
    assert it.state == "fanout" and payouts(w) == set()
    assert len([t for t in it.tasks if t.wave == 1]) == 3


def test_no_correction_task_notified_on_close(w):
    ss = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 1")
    w.reply(ss[1], f"{w.code_for(it, ss[1])} 5"); w.reply(ss[2], f"{w.code_for(it, ss[2])} 5")
    assert "is filled" in w.outbox.to(ss[0].id)[-1]
