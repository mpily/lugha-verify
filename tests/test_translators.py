import httpx

from app.translators.default_api import DefaultApiTranslator

OK = {"api_version": "1", "kitaita": "Choka yacha.", "kitaita_marked": "Choka [?aja].",
      "status": "machine", "pipeline_version": "git-abc"}


def make(responses):
    seq = iter(responses)
    calls = []

    def handler(req):
        calls.append(req)
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return httpx.Response(r[0], json=r[1])
    slept = []
    t = DefaultApiTranslator("https://space.test/api/translate", max_wait=30,
                             sleep=slept.append,
                             http=httpx.Client(transport=httpx.MockTransport(handler)))
    return t, calls, slept


def test_ok_uses_marked_text():
    t, calls, _ = make([(200, OK)])
    r = t.translate("Nyoka aja", "swh", "dav")
    assert r.text == "Choka [?aja]." and r.model_ref == "space:git-abc"
    assert calls[0].read() == b'{"text":"Nyoka aja","src":"swh","tgt":"dav"}'


def test_asleep_then_ok():
    t, calls, slept = make([httpx.ConnectError("x"), (503, {}), (200, OK)])
    assert t.translate("a", "swh", "dav").text == "Choka [?aja]."
    assert slept == [5.0, 10.0]


def test_gives_up_no_candidate():
    t, calls, slept = make([(503, {})] * 10)
    assert t.translate("a", "swh", "dav").text is None and sum(slept) >= 30


def test_unknown_and_unsupported():
    t, *_ = make([(200, {**OK, "kitaita": None, "status": "unknown"})])
    assert t.translate("a", "swh", "dav").text is None
    t, calls, _ = make([])
    assert t.translate("a", "eng", "dav").text is None and calls == []
