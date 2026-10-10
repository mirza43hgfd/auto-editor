"""Pipeline-stage job persistence (M7): pause / resume / crash recovery.

Qt-free: safe on worker threads and in headless tests.

One JobStore per project work dir; manifest at <work_dir>/job.json::

    {"version": 1,
     "job_status": "idle|running|paused|failed|done",
     "pause_reason": str, "pause_hint": str,
     "stages": {name: {"status": "pending|running|paused|done|failed",
                       "updated_at": epoch,
                       "checkpoint": {...},
                       "error": {"category","message","what_to_do",
                                 "retryable","at"} | None}},
     "created_at": epoch, "updated_at": epoch}

Checkpoint conventions (per completed unit, never whole re-runs):
  voiceover: {"done_units": ["chunk_000", ...], "total": N,
              "mp3": path, "chunks": [{"text","start","end"}]}
  photos:    {"done_units": ["slot_001", ...], "total": N,
              "units": {"slot_001": {"path":..., "prompt":...}}}
  reference: {"path": ...}
  slots:     {"slots": [...]}

Error handling rule (product brief §9): retryable errors PAUSE the job
(progress kept, resume continues where it stopped); permanent errors FAIL
the stage with a clear Roman Urdu message + what-to-do. The whole workflow
is NEVER restarted because one scene failed.
"""

import json
import os
import re
import tempfile
import threading
import time

MANIFEST_NAME = "job.json"
SCHEMA_VERSION = 1

# Stage order for the script-first pipeline (matches pipeline_panel.STAGES).
STAGES = ["voiceover", "slots", "reference", "photos", "assemble", "render"]

# Same staleness horizon as render_worker._render_lock: a "running" job older
# than this almost certainly died with the process -> offer resume as crashed.
STALE_AFTER = 21600  # 6 hours

_PENDING = "pending"
_RUNNING = "running"
_PAUSED = "paused"
_DONE = "done"
_FAILED = "failed"

# ---------------------------------------------------------------------------
# Pause signal
# ---------------------------------------------------------------------------

class JobPaused(Exception):
    """Raised when a job pauses (rate limit / user request / no keys).

    NOT a failure: progress is in the manifest, resume continues from the
    next pending unit. str() is prefixed with "PAUSED: " so even
    string-based handlers can tell it apart from a failure.
    """

    def __init__(self, user_message, resume_hint=""):
        self.user_message = user_message or "Pause ho gaya."
        self.resume_hint = resume_hint or ""
        super(JobPaused, self).__init__("PAUSED: " + self.user_message)


# ---------------------------------------------------------------------------
# Error classification: retryable (pause) vs permanent (fail)
# ---------------------------------------------------------------------------

# Distinctive substrings of provider.AllKeysExhausted user messages
# (ai_helper._post_pool converts them to AIError carrying these texts).
_PAUSE_RATE_MSG = "Saari keys is waqt available nahi"
_PAUSE_NOKEY_MSG = "Koi API key nahi mili"
_FAIL_BADKEY_MSG = "API key galat hai"

_MSG_RATE = ("Limit lag gayi (rate limit / quota). "
             "Thori der me khud theek ho jayega.")
_WTD_QUOTA = ("Job pause hai — progress mehfooz hai. Resume dabao; "
              "quota khatam ho to kal try karo ya paid tier.")
_MSG_BADKEY = "API key galat hai — Google ne reject kar di (401/403)."
_WTD_KEY = "Settings me sahi Gemini API key dalo, phir Resume dabao."
_MSG_NOKEYS = "Koi API key nahi mili."
_MSG_BADREQ = ("Request me masla hai (HTTP 400) — "
               "dobara try karne se theek nahi hoga.")
_WTD_BADREQ = "Input/script check karo, phir stage dobara chalao."
_MSG_TRANS = "Network ya server ka aarzi masla."
_WTD_RESUME = "Resume dabao — kaam wahin se shuru hoga, progress mehfooz hai."


def _info(category, retryable, message, what_to_do):
    return {"category": category, "retryable": retryable,
            "message": message, "what_to_do": what_to_do}


