"""Web Push cryptography in pure standard library (todo #240): P-256 ECDH and ECDSA, HKDF-SHA256, AES-128-GCM.

Why not a dependency: the project venv has no `cryptography`, and installing one would need a manual step after the merge.
The payloads are tiny (< 4 KiB), so speed does not matter. Checked against the RFC 8291 example (tests) and, during
development, against `cryptography`. Not constant time: acceptable here (local signing of a short-lived JWT, one fresh
ephemeral key per message), never reuse this module for anything else.

- `encrypt(payload, ua_public, auth_secret)`: RFC 8291 message body (aes128gcm content coding, RFC 8188, one record).
- `vapid_jwt(private, aud, sub, exp)`: RFC 8292 ES256 token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

# --- P-256 (SEC 2 / FIPS 186-4) ------------------------------------------------------------------------------------
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)

Point = tuple[int, int] | None  # None = point at infinity


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def on_curve(pt: Point) -> bool:
    if pt is None:
        return False
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - x * x * x - A * x - B) % P == 0


def _add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return x3, (lam * (x1 - x3) - y1) % P


def mul(k: int, pt: Point) -> Point:
    out: Point = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        k >>= 1
    return out


def encode_point(pt: Point) -> bytes:
    assert pt is not None
    return b"\x04" + pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")


def decode_point(raw: bytes) -> tuple[int, int]:
    """Uncompressed SEC1 point (65 bytes); anything else, or a point off the curve, raises ValueError."""
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("not an uncompressed P-256 point")
    pt = (int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big"))
    if not on_curve(pt):
        raise ValueError("point not on P-256")
    return pt


def new_private() -> int:
    return secrets.randbelow(N - 1) + 1


def public_of(d: int) -> bytes:
    return encode_point(mul(d, G))


def ecdh(d: int, peer_public: bytes) -> bytes:
    shared = mul(d, decode_point(peer_public))
    if shared is None:
        raise ValueError("degenerate shared secret")
    return shared[0].to_bytes(32, "big")


def sign(d: int, msg: bytes) -> bytes:
    """ECDSA P-256 / SHA-256, raw r || s (JWS form)."""
    e = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    while True:
        k = new_private()
        r = mul(k, G)[0] % N  # type: ignore[index]
        s = pow(k, -1, N) * (e + r * d) % N
        if r and s:
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify(public: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        q = decode_point(public)
    except ValueError:
        return False
    if len(sig) != 64:
        return False
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if not (0 < r < N and 0 < s < N):
        return False
    e = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    w = pow(s, -1, N)
    pt = _add(mul(e * w % N, G), mul(r * w % N, q))
    return pt is not None and pt[0] % N == r


# --- AES-128 (FIPS 197) and GCM (SP 800-38D) -----------------------------------------------------------------------
def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a, b = _xtime(a), b >> 1
    return out


def _make_sbox() -> bytes:
    box = bytearray(256)
    for x in range(256):
        inv = 0
        if x:
            inv = 1
            for _ in range(254):  # x^254 = x^-1 in GF(2^8)
                inv = _gmul(inv, x)
        s = inv
        for i in range(1, 5):
            s ^= ((inv << i) | (inv >> (8 - i))) & 0xFF
        box[x] = s ^ 0x63
    return bytes(box)


SBOX = _make_sbox()
_MUL2 = bytes(_xtime(i) & 0xFF for i in range(256))


def _expand(key: bytes) -> list[bytes]:
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [bytes(sum(w[4 * r:4 * r + 4], [])) for r in range(11)]


def _encrypt_block(rk: list[bytes], block: bytes) -> bytes:
    s = [a ^ b for a, b in zip(block, rk[0])]
    for rnd in range(1, 11):
        s = [SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]  # ShiftRows (column-major state)
        if rnd != 10:
            out = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                t = a0 ^ a1 ^ a2 ^ a3
                out += [a0 ^ t ^ _MUL2[a0 ^ a1], a1 ^ t ^ _MUL2[a1 ^ a2], a2 ^ t ^ _MUL2[a2 ^ a3], a3 ^ t ^ _MUL2[a3 ^ a0]]
            s = out
        s = [a ^ b for a, b in zip(s, rk[rnd])]
    return bytes(s)


_R = 0xE1 << 120


def _ghash_mul(x: int, y: int) -> int:
    z, v = 0, y
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            z ^= v
        v = (v >> 1) ^ _R if v & 1 else v >> 1
    return z


def aes128gcm_encrypt(key: bytes, iv: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """ciphertext || 16-byte tag, 12-byte IV."""
    if len(key) != 16 or len(iv) != 12:
        raise ValueError("AES-128-GCM needs a 16-byte key and a 12-byte IV")
    rk = _expand(key)
    h = int.from_bytes(_encrypt_block(rk, bytes(16)), "big")
    j0 = iv + b"\x00\x00\x00\x01"
    out = bytearray()
    for i in range(0, len(plaintext), 16):
        ctr = iv + ((2 + i // 16) & 0xFFFFFFFF).to_bytes(4, "big")
        ks = _encrypt_block(rk, ctr)
        out += bytes(a ^ b for a, b in zip(plaintext[i:i + 16], ks))
    y = 0
    for data in (aad, bytes(out)):
        for i in range(0, len(data), 16):
            y = _ghash_mul(y ^ int.from_bytes(data[i:i + 16].ljust(16, b"\x00"), "big"), h)
    y = _ghash_mul(y ^ ((len(aad) * 8) << 64 | len(out) * 8), h)
    tag = int.from_bytes(_encrypt_block(rk, j0), "big") ^ y
    return bytes(out) + tag.to_bytes(16, "big")


# --- RFC 8291 / RFC 8188 --------------------------------------------------------------------------------------------
def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


RECORD_SIZE = 4096


def encrypt(payload: bytes, ua_public: bytes, auth_secret: bytes, *, as_private: int | None = None,
            salt: bytes | None = None) -> bytes:
    """The encrypted push message body. `as_private` and `salt` are fresh random values; set only by tests."""
    if len(auth_secret) != 16:
        raise ValueError("auth secret must be 16 bytes")
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise ValueError("payload too long for one record")
    d = as_private or new_private()
    as_public = public_of(d)
    salt = salt or secrets.token_bytes(16)
    prk_key = _hmac(auth_secret, ecdh(d, ua_public))
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + aes128gcm_encrypt(cek, nonce, payload + b"\x02")


def vapid_jwt(d: int, aud: str, sub: str, exp: int) -> str:
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps({"aud": aud, "exp": exp, "sub": sub}, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    return f"{head}.{body}.{b64u(sign(d, signing_input))}"
