"""TTS chunk batching: merge adjacent short chunks to cut API call count.

Fewer, longer TTS requests burn far less rate-limit budget than many tiny
ones (RPM is usually the binding free-tier constraint, not volume).

TRADEOFF (documented honestly): merging coarsens per-chunk timing, and slot
boundaries are derived from chunk durations — so aggressive merging gives
fewer, longer slots. Expose max_chars in UI; default keeps chunks near the
existing ~300-500 char rhythm while eliminating 1-2 sentence fragments.

Wiring into generate_voiceover() is a follow-up milestone (needs the UI
setting); this module is the pure, tested helper.
"""

DEFAULT_MAX_CHARS = 1200


def merge_chunks(chunks, max_chars=DEFAULT_MAX_CHARS, separator="\n\n"):
    """Merge adjacent short chunks.

    chunks: list of non-empty strings (order preserved).
    Rules:
      - never split a chunk, even if it exceeds max_chars on its own;
      - accumulate following chunks while the joined length stays <= max_chars;
      - drop empty / whitespace-only chunks;
      - a single oversized chunk passes through untouched.
    Returns a new list of strings.
    """
    if max_chars is None or max_chars <= 0:
        max_chars = DEFAULT_MAX_CHARS
    out, cur = [], ""
    for ch in chunks or []:
        text = (ch or "").strip()
        if not text:
            continue
        if not cur:
            cur = text
            continue
        joined = cur + separator + text
        if len(joined) <= max_chars:
            cur = joined
        else:
            out.append(cur)
            cur = text
    if cur:
        out.append(cur)
    return out


def batch_stats(original, merged):
    """Small report used by UI/tests: how many calls were saved."""
    return {"chunks_before": len(original), "chunks_after": len(merged),
            "calls_saved": max(0, len(original) - len(merged))}
