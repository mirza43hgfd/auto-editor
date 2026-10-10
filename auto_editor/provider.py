"""Centralized AI provider / key-ring layer.

One abstraction over text / image / TTS operations against the Gemini API.
Replaces the key-pool logic that used to be scattered across ai_helper.py.

Secrets: full key values are NEVER logged, printed, or embedded in exception
messages anywhere in this module. Use mask_key() ("****abcd") wherever a key
must be identified in a message or status readout.
"""

import json
import random
import socket
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

API_HOST = "https://generativelanguage.googleapis.com"

# ---------------------------------------------------------------------------
# QUOTA HONESTY — read before touching failover behaviour.
# ---------------------------------------------------------------------------
SHARED_QUOTA_NOTICE = (
    "Failover across keys is RESILIENCE against a revoked or broken key. "
    "Keys issued under the same Google Cloud project / AI Studio account "
    "SHARE a single quota pool: rotating keys does NOT multiply quota and "
    "must NEVER be used to evade rate limits or billing restrictions "
    "(Google ToS). This layer backs off, honors Retry-After, and pauses the "
    "job with a resume hint instead of hammering the API."
)

# Error taxonomy knobs
RETRY_AFTER_MAX_SLEEP = 120   # Retry-After longer than this -> pause job, don't sleep
BACKOFF_BASE = 4.0            # seconds; doubled per consecutive failure
BACKOFF_CAP = 60.0
MAX_TRANSIENT_RETRIES = 5     # bounded retries for 5xx / network errors
MAX_POST_ITERATIONS = 25      # hard bound so post() always terminates


class ProviderError(Exception):
    """Base for all provider-layer errors. Never carries a key value."""


class HTTPStatusError(ProviderError):
    """A failed HTTP attempt. retry_after is seconds (int) or None."""

    def __init__(self, code, detail="", retry_after=None):
        self.code = code
        self.detail = detail or ""
        self.retry_after = retry_after
        # NOTE: the wire format below is parsed back by
        # http_status_from_ai_error(); keep "HTTP_<code>" prefix stable.
        ra = " retry_after=%d" % int(retry_after) if retry_after else ""
        super().__init__("HTTP_%d%s%s" % (code, ra, (" " + self.detail[:120]) if self.detail else ""))


class AllKeysExhausted(ProviderError):
    """No eligible key right now. The job layer should PAUSE (not crash):
    progress is preserved and the job resumes when a key becomes eligible or
    the user intervenes.

    Carries a user-facing Roman Urdu message + resume hint.
    """

    def __init__(self, user_message, resume_hint, resume_at=None,
                 last_error_category="unknown"):
        self.user_message = user_message
        self.resume_hint = resume_hint
        self.resume_at = resume_at            # epoch seconds, or None
        self.last_error_category = last_error_category
        super().__init__(user_message)


def mask_key(key):
    """'AIza...wX9q' -> '****wX9q'. Safe to show in UI/logs."""
    k = key or ""
    return "****" + k[-4:] if len(k) >= 4 else "****"


def parse_retry_after(value, now=None):
    """Parse a Retry-After header value -> seconds (int) or None.

    Accepts delta-seconds ("120") or an HTTP date. Returns None when the
    value is missing, unparseable, or already in the past.
    """
    if value is None:
        return None
    v = str(value).strip()
    try:
        secs = int(float(v))
        return secs if secs >= 0 else None
    except (ValueError, TypeError):
        pass
    try:
        dt = parsedate_to_datetime(v)
        if dt is None:
            return None
        now = now if now is not None else time.time()
        secs = int((dt.timestamp() - now))
        return secs if secs >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def http_status_from_ai_error(err):
    """Rebuild an HTTPStatusError from an AIError("HTTP_<code> ...") message.

    Exists so ai_helper._post_pool can keep honoring a stubbed _post_single
    (tests) while the real taxonomy lives here. Unknown shapes -> code 0
    (treated as transient).
    """
    import re
    m = re.match(r"^HTTP_(\d{3})(?:\s+retry_after=(\d+))?\s*(.*)$",
                 str(err), re.DOTALL)
    if not m:
        return HTTPStatusError(0, str(err)[:120])
    code = int(m.group(1))
    ra = int(m.group(2)) if m.group(2) else None
    return HTTPStatusError(code, (m.group(3) or "")[:120], ra)