def classify_error(err):
    """Classify an error -> dict(category, retryable, message, what_to_do).

    Categories: paused | rate_limit | no_keys | invalid_key | bad_request |
    transient | unknown. retryable=True means the job should PAUSE (not
    fail); the caller resumes from the manifest when conditions improve.

    Detection order: explicit JobPaused/paused-attribute first, then the
    AllKeysExhausted message markers (ai_helper stringifies them), then
    HTTP_<code> markers, then network keywords. Unknown -> retryable
    (bounded retries already happened at the provider layer).
    """
    if isinstance(err, JobPaused):
        return _info("paused", True, err.user_message,
                     err.resume_hint or _WTD_RESUME)
    if getattr(err, "paused", False):
        # Attached by ai_helper._post_pool on AllKeysExhausted.
        return _info("rate_limit", True, str(err) or _MSG_RATE,
                     getattr(err, "resume_hint", "") or _WTD_QUOTA)

    msg = str(err) or "Namaloom error."
    if _PAUSE_RATE_MSG in msg:
        return _info("rate_limit", True, _MSG_RATE, _WTD_QUOTA)
    if _PAUSE_NOKEY_MSG in msg:
        return _info("no_keys", True, _MSG_NOKEYS, _WTD_KEY)
    if _FAIL_BADKEY_MSG in msg:
        return _info("invalid_key", False, _MSG_BADKEY, _WTD_KEY)

    m = re.search(r"HTTP_(\d{3})", msg)
    if m:
        code = int(m.group(1))
        if code == 400:
            return _info("bad_request", False, _MSG_BADREQ, _WTD_BADREQ)
        if code in (401, 403):
            return _info("invalid_key", False, _MSG_BADKEY, _WTD_KEY)
        if code == 429:
            return _info("rate_limit", True, _MSG_RATE, _WTD_QUOTA)
        if code == 0 or 500 <= code < 600:
            return _info("transient", True, _MSG_TRANS, _WTD_RESUME)
        return _info("unknown", True, msg[:300], _WTD_RESUME)

    low = msg.lower()
    if "network:" in low or "timed out" in low or "timeout" in low \
            or "connection" in low:
        return _info("transient", True, _MSG_TRANS, _WTD_RESUME)
    return _info("unknown", True, msg[:300], _WTD_RESUME)


# ---------------------------------------------------------------------------
# JobStore
# ---------------------------------------------------------------------------

def _now():
    return time.time()


def _blank_stage():
    return {"status": _PENDING, "updated_at": 0.0,
            "checkpoint": {}, "error": None}


