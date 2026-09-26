import base64
import hashlib
import hmac

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

import bot.binance as binance


def test_hmac_signing_remains_compatible(monkeypatch):
    payload = "symbol=BTCUSDT&timestamp=123&recvWindow=5000"
    secret = "unit-test-secret"
    monkeypatch.setattr(binance, "SIGNING_METHOD", "hmac")
    monkeypatch.setattr(binance, "API_SECRET", secret)

    expected = hmac.new(
        secret.encode(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    assert binance._sign_payload(payload) == expected


def test_ed25519_signing_produces_verifiable_base64(monkeypatch):
    payload = "symbol=BTCUSDT&timestamp=123&recvWindow=5000"
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    )

    monkeypatch.setattr(binance, "SIGNING_METHOD", "ed25519")
    monkeypatch.setattr(
        binance,
        "ED25519_PRIVATE_KEY_B64",
        base64.b64encode(pem).decode("ascii"),
    )
    monkeypatch.setattr(binance, "ED25519_PRIVATE_KEY_PEM", "")
    monkeypatch.setattr(binance, "_ED25519_PRIVATE_KEY_CACHE", None)

    signature = base64.b64decode(binance._sign_payload(payload), validate=True)
    private_key.public_key().verify(signature, payload.encode("ascii"))


def test_ed25519_missing_private_key_fails_closed(monkeypatch):
    monkeypatch.setattr(binance, "SIGNING_METHOD", "ed25519")
    monkeypatch.setattr(binance, "ED25519_PRIVATE_KEY_B64", "")
    monkeypatch.setattr(binance, "ED25519_PRIVATE_KEY_PEM", "")
    monkeypatch.setattr(binance, "_ED25519_PRIVATE_KEY_CACHE", None)

    try:
        binance._sign_payload("timestamp=123")
    except RuntimeError as exc:
        assert str(exc) == "BINANCE_ED25519_PRIVATE_KEY_UNAVAILABLE"
    else:
        raise AssertionError("Ed25519 signer must fail closed without a private key")

def test_signing_credential_gate_accepts_ed25519_without_hmac_secret(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    )

    monkeypatch.setattr(binance, "API_KEY", "unit-test-api-key")
    monkeypatch.setattr(binance, "API_SECRET", "")
    monkeypatch.setattr(binance, "SIGNING_METHOD", "ed25519")
    monkeypatch.setattr(binance, "ED25519_PRIVATE_KEY_B64", "")
    monkeypatch.setattr(binance, "ED25519_PRIVATE_KEY_PEM", pem.decode("utf-8"))
    monkeypatch.setattr(binance, "_ED25519_PRIVATE_KEY_CACHE", None)

    binance._assert_signing_credentials_available()


def test_signing_credential_gate_requires_hmac_secret(monkeypatch):
    monkeypatch.setattr(binance, "API_KEY", "unit-test-api-key")
    monkeypatch.setattr(binance, "API_SECRET", "")
    monkeypatch.setattr(binance, "SIGNING_METHOD", "hmac")

    try:
        binance._assert_signing_credentials_available()
    except RuntimeError as exc:
        assert str(exc) == "BINANCE_API_SECRET_UNAVAILABLE"
    else:
        raise AssertionError("HMAC signer must fail closed without API secret")

