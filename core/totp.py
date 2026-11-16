"""TOTP (RFC 6238) без внешних зависимостей: SHA-1, 6 цифр, шаг 30 с."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30


def generate_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret, counter, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def totp_now(secret, at=None):
    return _code(secret, int((at if at is not None else time.time()) // STEP))


def verify(secret, code, at=None, window=1):
    """Возвращает счётчик шага при верном коде (±window шагов), иначе None."""
    code = str(code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return None
    counter = int((at if at is not None else time.time()) // STEP)
    for c in range(counter - window, counter + window + 1):
        if hmac.compare_digest(_code(secret, c), code):
            return c
    return None


def provisioning_uri(email, secret, issuer="OZOK"):
    return f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret}&issuer={quote(issuer)}"
