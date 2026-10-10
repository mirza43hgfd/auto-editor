"""Local app settings.

API key storage (M2):
  - On Windows: DPAPI (win32crypt.CryptProtectData, user scope) — the OS-held
    key encrypts the blob; only the same Windows user can decrypt it. This is
    real encryption, not obfuscation. Requires pywin32 (bundled in the EXE
    build); if win32crypt is unavailable it falls back to the obfuscated
    store below and documents that in the key_status readout.
  - Elsewhere (Linux/macOS dev): the existing obfuscated store (XOR with app
    salt + base64) — keeps the key out of casual plaintext view, NOT
    encryption. Documented honestly as such.
  - Format is backward compatible: DPAPI blobs are stored as
    "DPAPI1:<base64>", legacy blobs decode as before, so keys saved by older
    versions keep working.

The key is NEVER written to logs. This is the user's own key on their own PC.
"""

import base64
import json
import os
import sys

# Fixed per-app salt for light obfuscation (not encryption — keeps the key out
# of casual plaintext view in the config file).
_SALT = b"auto-editor-v1-gemini-key-salt"


def _xor(data: bytes) -> bytes:
    return bytes(b ^ _SALT[i % len(_SALT)] for i, b in enumerate(data))


def _config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "AutoEditor")
    return os.path.join(os.path.expanduser("~"), ".config", "autoeditor")


def _config_path():
    return os.path.join(_config_dir(), "config.json")


def _dpapi():
    """win32crypt module or None. Guarded: import only on Windows."""
    if sys.platform != "win32":
        return None
    try:
        import win32crypt
        return win32crypt
    except ImportError:
        return None


def dpapi_available():
    """True when OS-backed (DPAPI) key storage can be used right now."""
    return _dpapi() is not None


def key_storage_backend():
    """'DPAPI' on Windows with pywin32, else 'obfuscated' (honest label for UI)."""
    return "DPAPI" if dpapi_available() else "obfuscated"


def _dpapi_protect(data: bytes) -> bytes:
    crypt = _dpapi()
    # user scope (default): only this Windows user can decrypt — the right
    # scope for "the user's own key on their own PC".
    return crypt.CryptProtectData(data, None, None, None, None, 0)[1]


def _dpapi_unprotect(blob: bytes) -> bytes:
    crypt = _dpapi()
    return crypt.CryptUnprotectData(blob, None, None, None, None, 0)[1]


def _load_all():
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_all(data):
    os.makedirs(_config_dir(), exist_ok=True)
    tmp = _config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _config_path())


def _encode_key(key: str) -> str:
    raw = key.encode("utf-8")
    if _dpapi() is not None:
        try:
            return "DPAPI1:" + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
        except Exception:
            pass  # fall through to obfuscated store
    return base64.b64encode(_xor(raw)).decode("ascii")


def _decode_key(blob: str) -> str:
    try:
        if blob.startswith("DPAPI1:"):
            crypt = _dpapi()
            if crypt is None:
                return ""  # DPAPI blob on a non-DPAPI machine: cannot decrypt
            return _dpapi_unprotect(base64.b64decode(blob[7:].encode("ascii"))).decode("utf-8")
        return _xor(base64.b64decode(blob.encode("ascii"))).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------------------
# API key POOL — up to MAX_KEYS slots, auto-failover on quota errors.
# Keys are obfuscated in the config file and must NEVER be logged/printed.
# ---------------------------------------------------------------------------
MAX_KEYS = 7
COOLDOWN_SEC = 600  # a key that hits quota rests 10 minutes

STATUS_OK = "ok"          # green
STATUS_LIMITED = "limited"  # orange (quota/rate hit, cooling down)
STATUS_FAILED = "failed"  # red (rejected key)


def set_api_key(key: str):
    """Legacy single-key setter -> slot 0 of the pool."""
    keys = get_api_key_slots()
    keys[0] = key or ""
    set_api_key_slots(keys)


def get_api_key() -> str:
    """Legacy single-key getter -> first usable pool key. Never log this."""
    for k in get_api_key_slots():
        if k:
            return k
    return ""


def has_api_key() -> bool:
    return bool(get_api_key())


def get_api_key_slots():
    """Return list of MAX_KEYS strings ('' = empty slot). Never log."""
    data = _load_all()
    blobs = data.get("gemini_api_keys", [])
    slots = [_decode_key(b) if b else "" for b in blobs[:MAX_KEYS]]
    while len(slots) < MAX_KEYS:
        slots.append("")
    return slots


def set_api_key_slots(keys):
    """Store up to MAX_KEYS keys obfuscated. Empty strings clear slots."""
    data = _load_all()
    blobs = [_encode_key(k) if k else "" for k in list(keys)[:MAX_KEYS]]
    data["gemini_api_keys"] = blobs
    # migrate away the legacy single-key field
    data.pop("gemini_api_key", None)
    _save_all(data)


def get_key_status(i: int):
    """Return {'status': ok|limited|failed, 'cooldown_until': epoch} for slot i."""
    data = _load_all()
    return data.get("key_status", {}).get(str(i), {"status": STATUS_OK, "cooldown_until": 0})


def set_key_status(i: int, status: str, cooldown_sec: int = 0):
    import time
    data = _load_all()
    st = data.setdefault("key_status", {})
    st[str(i)] = {
        "status": status,
        "cooldown_until": (time.time() + cooldown_sec) if cooldown_sec else 0,
    }
    _save_all(data)


def usable_key_indices():
    """Indices of non-empty keys not currently cooling down, in slot order."""
    import time
    now = time.time()
    out = []
    for i, k in enumerate(get_api_key_slots()):
        if not k:
            continue
        st = get_key_status(i)
        if st["status"] == STATUS_FAILED:
            continue
        if st["cooldown_until"] and st["cooldown_until"] > now:
            continue
        out.append(i)
    return out


def browser_profile_dir():
    d = os.path.join(_config_dir(), "browser_profile")
    os.makedirs(d, exist_ok=True)
    return d


def get(key, default=None):
    return _load_all().get(key, default)


def set(key, value):
    data = _load_all()
    data[key] = value
    _save_all(data)
