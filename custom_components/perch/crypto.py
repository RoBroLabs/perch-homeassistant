"""Pairing crypto for the Perch companion. Uses only `cryptography`.

Checked byte for byte against the Perch protocol vectors in protocol/vectors/crypto.json. Nothing here logs or returns a secret
other than to its caller.
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .const import AUTH_LABEL, BIND_LABEL, CRED_LABEL, FP_LABEL

_DUMMY_HASH = hashlib.sha256(b"perch-dummy-credential").hexdigest()


def binding_hmac(code: str, tv_pub: bytes, nonce: bytes) -> bytes:
    """HMAC-SHA256(key=code, msg=BIND_LABEL||tv_pub||nonce)."""
    return hmac.new(code.encode(), BIND_LABEL + tv_pub + nonce, hashlib.sha256).digest()


def verify_binding(code: str, tv_pub: bytes, nonce: bytes, mac: bytes) -> bool:
    """Constant-time check of a binding HMAC."""
    return hmac.compare_digest(binding_hmac(code, tv_pub, nonce), mac)


def fingerprint_words(tv_pub: bytes, words: Sequence[str]) -> list[str]:
    """Four words from SHA-256(FP_LABEL||tv_pub), 2 big-endian bytes each mod len(words)."""
    digest = hashlib.sha256(FP_LABEL + tv_pub).digest()
    return [
        words[int.from_bytes(digest[2 * i : 2 * i + 2], "big") % len(words)]
        for i in range(4)
    ]


def new_credential() -> bytes:
    """A fresh 32-byte TV credential."""
    return os.urandom(32)


def credential_hash(credential: bytes) -> str:
    """SHA-256 hex digest stored instead of the credential."""
    return hashlib.sha256(credential).hexdigest()


def credential_matches(credential: bytes, stored_hash: str | None) -> bool:
    """Constant-time compare; uses a dummy hash when nothing is stored."""
    expected = _DUMMY_HASH if stored_hash is None else stored_hash
    matches = hmac.compare_digest(credential_hash(credential), expected)
    return matches and stored_hash is not None


_DUMMY_PUB = (
    X25519PrivateKey.from_private_bytes(hashlib.sha256(b"perch-dummy-tv").digest())
    .public_key()
    .public_bytes(Encoding.Raw, PublicFormat.Raw)
)


def auth_key(
    shared: bytes, stored_hash: bytes, challenge: bytes, server_key: bytes, tv_pub: bytes
) -> bytes:
    """Auth proof v2 key: HKDF(ikm=shared||hash, salt=challenge, info=label||server_key||tv_pub)."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=challenge,
        info=AUTH_LABEL + server_key + tv_pub,
    ).derive(shared + stored_hash)


def open_auth_proof(
    server_private: X25519PrivateKey,
    tv_pub: bytes | None,
    stored_hash: str | None,
    challenge: bytes,
    proof: bytes,
) -> bool:
    """Open a v2 auth proof; True only if it opens and hashes to the stored hash.

    An unknown TV (`tv_pub` or `stored_hash` None, or a malformed stored value)
    runs the same steps against dummy values and always returns False.
    """
    known = tv_pub is not None and stored_hash is not None
    try:
        hash_raw = bytes.fromhex(stored_hash) if stored_hash is not None else b""
    except ValueError:
        hash_raw = b""
    if len(hash_raw) != 32:
        known = False
        hash_raw = bytes.fromhex(_DUMMY_HASH)
    pub = tv_pub if known and tv_pub is not None else _DUMMY_PUB
    server_pub = server_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    try:
        shared = server_private.exchange(X25519PublicKey.from_public_bytes(pub))
    except ValueError:
        known = False
        shared = server_private.exchange(X25519PublicKey.from_public_bytes(_DUMMY_PUB))
        pub = _DUMMY_PUB
    key = auth_key(shared, hash_raw, challenge, server_pub, pub)
    nonce, body = proof[:12], proof[12:]
    try:
        if len(proof) < 12 + 16:
            raise InvalidTag
        credential = ChaCha20Poly1305(key).decrypt(nonce, body, challenge)
    except InvalidTag:
        credential = b""
    matches = hmac.compare_digest(hashlib.sha256(credential).digest(), hash_raw)
    return bool(matches and known and credential)


def seal_credential(
    credential: bytes,
    tv_pub: bytes,
    nonce: bytes,
    *,
    ephemeral: X25519PrivateKey | None = None,
    aead_nonce: bytes | None = None,
) -> bytes:
    """Return ephPub(32)||nonce(12)||ciphertext||tag(16).

    `ephemeral` and `aead_nonce` are injectable for tests only.
    """
    eph = ephemeral or X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(tv_pub))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=nonce,
        info=CRED_LABEL + eph_pub + tv_pub,
    ).derive(shared)
    n = aead_nonce or os.urandom(12)
    return eph_pub + n + ChaCha20Poly1305(key).encrypt(n, credential, tv_pub)