class JobStore:
    """Per-project job manifest. All mutations are atomic (tmp + rename)."""

    def __init__(self, work_dir):
        self.work_dir = os.path.abspath(work_dir)
        os.makedirs(self.work_dir, exist_ok=True)
        self.path = os.path.join(self.work_dir, MANIFEST_NAME)
        self._lock = threading.Lock()
        self._data = self._load()

    # -- persistence ------------------------------------------------------
    def _load(self):
        data = {"version": SCHEMA_VERSION, "job_status": "idle",
                "pause_reason": "", "pause_hint": "",
                "stages": {}, "created_at": _now(), "updated_at": _now()}
        raw = None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            for k, v in data.items():
                if k != "stages":
                    data[k] = raw.get(k, v)
        saved = (raw.get("stages") or {}) if isinstance(raw, dict) else {}
        for name in STAGES:
            st = _blank_stage()
            if isinstance(saved.get(name), dict):
                st.update(saved[name])
            data["stages"][name] = st
        return data

    def _save(self):
        self._data["updated_at"] = _now()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)  # atomic on POSIX and Windows

    # -- stage lifecycle ---------------------------------------------------
    def _stage(self, name):
        return self._data["stages"][name]

    def _overall_status(self):
        """Single source of truth for job_status.

        render=pending means "pipeline finished, render not started yet"
        (render is user-triggered), so it never blocks "done".
        """
        sts = self._data["stages"]
        if any(s["status"] == _FAILED for s in sts.values()):
            return _FAILED
        if any(s["status"] == _PAUSED for s in sts.values()):
            return _PAUSED
        if any(s["status"] == _RUNNING for s in sts.values()):
            return _RUNNING
        core = [s for n, s in sts.items() if n != "render"]
        if all(s["status"] == _DONE for s in core):
            return _DONE
        if all(s["status"] == _PENDING for s in sts.values()):
            return "idle"
        return _RUNNING  # resumable: some stages pending, none active

    def stage_status(self, name):
        with self._lock:
            return self._stage(name)["status"]

    def is_stage_done(self, name):
        return self.stage_status(name) == _DONE

    def start_stage(self, name):
        """Mark running. Checkpoint data is PRESERVED (resume keeps units)."""
        with self._lock:
            st = self._stage(name)
            st["status"] = _RUNNING
            st["updated_at"] = _now()
            st["error"] = None
            self._data["job_status"] = self._overall_status()
            self._save()

    def complete_stage(self, name):
        with self._lock:
            st = self._stage(name)
            st["status"] = _DONE
            st["updated_at"] = _now()
            st["error"] = None
            self._data["job_status"] = self._overall_status()
            self._save()

    def reset_stage(self, name):
        """Back to pending (Regenerate). Drops this stage's checkpoint."""
        with self._lock:
            self._data["stages"][name] = _blank_stage()
            self._data["job_status"] = self._overall_status()
            self._save()

    def checkpoint(self, name, data):
        """Merge data into the stage checkpoint (units accumulate)."""
        with self._lock:
            cp = self._stage(name)["checkpoint"]
            for k, v in (data or {}).items():
                cp[k] = v
            self._stage(name)["updated_at"] = _now()
            self._save()

    def checkpoint_of(self, name):
        with self._lock:
            return dict(self._stage(name)["checkpoint"])

    # -- per-unit progress ---------------------------------------------------
    def set_total(self, name, total):
        with self._lock:
            self._stage(name)["checkpoint"]["total"] = int(total)
            self._save()

    def mark_unit_done(self, name, unit_id, extra=None):
        """Record one completed unit (+ optional per-unit data)."""
        with self._lock:
            cp = self._stage(name)["checkpoint"]
            done = cp.setdefault("done_units", [])
            if unit_id not in done:
                done.append(unit_id)
            if extra:
                units = cp.setdefault("units", {})
                prev = units.get(unit_id) or {}
                prev.update(extra)
                units[unit_id] = prev
            self._stage(name)["updated_at"] = _now()
            self._save()

    def is_unit_done(self, name, unit_id):
        with self._lock:
            return unit_id in self._stage(name)["checkpoint"].get(
                "done_units", [])

    def unit_extra(self, name, unit_id):
        with self._lock:
            return dict(self._stage(name)["checkpoint"].get(
                "units", {}).get(unit_id) or {})

    # -- pause / fail / resume ----------------------------------------------
    @property
    def job_status(self):
        with self._lock:
            return self._data["job_status"]

    @property
    def pause_reason(self):
        with self._lock:
            return self._data.get("pause_reason", "")

    @property
    def pause_hint(self):
        with self._lock:
            return self._data.get("pause_hint", "")

    def pause_job(self, reason, hint=""):
        """Pause: progress kept, resume continues from next pending unit.

        job_status is ALWAYS "paused" — pausing is explicit intent, even if
        no stage was mid-flight (e.g. user paused between stages).
        """
        with self._lock:
            self._data["pause_reason"] = reason or "Pause ho gaya."
            self._data["pause_hint"] = hint or _WTD_RESUME
            for st in self._data["stages"].values():
                if st["status"] == _RUNNING:
                    st["status"] = _PAUSED
                    st["updated_at"] = _now()
            self._data["job_status"] = _PAUSED
            self._save()

    def fail_stage(self, name, category, message, what_to_do, retryable=False):
        with self._lock:
            st = self._stage(name)
            st["status"] = _FAILED
            st["updated_at"] = _now()
            st["error"] = {"category": category, "message": message,
                           "what_to_do": what_to_do, "retryable": bool(retryable),
                           "at": _now()}
            self._data["job_status"] = self._overall_status()
            self._save()

    def stage_error(self, name):
        with self._lock:
            return self._stage(name)["error"]

    def resume_job(self):
        """Next resumable stage -> (name, checkpoint dict), or None.

        Order: voiceover -> slots -> reference -> photos -> assemble ->
        render. Includes failed stages (user may have fixed the cause, e.g.
        added a valid key) — the stage re-runs from its checkpoint.
        """
        with self._lock:
            for name in STAGES:
                if self._stage(name)["status"] in (_RUNNING, _PAUSED, _FAILED):
                    self._stage(name)["status"] = _RUNNING
                    self._stage(name)["updated_at"] = _now()
                    self._data["job_status"] = self._overall_status()
                    cp = dict(self._stage(name)["checkpoint"])
                    self._save()
                    return name, cp
            return None

    # -- honest progress -------------------------------------------------------
    def job_summary(self):
        """Manifest-backed per-stage progress: {stage: {status, done, total}}.

        done/total come ONLY from checkpoints — never estimated.
        """
        with self._lock:
            out = {}
            for name in STAGES:
                st = self._stage(name)
                cp = st["checkpoint"]
                out[name] = {"status": st["status"],
                             "done": len(cp.get("done_units", [])),
                             "total": int(cp.get("total", 0)),
                             "error": st["error"]}
            return out

    def progress_line(self, name):
        s = self.job_summary()[name]
        if s["total"]:
            return "%d/%d" % (s["done"], s["total"])
        return s["status"]


