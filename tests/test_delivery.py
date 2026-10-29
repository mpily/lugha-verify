from hypothesis import given
from hypothesis import strategies as st

from app.delivery import choose_delivery_text
from tests.conftest import make_settings

LANGS = ["eng", "swh", "dav", "kln", "luo"]
STATUSES = [None, "machine", "speaker_corrected", "verified", "unknown", ""]
texts = st.one_of(st.none(), st.text(max_size=20))


@given(rl=st.sampled_from(LANGS), sl=st.sampled_from(["eng", "swh"]),
       fl=st.sampled_from(["eng", "swh"]), src=st.text(min_size=1, max_size=20),
       swh=texts, tt=texts, ts=st.sampled_from(STATUSES),
       spec=st.sampled_from(["*", "dav", "dav,kln,luo", "dav,swh"]))
def test_never_unverified_in_review_language(rl, sl, fl, src, swh, tt, ts, spec):
    s = make_settings(review_required_langs=spec, database_url="sqlite://")
    nr = s.needs_review(rl)
    c = choose_delivery_text(recipient_lang=rl, source_text=src, source_lang=sl,
                             fallback_lang=fl, swahili_text=swh, needs_review=nr,
                             translation_text=tt, translation_status=ts)
    allowed = {src, swh}
    if not nr or ts == "verified":
        allowed.add(tt)
    assert c.text in allowed
    if nr and ts != "verified" and c.lang == rl and rl not in (sl, "swh"):
        raise AssertionError("unverified text sent in review-required language")


def test_examples():
    kw = dict(source_text="Wash hands", source_lang="eng", fallback_lang="swh",
              swahili_text="Nawa mikono")
    c = choose_delivery_text(recipient_lang="dav", needs_review=True,
                             translation_text="X", translation_status="machine", **kw)
    assert (c.text, c.was_fallback) == ("Nawa mikono", True)
    c = choose_delivery_text(recipient_lang="dav", needs_review=True,
                             translation_text="X", translation_status="verified", **kw)
    assert (c.text, c.lang, c.was_fallback) == ("X", "dav", False)
    c = choose_delivery_text(recipient_lang="eng", needs_review=False,
                             translation_text=None, translation_status=None, **kw)
    assert c.text == "Wash hands" and not c.was_fallback
