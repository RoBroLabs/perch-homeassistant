"""Pairing, auth and let-in crypto for the Perch companion. Uses only `cryptography`.

Checked byte for byte against the Perch protocol vectors in protocol/vectors/crypto.json. Nothing here logs or returns a secret
other than to its caller.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
import hashlib
import hmac
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .const import (
    AUTH_LABEL,
    BIND_LABEL,
    CRED_LABEL,
    FP_LABEL,
    LET_IN_LABEL,
    LET_IN_PIN_MAX,
    LET_IN_PIN_MIN,
    LET_IN_PLAIN_LEN,
    LET_IN_SEALED_LEN,
)

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


# --- let-in PIN ---------------------------------------------------------------

PIN_PATTERN = re.compile(rf"^[0-9]{{{LET_IN_PIN_MIN},{LET_IN_PIN_MAX}}}$")
# scrypt cost for the stored PIN hash: about 16 MB and tens of milliseconds a check.
PIN_N, PIN_R, PIN_P = 2**14, 8, 1
_PIN_MAX_N = 2**17


def valid_pin(pin: str) -> bool:
    """6 to 12 ASCII digits."""
    return isinstance(pin, str) and PIN_PATTERN.match(pin) is not None


def _scrypt(pin: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pin, salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024 * 1024)


def hash_pin(pin: str, *, salt: bytes | None = None) -> str:
    """`scrypt$n$r$p$salt$hash` (base64 parts). Slow: run it in an executor."""
    salt = salt or os.urandom(16)
    digest = _scrypt(pin.encode(), salt, PIN_N, PIN_R, PIN_P)
    return "$".join(
        (
            "scrypt",
            str(PIN_N),
            str(PIN_R),
            str(PIN_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


_DUMMY_PIN_PARAMS = (b"perch-dummy-salt", PIN_N, PIN_R, PIN_P, b"\0" * 32)


def _parse_pin_hash(stored: str | None) -> tuple[bytes, int, int, int, bytes] | None:
    try:
        kind, n, r, p, salt, digest = (stored or "").split("$")
        params = (
            base64.b64decode(salt, validate=True),
            int(n),
            int(r),
            int(p),
            base64.b64decode(digest, validate=True),
        )
    except (ValueError, binascii.Error):
        return None
    if kind != "scrypt" or not 2 <= params[1] <= _PIN_MAX_N or params[2] > 16 or params[3] > 4:
        return None
    if len(params[4]) != 32:
        return None
    return params


def verify_pin(pin: str | None, stored: str | None) -> bool:
    """Constant-time check of `pin` against a stored hash. A missing or malformed
    PIN or hash still runs one scrypt, against dummy values, and returns False.
    Slow: run it in an executor."""
    params = _parse_pin_hash(stored)
    good = params is not None and valid_pin(pin or "")
    salt, n, r, p, expected = params if params is not None else _DUMMY_PIN_PARAMS
    candidate = (pin if good and pin else "000000").encode()
    matches = hmac.compare_digest(_scrypt(candidate, salt, n, r, p), expected)
    return bool(matches and good)


def let_in_key(
    shared: bytes,
    static_shared: bytes,
    stored_hash: bytes,
    challenge: bytes,
    eph_pub: bytes,
    server_key: bytes,
    tv_pub: bytes,
) -> bytes:
    """HKDF(ikm=shared||static_shared||hash, salt=challenge,
    info=label||ephPub||server_key||tvPub). `shared` is X25519(eph, server_key);
    `static_shared` is X25519(tv static, server_key), so only the TV's own key
    pair and credential together can seal."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=challenge,
        info=LET_IN_LABEL + eph_pub + server_key + tv_pub,
    ).derive(shared + static_shared + stored_hash)


def let_in_aad(challenge: bytes, command_id: int) -> bytes:
    """label || challenge || uint64_be(command id). The id must be 1 to 2^63-1."""
    if not 0 < command_id < 2**63:
        raise ValueError("command id")
    return LET_IN_LABEL + challenge + command_id.to_bytes(8, "big")


def seal_pin(
    pin: str,
    server_key: bytes,
    credential: bytes,
    challenge: bytes,
    tv_private: X25519PrivateKey,
    command_id: int,
    *,
    ephemeral: X25519PrivateKey | None = None,
    aead_nonce: bytes | None = None,
) -> bytes:
    """The TV's side, for tests and vectors: ephPub(32)||nonce(12)||ct(16)||tag(16)."""
    if not valid_pin(pin):
        raise ValueError("pin")
    eph = ephemeral or X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    server = X25519PublicKey.from_public_bytes(server_key)
    shared = eph.exchange(server)
    static_shared = tv_private.exchange(server)
    tv_pub = tv_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key = let_in_key(
        shared,
        static_shared,
        hashlib.sha256(credential).digest(),
        challenge,
        eph_pub,
        server_key,
        tv_pub,
    )
    plain = pin.encode().ljust(LET_IN_PLAIN_LEN, b"\0")
    n = aead_nonce or os.urandom(12)
    return eph_pub + n + ChaCha20Poly1305(key).encrypt(
        n, plain, let_in_aad(challenge, command_id)
    )


def open_sealed_pin(
    server_private: X25519PrivateKey,
    stored_hash: str,
    challenge: bytes,
    tv_pub: bytes,
    command_id: int,
    sealed: bytes,
) -> str | None:
    """The PIN inside `sealed`, or None if it does not open or is not a PIN.

    None means the frame was not made by this TV for this connection and command;
    it says nothing about the PIN.
    """
    if len(sealed) != LET_IN_SEALED_LEN:
        return None
    try:
        hash_raw = bytes.fromhex(stored_hash)
    except ValueError:
        return None
    if len(hash_raw) != 32:
        return None
    eph_pub, nonce, body = sealed[:32], sealed[32:44], sealed[44:]
    server_pub = server_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    try:
        shared = server_private.exchange(X25519PublicKey.from_public_bytes(eph_pub))
        static_shared = server_private.exchange(X25519PublicKey.from_public_bytes(tv_pub))
        key = let_in_key(
            shared, static_shared, hash_raw, challenge, eph_pub, server_pub, tv_pub
        )
        plain = ChaCha20Poly1305(key).decrypt(
            nonce, body, let_in_aad(challenge, command_id)
        )
    except (InvalidTag, ValueError):
        return None
    pin = plain.rstrip(b"\0")
    try:
        text = pin.decode("ascii")
    except UnicodeDecodeError:
        return None
    return text if valid_pin(text) else None
