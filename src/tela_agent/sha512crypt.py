"""SHA512-CRYPT ("$6$") password hashes, as understood by Dovecot and glibc.

Python 3.13 removed the crypt module, so this implements Ulrich Drepper's
algorithm (https://www.akkadia.org/drepper/SHA-crypt.txt) directly. Tested
against `openssl passwd -6`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ROUNDS_DEFAULT = 5000
# Byte order in which the final digest is encoded (three bytes per group).
_ORDER = [
    (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26),
    (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32),
    (12, 33, 54), (34, 55, 13), (56, 14, 35), (15, 36, 57), (37, 58, 16), (59, 17, 38),
    (18, 39, 60), (40, 61, 19), (62, 20, 41),
]  # fmt: skip


def _b64(b2: int, b1: int, b0: int, n: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_ALPHABET[w & 0x3F])
        w >>= 6
    return "".join(out)


def _repeat(digest: bytes, length: int) -> bytes:
    return (digest * (length // 64 + 1))[:length]


def sha512_crypt(password: str, salt: str | None = None, rounds: int = _ROUNDS_DEFAULT) -> str:
    pw = password.encode("utf-8")
    if salt is None:
        salt = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    s = salt.encode("ascii")[:16]
    rounds = max(1000, min(rounds, 999_999_999))

    b = hashlib.sha512(pw + s + pw).digest()
    a = hashlib.sha512()
    a.update(pw + s)
    a.update(_repeat(b, len(pw)))
    n = len(pw)
    while n:
        a.update(b if n & 1 else pw)
        n >>= 1
    a_digest = a.digest()

    dp = _repeat(hashlib.sha512(pw * len(pw)).digest(), len(pw))
    ds = _repeat(hashlib.sha512(s * (16 + a_digest[0])).digest(), len(s))

    c = a_digest
    for i in range(rounds):
        h = hashlib.sha512()
        h.update(dp if i & 1 else c)
        if i % 3:
            h.update(ds)
        if i % 7:
            h.update(dp)
        h.update(c if i & 1 else dp)
        c = h.digest()

    encoded = "".join(_b64(c[x], c[y], c[z], 4) for x, y, z in _ORDER) + _b64(0, 0, c[63], 2)
    prefix = "$6$" if rounds == _ROUNDS_DEFAULT else f"$6$rounds={rounds}$"
    return f"{prefix}{s.decode()}${encoded}"


def verify(password: str, hashed: str) -> bool:
    parts = hashed.split("$")
    if len(parts) < 4 or parts[1] != "6":
        return False
    rounds = _ROUNDS_DEFAULT
    salt = parts[2]
    if salt.startswith("rounds="):
        rounds = int(salt.split("=", 1)[1])
        salt = parts[3]
    return hmac.compare_digest(sha512_crypt(password, salt, rounds), hashed)
