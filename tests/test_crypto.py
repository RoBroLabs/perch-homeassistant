"""Vector tests for custom_components.perch.crypto, from protocol/vectors/crypto.json."""

import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from custom_components.perch import crypto
from custom_components.perch.const import CRED_LABEL

from .schema import PROTOCOL

VEC = json.loads((PROTOCOL / "vectors" / "crypto.json").read_text())
IN, OUT = VEC["inputs"], VEC["outputs"]
WORDS = json.loads((PROTOCOL / "words.json").read_text())["words"]
h = bytes.fromhex


def _open(envelope: bytes, tv_priv: X25519PrivateKey, nonce: bytes) -> bytes:
    eph_pub, n, ct = envelope[:32], envelope[32:44], envelope[44:]
    tv_pub = tv_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = tv_priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=nonce,
        info=CRED_LABEL + eph_pub + tv_pub,
    ).derive(shared)
    return ChaCha20Poly1305(key).decrypt(n, ct, tv_pub)


def _tv_priv():
    return X25519PrivateKey.from_private_bytes(h(IN["tv_private_key"]))


def test_vector_public_keys_derive_from_private_keys():
    # Sanity: the vector's TV public key is what the TV private key yields.
    assert _tv_priv().public_key().public_bytes_raw() == h(OUT["tv_public_key"])


def test_binding_vector():
    pub, nonce = h(OUT["tv_public_key"]), h(IN["pairing_nonce"])
    mac = crypto.binding_hmac(IN["code"], pub, nonce)
    assert mac == h(OUT["binding_hmac"])
    assert crypto.verify_binding(IN["code"], pub, nonce, mac)


def test_wrong_code_fails():
    assert not crypto.verify_binding(
        "000000", h(OUT["tv_public_key"]), h(IN["pairing_nonce"]), h(OUT["binding_hmac"])
    )


def test_swapped_key_fails():
    other = X25519PrivateKey.generate().public_key().public_bytes_raw()
    assert not crypto.verify_binding(
        IN["code"], other, h(IN["pairing_nonce"]), h(OUT["binding_hmac"])
    )


def test_fingerprint_vector_with_real_word_list():
    assert len(WORDS) == 1296
    words = crypto.fingerprint_words(h(OUT["tv_public_key"]), WORDS)
    assert " ".join(words) == OUT["fingerprint_words"]


def test_shipped_word_list_is_the_protocol_list():
    shipped = json.loads(
        (Path(crypto.__file__).parent / "words.json").read_text()
    )["words"]
    assert shipped == WORDS


def test_envelope_vector_byte_for_byte():
    env = crypto.seal_credential(
        h(IN["credential"]),
        h(OUT["tv_public_key"]),
        h(IN["pairing_nonce"]),
        ephemeral=X25519PrivateKey.from_private_bytes(
            h(IN["companion_ephemeral_private_key"])
        ),
        aead_nonce=h(IN["envelope_aead_nonce"]),
    )
    assert env.hex() == OUT["envelope"]
    assert env[:32].hex() == OUT["companion_ephemeral_public_key"]
    assert len(env) == 32 + 12 + 32 + 16


def test_envelope_opens_with_tv_key_only():
    env = h(OUT["envelope"])
    assert _open(env, _tv_priv(), h(IN["pairing_nonce"])) == h(IN["credential"])
    with pytest.raises(InvalidTag):
        _open(env, X25519PrivateKey.generate(), h(IN["pairing_nonce"]))


def test_credential_hash_vector():
    assert crypto.credential_hash(h(IN["credential"])) == OUT["credential_hash"]


def test_random_envelope_round_trip():
    tv = X25519PrivateKey.generate()
    pub = tv.public_key().public_bytes_raw()
    cred, nonce = crypto.new_credential(), b"n" * 16
    assert len(cred) == 32
    assert _open(crypto.seal_credential(cred, pub, nonce), tv, nonce) == cred


def test_credential_matches():
    cred = crypto.new_credential()
    stored = crypto.credential_hash(cred)
    assert len(stored) == 64
    assert crypto.credential_matches(cred, stored)
    assert not crypto.credential_matches(crypto.new_credential(), stored)
    assert not crypto.credential_matches(cred, None)


# --- auth proof v2 ------------------------------------------------------------


def seal_auth_proof(
    tv_priv: X25519PrivateKey,
    server_pub: bytes,
    stored_hash_hex: str,
    challenge: bytes,
    credential: bytes,
    nonce: bytes | None = None,
) -> bytes:
    """Test-only: what the TV sends. nonce(12)||ciphertext||tag(16)."""
    tv_pub = tv_priv.public_key().public_bytes_raw()
    shared = tv_priv.exchange(X25519PublicKey.from_public_bytes(server_pub))
    key = crypto.auth_key(
        shared, bytes.fromhex(stored_hash_hex), challenge, server_pub, tv_pub
    )
    n = nonce or os.urandom(12)
    return n + ChaCha20Poly1305(key).encrypt(n, credential, challenge)


def _server_priv():
    return X25519PrivateKey.from_private_bytes(h(IN["server_ephemeral_private_key"]))


def test_auth_server_key_vector():
    assert _server_priv().public_key().public_bytes_raw() == h(
        OUT["server_ephemeral_public_key"]
    )


def test_auth_proof_vector_byte_for_byte():
    proof = seal_auth_proof(
        _tv_priv(),
        h(OUT["server_ephemeral_public_key"]),
        OUT["credential_hash"],
        h(IN["challenge"]),
        h(IN["credential"]),
        nonce=h(IN["auth_aead_nonce"]),
    )
    assert proof.hex() == OUT["auth_proof"]


def test_auth_proof_vector_opens():
    assert crypto.open_auth_proof(
        _server_priv(),
        h(OUT["tv_public_key"]),
        OUT["credential_hash"],
        h(IN["challenge"]),
        h(OUT["auth_proof"]),
    )


def test_auth_proof_wrong_stored_hash_fails_to_open():
    assert OUT["auth_proof_wrong_hash_expect"] == "open_fails"
    args = (_server_priv(), h(OUT["tv_public_key"]))
    assert not crypto.open_auth_proof(
        *args, IN["wrong_stored_credential_hash"], h(IN["challenge"]), h(OUT["auth_proof"])
    )


def test_auth_proof_other_failures():
    base = dict(
        server_private=_server_priv(),
        tv_pub=h(OUT["tv_public_key"]),
        stored_hash=OUT["credential_hash"],
        challenge=h(IN["challenge"]),
        proof=h(OUT["auth_proof"]),
    )
    assert crypto.open_auth_proof(**base)
    flipped = bytearray(base["proof"])
    flipped[20] ^= 1
    assert not crypto.open_auth_proof(**{**base, "proof": bytes(flipped)})
    assert not crypto.open_auth_proof(**{**base, "challenge": b"x" * 32})
    assert not crypto.open_auth_proof(**{**base, "proof": b"short"})
    assert not crypto.open_auth_proof(**{**base, "tv_pub": None})
    assert not crypto.open_auth_proof(**{**base, "stored_hash": None})
    assert not crypto.open_auth_proof(**{**base, "stored_hash": "zz"})
    other = X25519PrivateKey.generate().public_key().public_bytes_raw()
    assert not crypto.open_auth_proof(**{**base, "tv_pub": other})
