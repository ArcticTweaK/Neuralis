"""
neuralis.crypto
===============
Module 8 — Application-layer cryptography for Neuralis.

Public API surface:

    from neuralis.crypto.signing   import Signer, Verifier
    from neuralis.crypto.envelope  import SealedEnvelope, open_envelope, seal_envelope
    from neuralis.crypto.keystore  import CryptoKeyStore
    from neuralis.crypto.exchange  import KeyExchange, SharedSecret
    from neuralis.crypto.tokens    import CapabilityToken, verify_token

Everything in this package is pure application-layer — it sits above the
transport session (Module 2) and gives the agent/protocol layers a clean
API for signing, sealing, and verifying messages without touching raw
cryptographic primitives directly.
"""

from neuralis.crypto.signing  import Signer, Verifier, SignatureError
from neuralis.crypto.envelope import SealedEnvelope, seal_envelope, open_envelope, EnvelopeError
from neuralis.crypto.keystore import CryptoKeyStore, KeyRecord, KeyRotationError
from neuralis.crypto.exchange import KeyExchange, SharedSecret, ExchangeError
from neuralis.crypto.tokens   import CapabilityToken, issue_token, verify_token, TokenError

__all__ = [
    "Signer", "Verifier", "SignatureError",
    "SealedEnvelope", "seal_envelope", "open_envelope", "EnvelopeError",
    "CryptoKeyStore", "KeyRecord", "KeyRotationError",
    "KeyExchange", "SharedSecret", "ExchangeError",
    "CapabilityToken", "issue_token", "verify_token", "TokenError",
]
