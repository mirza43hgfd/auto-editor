"""M2 provider/key-ring tests. ALL MOCKED — no network, no real API keys.

Covers: 429 backoff, Retry-After honoring, 401 invalidation, 400 fail-fast,
AllKeysExhausted pause semantics, failover resilience, shared-quota notice,
key masking, SQLite prompt cache, TTS batching, eligibility rules,
Retry-After parsing, DPAPI-backend reporting.

Run:  .venv/bin/python tests/test_provider.py
Exit 0 = all pass; non-zero = real failure.
"""

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from auto_editor import provider
from auto_editor import cache as cache_mod
from auto_editor import tts_batch
from auto_editor import ai_helper
from auto_editor import settings_store

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("PASS  %s" % name)
    else:
        failed += 1
        print("FAIL  %s  %s" % (name, detail))


# ---------------------------------------------------------------- fake clock
def make_clock(start=1000.0):
    now = [start]
    sleeps = []

    def now_fn():
        return now[0]

    def sleep_fn(s):
        sleeps.append(s)
        now[0] += s

    return now_fn, sleep_fn, sleeps


# ------------------------------------------------------- scripted transport
def make_transport(script):
    """script: list of ("ok", dict) | ("http", code, retry_after?)."""
    calls = []
    idx = [0]

    def t(model, payload, key, timeout=90):
        calls.append(key)
        if idx[0] >= len(script):
            raise AssertionError("transport called more often than scripted")
        item = script[idx[0]]
        idx[0] += 1
        if item[0] == "ok":
            return item[1]
        _, code = item[0], item[1]
        ra = item[2] if len(item) > 2 else None
        raise provider.HTTPStatusError(code, "scripted", ra)

    t.calls = calls
    return t


def make_provider(keys, script, **kw):
    now_fn, sleep_fn, sleeps = make_clock()
    kw.setdefault("jitter", False)
    p = provider.Provider(keys, transport=make_transport(script),
                          now_fn=now_fn, sleep_fn=sleep_fn, **kw)
    return p, p._transport, sleeps


# ------------------------------------------------- 1. 429 twice then success
p, t, sleeps = make_provider(
    [{"label": "K1", "key": "SECRETKEY-1"}],
    [("http", 429), ("http", 429), ("ok", {"r": 1})])
resp = p.post("gemini-3.5-flash", {"x": 1})
check("429x2->ok: response returned", resp == {"r": 1}, repr(resp))
check("429x2->ok: backoff sleeps [4.0, 8.0]",
      sleeps == [4.0, 8.0], repr(sleeps))
rec = p._records[0]
check("429x2->ok: failures reset after success",
      rec.consecutive_failures == 0 and rec.last_error_category == "",
      repr(rec.__dict__))

# ------------------------------------------------- 2. Retry-After honored
p, t, sleeps = make_provider(
    [{"label": "K1", "key": "SECRETKEY-1"}],
    [("http", 429, 30), ("ok", {"r": 2})])
resp = p.post("gemini-3.5-flash", {"x": 1})
check("Retry-After: honored with 30s sleep",
      resp == {"r": 2} and sleeps == [30], repr((resp, sleeps)))

# ------------------------------------------------- 3. 401 marks invalid, never retried
p, t, sleeps = make_provider(
    [{"label": "K1", "key": "SECRETKEY-1"}],
    [("http", 401)])
try:
    p.post("gemini-3.5-flash", {"x": 1})
    check("401: raises AllKeysExhausted", False, "no exception")
except provider.AllKeysExhausted as e:
    check("401: raises AllKeysExhausted", True)
    check("401: 'key galat hai' surfaced", "galat" in e.user_message, e.user_message)
    check("401: resume hint present", bool(e.resume_hint), e.resume_hint)
check("401: key marked invalid", p._records[0].validation_state == "invalid")
check("401: never retried (1 call)", len(t.calls) == 1, repr(t.calls))
check("401: mask() never exposes the raw key",
      p._records[0].mask() == "****EY-1"
      and "SECRETKEY-1" not in p._records[0].mask())
# the real leak check: exception text must not contain the raw key
try:
    p2, t2, _ = make_provider([{"label": "K1", "key": "SUPERSECRETKEY99"}],
                              [("http", 401)])
    p2.post("m", {})
except provider.AllKeysExhausted as e2:
    check("401: raw key not in exception text",
          "SUPERSECRETKEY99" not in (e2.user_message + e2.resume_hint + str(e2)))

