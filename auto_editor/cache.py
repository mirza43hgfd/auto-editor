"""SQLite-backed prompt/response cache.

Identical (model, prompt, params, media inputs) never hits the network twice
in a row: text responses are cached as TEXT, generated images as raw bytes.
Media inputs are keyed by path + size + mtime (cheap; correct for files this
app generates, since a rewrite bumps mtime).

Stats: hits / saves / entries — surfaced in the quota dashboard.
Threading: one connection per PromptCache; create one per thread if needed.
"""

import hashlib
import json
import os
import sqlite3
import time

SCHEMA_VERSION = 1


def _default_path():
    from . import settings_store
    d = settings_store._config_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "prompt_cache.sqlite3")


class PromptCache:
    def __init__(self, path=None):
        self.path = path or _default_path()
        if self.path != ":memory:":
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            " k TEXT PRIMARY KEY, kind TEXT NOT NULL, blob BLOB NOT NULL,"
            " created REAL NOT NULL)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (k, v) VALUES ('schema', '%d')"
            % SCHEMA_VERSION)
        self._conn.commit()
        self._hits = 0
        self._saves = 0

    # -- key ------------------------------------------------------------------
    def key(self, model, prompt, params=None, media_paths=None):
        """Stable hex key for one logical request."""
        h = hashlib.sha256()
        h.update(("ae-cache-v1|%s|" % (model or "")).encode("utf-8"))
        h.update((prompt or "").encode("utf-8", "replace"))
        h.update(b"|")
        h.update(json.dumps(params or {}, sort_keys=True,
                            default=str).encode("utf-8"))
        for p in media_paths or []:
            try:
                st = os.stat(p)
                h.update(("|%s|%d|%d" % (p, st.st_size,
                                        int(st.st_mtime))).encode("utf-8"))
            except OSError:
                h.update(("|%s|missing" % p).encode("utf-8"))
        return h.hexdigest()

    # -- raw get/put ------------------------------------------------------------
    def get(self, ckey):
        row = self._conn.execute(
            "SELECT kind, blob FROM cache WHERE k=?", (ckey,)).fetchone()
        if row is None:
            return None
        self._hits += 1
        return row[0], row[1]

    def put(self, ckey, kind, blob):
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (k, kind, blob, created)"
            " VALUES (?, ?, ?, ?)", (ckey, kind, blob, time.time()))
        self._conn.commit()
        self._saves += 1

    # -- typed helpers ------------------------------------------------------------
    def get_text(self, model, prompt, params=None, media_paths=None):
        hit = self.get(self.key(model, prompt, params, media_paths))
        if hit is None or hit[0] != "text":
            return None
        return hit[1].decode("utf-8", "replace")

    def put_text(self, model, prompt, text, params=None, media_paths=None):
        self.put(self.key(model, prompt, params, media_paths), "text", text)

    def get_bytes(self, model, prompt, params=None, media_paths=None):
        hit = self.get(self.key(model, prompt, params, media_paths))
        if hit is None or hit[0] != "bytes":
            return None
        return bytes(hit[1])

    def put_bytes(self, model, prompt, blob, params=None, media_paths=None):
        self.put(self.key(model, prompt, params, media_paths), "bytes", blob)

    def get_text_by_key(self, ckey):
        hit = self.get(ckey)
        if hit is None or hit[0] != "text":
            return None
        return hit[1].decode("utf-8", "replace")

    def put_text_by_key(self, ckey, text):
        self.put(ckey, "text", text)

    # -- stats ----------------------------------------------------------------------
    def stats(self):
        entries = self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        return {"hits": self._hits, "saves": self._saves, "entries": entries}

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
