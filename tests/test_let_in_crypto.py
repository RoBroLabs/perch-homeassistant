"""The let-in seal and the PIN hash, against protocol/vectors/let_in.json."""

import base64
import json

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import pytest

from custom_components.perch import crypto

from .schema import PROTOCOL

VEC = json.loads((PROTOCOL / "vectors" / "let_in.json").read_text())
IN, OUT = VEC["inputs"], VEC["outputs"]
BASE = json.loads((PROTOCOL / "vectors" / "crypto.json").read_text())
h = bytes.fromhex


def _keys():
    tv = X25519PrivateKey.from_private_bytes(h(IN["tv_private_key"]))
    server = X25519PrivateKey.from_private_bytes(h(IN["server_ephemeral_private_key"]))
    eph = X25519PrivateKey.from_private_bytes(h(IN["let_in_ephemeral_private_key"]))
    return tv, server, eph


def test_shared_inputs_match_crypto_vectors():
    for key in ("tv_private_key", "server_ephemeral_private_key", "challenge", "credential"):
        assert IN[key] == BASE["inputs"][key]
    assert OUT["credential_hash"] == BASE["outputs"]["credential_hash"]


def test_seal_reproduces_vector_step_by_step():
    tv, server, eph = _keys()
    tv_pub, server_pub = tv.public_key().public_bytes_raw(), server.public_key().public_bytes_raw()
    eph_pub = eph.public_key().public_bytes_raw()
    assert (tv_pub.hex(), server_pub.hex(), eph_pub.hex()) == (
        OUT["tv_public_key"], OUT["server_key"], OUT["let_in_ephemeral_public_key"],
    )
    shared = eph.exchange(server.public_key())
    assert shared.hex() == OUT["shared"]
    assert server.exchange(eph.public_key()) == shared
    static = tv.exchange(server.public_key())
    assert static.hex() == OUT["static_shared"]
    assert static == server.exchange(tv.public_key())
    assert (shared + static + h(OUT["credential_hash"])).hex() == OUT["ikm"]
    key = crypto.let_in_key(
        shared, static, h(OUT["credential_hash"]), h(IN["challenge"]), eph_pub, server_pub, tv_pub
    )
    assert key.hex() == OUT["key"]
    assert crypto.let_in_aad(h(IN["challenge"]), IN["command_id"]).hex() == OUT["aad"]
    sealed = crypto.seal_pin(
        IN["pin"], server_pub, h(IN["credential"]), h(IN["challenge"]), tv,
        IN["command_id"], ephemeral=eph, aead_nonce=h(IN["let_in_aead_nonce"]),
    )
    assert sealed.hex() == OUT["sealed_pin"]
    assert base64.b64encode(sealed).decode() == OUT["sealed_pin_base64"]
    assert len(sealed) == 76


def _open(sealed=None, cid=None, stored=None, challenge=None, tv_pub=None):
    tv, server, _ = _keys()
    return crypto.open_sealed_pin(
        server,
        stored or OUT["credential_hash"],
        challenge or h(IN["challenge"]),
        tv_pub or tv.public_key().public_bytes_raw(),
        cid or IN["command_id"],
        sealed or h(OUT["sealed_pin"]),
    )


def test_open_vector():
    assert _open() == IN["pin"]


def test_a_stolen_credential_hash_alone_cannot_seal():
    # Someone with the stored hash (a backup) but not the TV's private key.
    tv, server, eph = _keys()
    impostor = X25519PrivateKey.generate()
    sealed = crypto.seal_pin(
        IN["pin"], server.public_key().public_bytes_raw(), h(IN["credential"]),
        h(IN["challenge"]), impostor, IN["command_id"],
    )
    assert _open(sealed=sealed) is None


def test_open_fails_for_another_command_connection_tv_or_credential():
    assert _open(cid=12) is None
    assert _open(stored=BASE["inputs"]["wrong_stored_credential_hash"]) is None
    assert _open(challenge=b"\x01" * 32) is None
    assert _open(tv_pub=X25519PrivateKey.generate().public_key().public_bytes_raw()) is None


def test_open_fails_for_any_flipped_bit_or_wrong_length():
    sealed = bytearray(h(OUT["sealed_pin"]))
    for i in range(0, len(sealed), 7):
        bad = bytearray(sealed)
        bad[i] ^= 1
        assert _open(sealed=bytes(bad)) is None
    assert _open(sealed=bytes(sealed[:-1])) is None
    assert _open(sealed=bytes(sealed) + b"\0") is None


def test_low_order_ephemeral_key_is_refused():
    sealed = b"\0" * 32 + h(OUT["sealed_pin"])[32:]
    assert _open(sealed=sealed) is None


@pytest.mark.parametrize(
    ("pin", "ok"),
    [("123456", True), ("123456789012", True), ("12345", False),
     ("1234567890123", False), ("12345a", False), ("١٢٣٤٥٦", False), ("", False)],
)
def test_valid_pin(pin, ok):
    assert crypto.valid_pin(pin) is ok


def test_seal_refuses_a_bad_pin():
    tv, server, _ = _keys()
    with pytest.raises(ValueError):
        crypto.seal_pin("12", server.public_key().public_bytes_raw(), b"c" * 32, b"x" * 32,
                        tv, 1)


@pytest.mark.parametrize("cid", [0, 2**63])
def test_aad_refuses_ids_outside_the_range(cid):
    with pytest.raises(ValueError):
        crypto.let_in_aad(b"x" * 32, cid)


def test_pin_hash_is_salted_slow_and_checks():
    first, second = crypto.hash_pin("482916"), crypto.hash_pin("482916")
    assert first != second
    assert first.startswith("scrypt$16384$8$1$")
    assert "482916" not in first
    assert crypto.verify_pin("482916", first)
    assert not crypto.verify_pin("482917", first)
    assert not crypto.verify_pin("4829160", first)


@pytest.mark.parametrize(
    "stored",
    [None, "", "scrypt$x", "bcrypt$16384$8$1$AAAA$AAAA",
     "scrypt$1048576$8$1$c2FsdA==$" + base64.b64encode(b"\0" * 32).decode()],
)
def test_malformed_stored_hash_never_matches(stored):
    assert crypto.verify_pin("482916", stored) is False
