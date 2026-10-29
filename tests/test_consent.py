"""Only share_consent=true tasks leave the instance, with no identity."""
from hypothesis import given, settings
from hypothesis import strategies as st

from app.exports import SHARED_FIELDS, mark_shared, pending_shared
from tests.conftest import SALT, World


@settings(max_examples=25, deadline=None)
@given(shares=st.lists(st.booleans(), min_size=2, max_size=5))
def test_shared_export_only_consented(shares):
    w = World()
    ss = [w.speaker() for _ in shares]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    for sp, share in zip(ss, shares):
        w.reply(sp, f"{w.code_for(it, sp)} 3 Choka iyacha" + (" SHARE" if share else ""))
    w.clock.advance(minutes=200); w.engine.tick()
    ids, recs = pending_shared(w.db, SALT)
    consented = {t.id for t in it.tasks if t.share_consent and t.replied_at}
    assert set(ids) == consented
    assert sum(shares) == len(consented)
    for r in recs:
        assert set(r) == set(SHARED_FIELDS)
        assert not any(k in r for k in ("speaker_id", "phone", "campaign_id", "task_id"))
    mark_shared(w.db, ids, w.clock())
    assert pending_shared(w.db, SALT) == ([], [])


def test_open_item_not_shared_yet(w):
    ss = [w.speaker() for _ in range(3)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5 SHARE")
    assert pending_shared(w.db, SALT) == ([], [])