# ------------------------------------------------- 4. 400 fails fast
p, t, sleeps = make_provider(
    [{"label": "K1", "key": "SECRETKEY-1"}],
    [("http", 400)])
try:
    p.post("gemini-3.5-flash", {"x": 1})
    check("400: raises ProviderError", False, "no exception")
except provider.AllKeysExhausted:
    check("400: raises ProviderError (not Exhausted)", False, "wrong type")
except provider.ProviderError as e:
    check("400: raises ProviderError (fail fast)", True)
    check("400: no retry (1 call)", len(t.calls) == 1, repr(t.calls))

# ------------------------------------------------- 5. all keys exhausted (long cooldowns)
p, t, sleeps = make_provider(
    [{"label": "K1", "key": "K1"}, {"label": "K2", "key": "K2"}],
    [("http", 429, 600), ("http", 429, 600)])
try:
    p.post("gemini-3.5-flash", {"x": 1})
    check("exhausted: raises", False, "no exception")
except provider.AllKeysExhausted as e:
    check("exhausted: raises AllKeysExhausted", True)
    check("exhausted: resume_at set", e.resume_at == 1600.0, repr(e.resume_at))
    check("exhausted: Roman Urdu message", bool(e.user_message))
    check("exhausted: resume hint mentions progress saved",
          "mehfooz" in e.resume_hint, e.resume_hint)
    check("exhausted: no sleep for 600s window (pause instead)",
          sleeps == [], repr(sleeps))
check("exhausted: both keys tried once", len(t.calls) == 2, repr(t.calls))

# ------------------------------------------------- 6. failover resilience (not quota multiplication)
p, t, sleeps = make_provider(
    [{"label": "K1", "key": "K1"}, {"label": "K2", "key": "K2"}],
    [("http", 429, 600), ("ok", {"win": "K2"})])
resp = p.post("gemini-3.5-flash", {"x": 1})
check("failover: second key wins", resp == {"win": "K2"}, repr(resp))
k1 = p._records[0]
check("failover: K1 marked rate-limited, not eligible now",
      k1.last_error_category == "rate_limited" and not k1.eligible("m", 1000.0))

# ------------------------------------------------- 7. eligibility rules
p, _, _ = make_provider(
    [{"label": "off", "key": "A", "enabled": False},
     {"label": "img-only", "key": "B", "models": {"image-model"}},
     {"label": "bad", "key": "C"},
     {"label": "good", "key": "D"}],
    [])
p._records[2].validation_state = "invalid"
elig = [r.label for r in p.eligible("gemini-3.5-flash", 1000.0)]
check("eligibility: disabled/model-mismatch/invalid skipped",
      elig == ["good"], repr(elig))

# ------------------------------------------------- 8. shared-quota notice present
check("shared-quota notice present",
      "SHARE" in provider.SHARED_QUOTA_NOTICE)
check("shared-quota notice forbids evasion",
      "never" in provider.SHARED_QUOTA_NOTICE.lower()
      and "ToS" in provider.SHARED_QUOTA_NOTICE)

# ------------------------------------------------- 9. mask_key
check("mask_key format", provider.mask_key("AIzaSyDUMMYKEYwX9q") == "****wX9q")
check("mask_key short", provider.mask_key("ab") == "****")

# ------------------------------------------------- 10. Retry-After parsing
check("parse Retry-After seconds", provider.parse_retry_after("120") == 120)
check("parse Retry-After garbage", provider.parse_retry_after("soon") is None)
check("parse Retry-After None", provider.parse_retry_after(None) is None)
future = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                       time.gmtime(time.time() + 3600))
ra = provider.parse_retry_after(future)
check("parse Retry-After HTTP date", ra is not None and 3500 <= ra <= 3600, repr(ra))
past = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                     time.gmtime(time.time() - 3600))
check("parse Retry-After past date -> None",
      provider.parse_retry_after(past) is None)

# ------------------------------------------------- 11. AIError round-trip
e = provider.http_status_from_ai_error(ai_helper.AIError("HTTP_429 retry_after=45 quota"))
check("AIError->HTTPStatusError round-trip",
      isinstance(e, provider.HTTPStatusError) and e.code == 429
      and e.retry_after == 45, repr(e.__dict__))
e2 = provider.http_status_from_ai_error(ai_helper.AIError("boom"))
check("AIError unknown -> code 0 (transient)", e2.code == 0)