# ---------------------------------------------------------------------------
# Per-key record
# ---------------------------------------------------------------------------
class KeyRecord:
    """State for one configured key.

    Fields kept per the product brief: label, enabled/disabled,
    validation_state, last_success_at, last_error_category,
    rate_limited_until, retry_after_at, consecutive_failures.
    """

    def __init__(self, label, key, enabled=True, models=None, slot_index=None):
        self.label = label
        self._key = key                      # secret: access via peek() only
        self.enabled = enabled
        self.models = set(models) if models else None  # None = all models
        self.slot_index = slot_index         # settings_store slot, if any
        self.validation_state = "unknown"    # unknown | valid | invalid
        self.last_success_at = 0.0
        self.last_error_category = ""
        self.rate_limited_until = 0.0
        self.retry_after_at = 0.0
        self.consecutive_failures = 0

    def peek(self):
        """The raw key — for the Authorization header ONLY. Never log this."""
        return self._key

    def mask(self):
        return mask_key(self._key)

    def eligible(self, model, now):
        if not self.enabled:
            return False
        if self.validation_state == "invalid":
            return False
        if self.models is not None and model not in self.models:
            return False
        if self.rate_limited_until and self.rate_limited_until > now:
            return False
        return True

    # -- state transitions -------------------------------------------------
    def mark_success(self, now):
        self.validation_state = "valid"
        self.last_success_at = now
        self.last_error_category = ""
        self.consecutive_failures = 0
        self.rate_limited_until = 0.0
        self.retry_after_at = 0.0

    def mark_rate_limited(self, until, now, retry_after=None):
        self.last_error_category = "rate_limited"
        self.rate_limited_until = until
        self.retry_after_at = retry_after or 0.0
        self.consecutive_failures += 1

    def mark_invalid(self, now):
        self.validation_state = "invalid"
        self.last_error_category = "invalid_key"
        self.consecutive_failures += 1

    def mark_transient(self, now):
        self.last_error_category = "transient"
        self.consecutive_failures += 1