# ---------------------------------------------------------------------------
# Central pause/fail decision + crash-recovery scan
# ---------------------------------------------------------------------------

def record_stage_error(job, stage, err):
    """Classify err, record pause-or-fail in the manifest.

    Returns "paused" or "failed". Never raises (JobPaused passes through
    untouched — it was already recorded by whoever raised it).
    """
    if isinstance(err, JobPaused):
        job.pause_job(err.user_message, err.resume_hint)
        return "paused"
    info = classify_error(err)
    if info["retryable"]:
        job.pause_job(info["message"], info["what_to_do"])
        return "paused"
    job.fail_stage(stage, info["category"], info["message"],
                   info["what_to_do"], retryable=False)
    return "failed"


def raise_if_paused(job):
    """After a (False, msg) return, escalate a paused job to JobPaused."""
    if job is not None and job.job_status == _PAUSED:
        raise JobPaused(job.pause_reason or "Pause ho gaya.",
                        job.pause_hint or "")
    return False


def scan_incomplete(work_dirs, now=None):
    """Find work dirs with unfinished jobs (crash recovery).

    Returns [{"work_dir","job_status","summary","stale","updated_at"}].
    "stale" = still marked running but untouched for STALE_AFTER (same
    horizon as render_worker's stale render.lock) -> probably crashed.
    Does NOT auto-start anything; the UI offers Resume.
    """
    now = now if now is not None else _now()
    found = []
    for d in work_dirs or []:
        manifest = os.path.join(d or "", MANIFEST_NAME)
        if not os.path.isfile(manifest):
            continue
        try:
            store = JobStore(d)
        except Exception:
            continue
        status = store.job_status
        summary = store.job_summary()
        unfinished = any(v["status"] in (_RUNNING, _PAUSED, _FAILED)
                         for v in summary.values())
        if status in (_RUNNING, _PAUSED, _FAILED) or unfinished:
            with store._lock:
                updated = store._data.get("updated_at", 0)
            stale = (status == _RUNNING
                     and (now - updated) >= STALE_AFTER)
            found.append({"work_dir": os.path.abspath(d),
                          "job_status": status, "summary": summary,
                          "stale": stale, "updated_at": updated})
    # most recently active first
    found.sort(key=lambda f: f["updated_at"], reverse=True)
    return found
