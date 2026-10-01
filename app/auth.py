"""Passwords and sessions.

* Passwords are hashed with scrypt (Python stdlib, memory-hard); plain passwords are
  never stored or logged. A generated password is returned exactly once.
* A session token is ``vlg1.<payload>.<hmac>``: an HMAC-SHA256-signed JSON payload
  with the username, a token version and an expiry. Changing or resetting a
  password bumps the version, which invalidates every earlier token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import string
import time

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN = 2 ** 14, 8, 1, 32
_EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")

# No look-alikes (0/O, 1/l/I) so a password copied from a CSV by eye still works.
_LOWER = "abcdefghijkmnopqrstuvwxyz"
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_DIGITS = "23456789"
_SYMBOLS = "!@#$%^&*-_=+?"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256


def normalize_username(name: str) -> str:
    return (name or "").strip().lower()


def is_email(name: str) -> bool:
    return bool(_EMAIL_RE.match(name))


def generate_password(length: int = 16) -> str:
    pools = [_LOWER, _UPPER, _DIGITS, _SYMBOLS]
    chars = [secrets.choice(p) for p in pools]
    everything = "".join(pools)
    chars += [secrets.choice(everything) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def password_problems(password: str, username: str) -> list[str]:
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(f"must be at most {MAX_PASSWORD_LENGTH} characters")
    if password.strip().lower() in (username.lower(), username.split("@")[0].lower()):
        problems.append("must not be your username")
    classes = sum(any(c in pool for c in password) for pool in
                  (string.ascii_lowercase, string.ascii_uppercase, string.digits))
    if classes < 2 and len(password) < 20:
        problems.append("must mix letters and digits (or be 20+ characters)")
    return problems


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                            p=_SCRYPT_P, dklen=_DKLEN, maxmem=64 * 1024 * 1024)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


_DUMMY_HASH = None


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. With no stored hash, still does the work (timing)."""
    global _DUMMY_HASH
    if not stored:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password("dummy-password-for-timing")
        stored, ok_if_match = _DUMMY_HASH, False
    else:
        ok_if_match = True
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r),
                              p=int(p), dklen=len(_unb64(digest)), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(calc, _unb64(digest)) and ok_if_match
    except (ValueError, TypeError):
        return False


def issue_token(secret: str, username: str, token_version: int, hours: float) -> tuple[str, int]:
    now = int(time.time())
    exp = now + int(hours * 3600)
    payload = _b64(json.dumps({"sub": username, "tv": token_version, "iat": now, "exp": exp,
                               "jti": secrets.token_hex(8)}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"vlg1.{payload}".encode(), hashlib.sha256).digest())
    return f"vlg1.{payload}.{sig}", exp


def read_token(secret: str, token: str) -> dict | None:
    """Payload of a valid, unexpired token, else None."""
    try:
        prefix, payload, sig = token.split(".")
        if prefix != "vlg1":
            return None
        expected = _b64(hmac.new(secret.encode(), f"vlg1.{payload}".encode(),
                                 hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig):
            return None
        data = json.loads(_unb64(payload))
        if int(data.get("exp", 0)) < time.time():
            return None
        return data
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def credentials_csv(rows: list[tuple[str, str]]) -> str:
    """username,password CSV (RFC 4180 quoting)."""
    def q(v: str) -> str:
        return '"' + v.replace('"', '""') + '"' if any(c in v for c in ',"\n\r') else v
    return "username,password\r\n" + "".join(f"{q(u)},{q(p)}\r\n" for u, p in rows)