# ---------------------------------------------------------------------------
# Default network transport (urllib). Injectable for tests.
# ---------------------------------------------------------------------------
def default_transport(model, payload, api_key, timeout=90):
    """POST generateContent. Returns response dict.

    Raises HTTPStatusError (never leaks the key: detail is server text only).
    """
    url = "%s/v1beta/models/%s:generateContent" % (API_HOST, model)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise HTTPStatusError(e.code, detail,
                              parse_retry_after(e.headers.get("Retry-After")))
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            ConnectionError) as e:
        raise HTTPStatusError(0, "network: %s" % str(e)[:120])


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class Provider:
    """Owns key selection, backoff, failover and error taxonomy.

    keys: iterable of KeyRecord or dicts {label, key, enabled?, models?,
          slot_index?}. Empty key values are skipped.
    store: optional settings_store-like module used ONLY to persist
           limited/failed states (so restarts remember cooldowns).
    transport: (model, payload, key, timeout) -> dict, raising HTTPStatusError.
    now_fn / sleep_fn: injectable for deterministic tests.
    """

    def __init__(self, keys=(), store=None, transport=None, now_fn=None,
                 sleep_fn=None, jitter=True,
                 max_transient_retries=MAX_TRANSIENT_RETRIES):
        self._records = []
        for k in keys:
            if isinstance(k, KeyRecord):
                rec = k
            else:
                rec = KeyRecord(label=k.get("label", "key"),
                                key=k.get("key", ""),
                                enabled=k.get("enabled", True),
                                models=k.get("models"),
                                slot_index=k.get("slot_index"))
            if rec.peek():
                self._records.append(rec)
        self._store = store
        self._transport = transport or default_transport
        self._now = now_fn or time.time
        self._sleep = sleep_fn or time.sleep
        self._jitter = jitter
        self._max_transient = max_transient_retries

    @classmethod
    def from_settings(cls, store, **kw):
        """Build from settings_store key slots (labels 'Key 1'..'Key 7')."""
        recs = []
        for i, k in enumerate(store.get_api_key_slots()):
            if not k:
                continue
            rec = KeyRecord(label="Key %d" % (i + 1), key=k, slot_index=i)
            st = store.get_key_status(i)
            if st.get("status") == store.STATUS_FAILED:
                rec.validation_state = "invalid"
            cu = st.get("cooldown_until") or 0
            if cu:
                rec.rate_limited_until = cu
            recs.append(rec)
        return cls(recs, store=store, **kw)

    # -- selection ----------------------------------------------------------
    def eligible(self, model, now=None):
        """Eligible records, most-reliable first (fewest failures, then slot)."""
        now = now if now is not None else self._now()
        ok = [r for r in self._records if r.eligible(model, now)]
        ok.sort(key=lambda r: (r.consecutive_failures,
                               0 if r.last_success_at else 1,
                               r.slot_index if r.slot_index is not None else 999))
        return ok

    def _earliest_retry_at(self, now):
        cands = [r.rate_limited_until for r in self._records
                 if r.validation_state != "invalid"
                 and r.rate_limited_until and r.rate_limited_until > now]
        return min(cands) if cands else None

    # -- persistence ---------------------------------------------------------
    def _sync(self, rec):
        """Persist limited/failed/ok states back to settings_store (if any)."""
        st = self._store
        if st is None or rec.slot_index is None:
            return
        now = self._now()
        try:
            if rec.validation_state == "invalid":
                st.set_key_status(rec.slot_index, st.STATUS_FAILED)
            elif rec.rate_limited_until and rec.rate_limited_until > now:
                import math
                st.set_key_status(rec.slot_index, st.STATUS_LIMITED,
                                  math.ceil(rec.rate_limited_until - now))
            else:
                st.set_key_status(rec.slot_index, st.STATUS_OK)
        except Exception:
            pass  # persistence must never break a request

    # -- backoff --------------------------------------------------------------
    def _backoff(self, consecutive_failures):
        wait = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, consecutive_failures)))
        if self._jitter:
            wait *= random.uniform(0.75, 1.25)
        return wait

    # -- main entry ------------------------------------------------------------
    def post(self, model, payload, op="text", timeout=90):
        """POST via the key ring. Returns the response dict.

        Raises:
          ProviderError      — fail-fast errors (e.g. HTTP 400 bad request).
          AllKeysExhausted   — nothing eligible; pause the job, keep progress.
        """
        invalid_seen = False
        transient_retries = 0
        last_err = None

        for _ in range(MAX_POST_ITERATIONS):
            now = self._now()
            keys = self.eligible(model, now)
            if not keys:
                resume_at = self._earliest_retry_at(now)
                if resume_at and (resume_at - now) <= RETRY_AFTER_MAX_SLEEP:
                    self._sleep(max(0.0, resume_at - now))
                    continue
                raise self._exhausted(invalid_seen, last_err, resume_at)

            key = keys[0]
            try:
                resp = self._transport(model, payload, key.peek(), timeout)
            except HTTPStatusError as e:
                last_err = e
                code = e.code
                if code == 400:
                    # Bad request: another key / retry will NOT help. Fail fast.
                    key.last_error_category = "bad_request"
                    raise ProviderError(
                        "Bad request (HTTP 400): %s. "
                        "Request theek karo — dobara try karne se faida nahi."
                        % (e.detail[:150] or "server ne request reject kar di"))
                if code in (401, 403):
                    # Invalid / revoked credentials: NEVER retry this key.
                    key.mark_invalid(now)
                    self._sync(key)
                    invalid_seen = True
                    continue
                if code == 429:
                    wait = (e.retry_after if e.retry_after and e.retry_after > 0
                            else self._backoff(key.consecutive_failures))
                    key.mark_rate_limited(now + wait, now, e.retry_after or 0.0)
                    self._sync(key)
                    continue
                # 5xx or network (code 0): bounded exponential backoff + jitter,
                # retry the SAME key; move on only when the budget is spent.
                if code == 0 or 500 <= code < 600:
                    if transient_retries < self._max_transient:
                        transient_retries += 1
                        key.mark_transient(now)
                        self._sleep(self._backoff(key.consecutive_failures))
                        continue
                    key.mark_rate_limited(
                        now + self._backoff(key.consecutive_failures), now, 0.0)
                    self._sync(key)
                    transient_retries = 0
                    continue
                raise ProviderError("HTTP_%d: %s" % (code, e.detail[:150]))
            except Exception as e:  # transport bug / unexpected: don't loop forever
                raise ProviderError("Transport error: %s" % str(e)[:150])

            key.mark_success(now)
            self._sync(key)
            return resp

        # Unreachable in practice (loop always exits via return/raise), but
        # guarantees termination even if eligibility logic ever misbehaves.
        raise self._exhausted(invalid_seen, last_err, None)

    # -- exhaustion messages ----------------------------------------------------
    def _exhausted(self, invalid_seen, last_err, resume_at):
        if not self._records:
            return AllKeysExhausted(
                "Koi API key nahi mili. Settings me apni Gemini API key dalo, "
                "phir dobara try karo.",
                "Settings kholo → API key dalo → kaam wahin se resume hoga, "
                "progress mehfooz hai.",
                resume_at=None, last_error_category="no_keys")
        if invalid_seen and all(r.validation_state == "invalid"
                                for r in self._records if r.peek()):
            return AllKeysExhausted(
                "API key galat hai — Google ne reject kar di (401/403). "
                "Settings me sahi key dalo.",
                "Sahi key dalne ke baad kaam wahin se resume hoga; "
                "koi progress zaya nahi hogi.",
                resume_at=None, last_error_category="invalid_key")
        msg = ("Saari keys is waqt available nahi hain "
               "(rate limit / quota). Thori der baad khud retry hoga.")
        hint = ("Progress mehfooz hai — job pause hai, resume par wahin se "
                "shuru hogi. Quota khatam ho to kal try karo ya paid tier.")
        if resume_at:
            hint = ("Lagbhag %d second me retry hoga. Progress mehfooz hai."
                    % max(1, int(resume_at - self._now()))) + " " + hint
        cat = (last_err.code if isinstance(last_err, HTTPStatusError)
               else "unknown")
        return AllKeysExhausted(msg, hint, resume_at=resume_at,
                                last_error_category=str(cat))
