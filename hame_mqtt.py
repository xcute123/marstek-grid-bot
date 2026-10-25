"""
Marstek / Hame cloud MQTT helpers (port of tomquist/hame-relay src/topic.ts + encryption.ts).

Topic id for a device on the Marstek cloud broker:
  * salt pair from getDeviceList "salt" = "<current>,<issued>"
  * confirmed/rotating -> CommonHelper.cq(current_salt, mac, type)
  * pending/unusable   -> AES-128-CBC(topic_encryption_key, mac) hex  (calculateNewVersionTopicId)
Topics:
  app -> device : <prefix><type>/App/<id>/ctrl      (we publish "cd=1")
  device -> app : <prefix><type>/device/<id>/ctrl   (we subscribe; CSV key=value telemetry)
"""
from __future__ import annotations

import hashlib

_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


# ---------------------------------------------------------------- StreamUtil
def _stream_e(data: str, key: str) -> str:
    if not key:
        raise ValueError("Key cannot be empty")
    seed = 0
    for byte in key.encode("utf-8"):
        seed = (seed * 31 + byte) % 2_147_483_647
    ks = []
    state = seed
    for _ in range(len(data)):  # length in characters, as in the original
        state = (state * 1_664_525 + 1_013_904_223) % 4_294_967_296
        ks.append((state ^ (state >> 16)) & 0xFF)
    db = data.encode("utf-8")
    return bytes(b ^ ks[i % len(ks)] for i, b in enumerate(db)).hex()


# ---------------------------------------------------------------- CodeUtil
def _code_e(inp: str) -> str:
    h = hashlib.sha256(inp.encode("utf-8")).digest()
    words = [int.from_bytes(h[i * 4:i * 4 + 4], "big") for i in range(8)]
    out = []
    for i in range(24):
        word = words[i % 8]
        shift = (i // 8) * 8
        b = (word >> shift) & 0xFF
        out.append(_CHARSET[b % 62])
        out.append(_CHARSET[(b * 31) % 62])
    return "".join(out)


# ---------------------------------------------------------------- HexUtil
def _build_perm(key: bytes, size: int) -> list[int]:
    p = list(range(size))
    if size <= 1:
        return p
    j = 0
    kl = len(key)
    for i in range(size):
        j = (j + p[i] + key[i % kl]) % size
        p[i], p[j] = p[j], p[i]
    return p


def _scramble(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    p = _build_perm(key, len(data))
    return bytes(data[idx] for idx in p)


def _text_for_rand(content: str, mac: str) -> str:
    if not content or not mac:
        return ""
    hex_content = content.encode("utf-8").hex()
    if len(hex_content) < 2:
        return ""
    parsed = int(hex_content[-2:], 16)
    n = parsed % 5 or 1
    data = bytes.fromhex(hex_content)
    key = mac.encode("utf-8")  # _hexToBytes(strToHex(mac)) == utf-8 bytes
    for _ in range(n):
        data = _scramble(data, key)
    return data.hex()


# ---------------------------------------------------------------- CommonHelper
def cq(salt: str, mac: str, vid: str) -> str:
    ln = len(mac)
    if ln < 4:
        return ""
    var1 = f"{vid}_{mac[:-4]}"
    var2 = f"{mac[1:ln - 2]}_{vid}"
    h1 = _text_for_rand(salt, var1)
    h2 = _stream_e(f"{vid}{mac}", var2)
    return _code_e(f"{h1}{h2}")[:24]


def resolve_topic_id(salt_pair: str, mac: str, vid: str) -> tuple[str, str | None]:
    """-> (kind, id|None); kind in confirmed | rotating | pending | unusable."""
    if not salt_pair or not isinstance(salt_pair, str):
        return "unusable", None
    parts = salt_pair.split(",")
    if len(parts) < 2:
        return "unusable", None
    current, issued = parts[0], parts[1]
    if current == "0":
        return "pending", None
    if current == "" or issued == "" or issued == "0":
        return "unusable", None
    if issued == current:
        return "confirmed", cq(issued, mac, vid) or None
    return "rotating", cq(current, mac, vid) or None


def aes_topic_id(key_hex: str, mac: str) -> str:
    """calculateNewVersionTopicId: AES-128-CBC, zero IV, PKCS7, hex."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = bytes.fromhex(key_hex.strip())
    padder = padding.PKCS7(128).padder()
    data = padder.update(mac.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    return (enc.update(data) + enc.finalize()).hex()


def parse_kv(payload: str) -> dict[str, str]:
    """Device frames are CSV 'k=v,k=v' (records may be ';'-separated). Returns flat dict."""
    out: dict[str, str] = {}
    for rec in payload.replace(" ", "").split(";"):
        for tok in rec.split(","):
            if tok.count("=") == 1:
                k, v = tok.split("=", 1)
                if k:
                    out[k] = v
    return out


if __name__ == "__main__":
    # test vectors from hame-relay src/topic.test.ts
    vec = [
        (("abc123def456789a", "112233445566", "HMG-50"), "LV9VDVC0S03VDVlVTVTVK0q0"),
        (("fedcba9876543210", "aabbccddeeff", "HMG-50"), "HVe0ZVW0Y0jVBVRVC0DVC0pV"),
        (("1234567890abcdef", "001122334455", "HMG-50"), "C0q0a0w03VdVZVhVc0lVlVE0"),
        (("sample123456782d", "aabbccdd1234", "HMG-50"), "I0a0i03VRVO0w09Vk0BV80g0"),
    ]
    ok = True
    for args, exp in vec:
        got = cq(*args)
        print(("OK  " if got == exp else "FAIL"), args, got, exp)
        ok &= got == exp
    print("ALL OK" if ok else "MISMATCH")