# ------------------------------------------------- 12. SQLite prompt cache
with tempfile.TemporaryDirectory() as td:
    c = cache_mod.PromptCache(os.path.join(td, "c.sqlite3"))
    check("cache: miss -> None", c.get_text("m", "hello") is None)
    c.put_text("m", "hello", "world")
    check("cache: text round-trip", c.get_text("m", "hello") == "world")
    check("cache: hit counted", c.stats()["hits"] == 1, repr(c.stats()))
    check("cache: different prompt misses",
          c.get_text("m", "hello!") is None)
    check("cache: different model misses",
          c.get_text("m2", "hello") is None)
    check("cache: params matter",
          c.get_text("m", "hello", {"system": "s"}) is None)
    c.put_bytes("im", "pic", b"\x89PNG...")
    check("cache: bytes round-trip",
          c.get_bytes("im", "pic") == b"\x89PNG...")
    check("cache: kind mismatch -> None",
          c.get_text("im", "pic") is None)
    # media mtime/size sensitivity
    f = os.path.join(td, "ref.png")
    with open(f, "wb") as fh:
        fh.write(b"v1")
    c.put_text("m", "p", "t1", media_paths=[f])
    check("cache: media hit", c.get_text("m", "p", media_paths=[f]) == "t1")
    with open(f, "wb") as fh:
        fh.write(b"v1-changed!!")
    check("cache: changed media misses",
          c.get_text("m", "p", media_paths=[f]) is None)
    st = c.stats()
    check("cache: saves counted", st["saves"] >= 3, repr(st))
    check("cache: entries>0", st["entries"] > 0, repr(st))
    c.close()

# ------------------------------------------------- 13. ai_helper cache integration (mocked)
with tempfile.TemporaryDirectory() as td:
    ai_helper.configure_cache(os.path.join(td, "ai.sqlite3"))
    real_pool = ai_helper._post_model_pool
    calls = []

    def fake_pool(model, payload, api_key=None, timeout=90):
        calls.append(model)
        return {"candidates": [{"content": {"parts": [{"text": "cached-reply"}]}}]}

    ai_helper._post_model_pool = fake_pool
    try:
        r1 = ai_helper.call_gemini("unique-cache-probe-xyz")
        r2 = ai_helper.call_gemini("unique-cache-probe-xyz")
        check("ai_helper cache: 2nd identical call hits cache",
              r1 == r2 == "cached-reply" and len(calls) == 1, repr((r1, r2, calls)))
        r3 = ai_helper.call_gemini("unique-cache-probe-xyz",
                                   use_cache=False)
        check("ai_helper cache: use_cache=False bypasses",
              r3 == "cached-reply" and len(calls) == 2, repr(calls))
    finally:
        ai_helper._post_model_pool = real_pool

# ------------------------------------------------- 14. TTS batching
check("tts_batch: merges shorts",
      tts_batch.merge_chunks(["aaa", "bb", "c"], max_chars=10) == ["aaa\n\nbb\n\nc"])
check("tts_batch: oversized passes through",
      tts_batch.merge_chunks(["x" * 2000, "y"], max_chars=100) == ["x" * 2000, "y"])
check("tts_batch: empties dropped",
      tts_batch.merge_chunks(["a", "", "   ", "b"], max_chars=100) == ["a\n\nb"])
check("tts_batch: empty in -> empty out",
      tts_batch.merge_chunks([]) == [])
st = tts_batch.batch_stats(["a", "b", "c"], ["a\n\nb\n\nc"])
check("tts_batch: stats", st == {"chunks_before": 3, "chunks_after": 1,
                                 "calls_saved": 2}, repr(st))

# ------------------------------------------------- 15. DPAPI backend reporting (Linux: obfuscated)
with tempfile.TemporaryDirectory() as cfg:
    real = settings_store._config_dir
    settings_store._config_dir = lambda: cfg
    try:
        check("dpapi_available() False on Linux",
              settings_store.dpapi_available() is False)
        check("key_storage_backend() honest label",
              settings_store.key_storage_backend() == "obfuscated")
        settings_store.set_api_key_slots(["DUMMY-KEY-1"])
        check("key round-trip (obfuscated store)",
              settings_store.get_api_key_slots()[0] == "DUMMY-KEY-1")
        raw = open(settings_store._config_path()).read()
        check("key not plaintext in config",
              "DUMMY-KEY-1" not in raw)
    finally:
        settings_store._config_dir = real

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
