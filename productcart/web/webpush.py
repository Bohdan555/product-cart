"""Web Push без сторонніх бібліотек: VAPID (RFC 8292) і шифрування aes128gcm (RFC 8291) на `cryptography`.

Ключ VAPID виводимо з APP_SECRET (HKDF), тож нового секрету не треба: поки APP_SECRET той самий,
підписки браузерів лишаються дійсними. Надсилаємо лише на відомі push-сервіси браузерів (захист від SSRF).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)

# Push-сервіси браузерів: Chrome/Android, Firefox, Safari/iOS, Edge
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com",
              "web.push.apple.com", "notify.windows.com", "push.apple.com")
_N = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)   # порядок кривої P-256


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def allowed_endpoint(url: str) -> bool:
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS) and len(url) < 1000


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """RFC 8291: тіло запиту aes128gcm (заголовок + один запис). salt/server_key — лише для тестів."""
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(server_key.public_key())
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh = server_key.exchange(ec.ECDH(), peer)
    ikm = _hkdf(auth_secret, ecdh, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)      # \x02 — останній (єдиний) запис
    return salt + struct.pack("!I", 4096) + bytes([len(as_public)]) + as_public + body


@dataclass
class Vapid:
    key: ec.EllipticCurvePrivateKey
    subject: str

    @classmethod
    def from_secret(cls, secret: str, subject: str) -> "Vapid":
        seed = _hkdf(b"productcart-vapid", (secret or "dev").encode(), b"vapid p-256 v1", 32)
        d = int.from_bytes(seed, "big") % (_N - 1) + 1
        return cls(ec.derive_private_key(d, ec.SECP256R1()), subject)

    @property
    def public_key(self) -> str:
        """applicationServerKey для pushManager.subscribe (base64url, 65 байт)."""
        return b64u(_raw_public(self.key.public_key()))

    def header(self, endpoint: str, ttl_h: int = 12) -> str:
        u = urlsplit(endpoint)
        claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + ttl_h * 3600, "sub": self.subject}
        signing = (b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." +
                   b64u(json.dumps(claims, separators=(",", ":")).encode()))
        r, s = decode_dss_signature(self.key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        jwt = signing + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={jwt}, k={self.public_key}"


class Gone(Exception):
    """Підписка більше не діє (браузер відписався) — її треба видалити."""


def send(vapid: Vapid, sub: dict, message: dict, *, ttl: int = 24 * 3600, client: httpx.Client | None = None) -> bool:
    """sub = {"endpoint", "p256dh", "auth"}; message = {"title", "body", "url", "tag"}. True — прийнято сервісом."""
    endpoint = sub["endpoint"]
    if not allowed_endpoint(endpoint):
        raise Gone("невідомий push-сервіс")
    body = encrypt(json.dumps(message, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    headers = {"Authorization": vapid.header(endpoint), "Content-Encoding": "aes128gcm", "TTL": str(ttl),
               "Content-Type": "application/octet-stream", "Urgency": "normal"}
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        r = client.post(endpoint, content=body, headers=headers)
    finally:
        if own:
            client.close()
    if r.status_code in (404, 410):
        raise Gone(f"{r.status_code}")
    if r.status_code >= 400:
        log.warning("Push: %s відповів %s: %s", urlsplit(endpoint).hostname, r.status_code, r.text[:200])
        return False
    return True
