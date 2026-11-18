"""Opaque URL ID encoding/decoding (Feistel + Base62) for Marsoud.

Converts sequential integer database IDs into fixed-length 8-char Base62
tokens in web URLs so external users cannot enumerate entities or infer
record counts (companies, users, invoices, leads, etc.).
"""
import hashlib
import hmac
import struct
from flask import current_app
from werkzeug.routing import BaseConverter, ValidationError

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)  # 62
TOKEN_LEN = 8
# 62^8 = 218,340,105,584,896 (~47 bits). We operate on 44-bit integers
# (up to 17.5 trillion IDs) split into two 22-bit halves so every output
# fits cleanly in 8 Base62 chars without overflow.
HALF_BITS = 22
HALF_MASK = (1 << HALF_BITS) - 1
MAX_ID = (1 << (HALF_BITS * 2)) - 1


def _get_secret() -> bytes:
    try:
        key = current_app.config.get("SECRET_KEY") or "marsoud-default-hid-key"
    except RuntimeError:
        import os
        key = os.environ.get("FLASK_SECRET_KEY") or "marsoud-default-hid-key"
    return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def _round_fn(val: int, round_idx: int, secret: bytes) -> int:
    msg = struct.pack(">BI", round_idx, val)
    digest = hmac.new(secret, msg, hashlib.sha256).digest()
    return struct.unpack(">I", digest[:4])[0] & HALF_MASK


def encode_id(num) -> str:
    if num is None:
        return ""
    if isinstance(num, str):
        if num.isdigit():
            num = int(num)
        else:
            # Already encoded or non-numeric string
            return num
    n = int(num)
    if n < 0 or n > MAX_ID:
        raise ValueError(f"ID out of range for opaque encoding: {n}")

    secret = _get_secret()
    left = (n >> HALF_BITS) & HALF_MASK
    right = n & HALF_MASK
    for r in range(4):
        left, right = right, left ^ _round_fn(right, r, secret)
    cipher = (left << HALF_BITS) | right

    chars = []
    for _ in range(TOKEN_LEN):
        cipher, rem = divmod(cipher, BASE)
        chars.append(ALPHABET[rem])
    return "".join(reversed(chars))


def decode_id(token: str) -> int:
    if not isinstance(token, str) or len(token) != TOKEN_LEN:
        raise ValueError("Invalid opaque ID length")
    cipher = 0
    for ch in token:
        idx = ALPHABET.find(ch)
        if idx == -1:
            raise ValueError("Invalid opaque ID character")
        cipher = cipher * BASE + idx

    if cipher > MAX_ID:
        raise ValueError("Opaque ID value out of domain")

    secret = _get_secret()
    left = (cipher >> HALF_BITS) & HALF_MASK
    right = cipher & HALF_MASK
    for r in reversed(range(4)):
        left, right = right ^ _round_fn(left, r, secret), left
    return (left << HALF_BITS) | right


class HashIdConverter(BaseConverter):
    regex = r"[0-9A-Za-z]{8}"

    def to_python(self, value: str) -> int:
        try:
            return decode_id(value)
        except ValueError:
            raise ValidationError()

    def to_url(self, value) -> str:
        return encode_id(value)


def decode_any_id(val) -> int:
    """Decode an opaque 8-char hid token OR accept an int/digit string in JSON bodies."""
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if s.isdigit():
        return int(s)
    return decode_id(s)
