from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import asdict, dataclass
from datetime import timedelta

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MESSAGE_RETENTION = timedelta(hours=48)
MAX_MESSAGE_BYTES = 32 * 1024
_FORMAT_VERSION = b"\x01"
_NONCE_BYTES = 12


@dataclass(frozen=True, slots=True)
class MessageContext:
    connection_id: int
    contact_id: int
    tg_message_id: int | None
    direction: str

    def associated_data(self) -> bytes:
        if self.direction not in {"in", "out"}:
            raise ValueError("direction must be 'in' or 'out'")
        return json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")


class MessageCipher:
    """Versioned AES-256-GCM envelope with row identity as authenticated data."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("message encryption key must be exactly 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_encoded_key(cls, encoded: str) -> MessageCipher:
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            key = base64.urlsafe_b64decode(padded.encode("ascii"))
        except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
            raise ValueError("invalid URL-safe Base64 encryption key") from exc
        return cls(key)

    @staticmethod
    def generate_encoded_key() -> str:
        return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")

    def encrypt(self, text: str, *, context: MessageContext) -> bytes:
        plaintext = text.encode("utf-8")
        if not plaintext:
            raise ValueError("message text must not be empty")
        if len(plaintext) > MAX_MESSAGE_BYTES:
            raise ValueError("message text exceeds retention limit")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext, context.associated_data())
        return _FORMAT_VERSION + nonce + ciphertext

    def decrypt(self, envelope: bytes, *, context: MessageContext) -> str:
        if len(envelope) <= 1 + _NONCE_BYTES or envelope[:1] != _FORMAT_VERSION:
            raise ValueError("unsupported encrypted message envelope")
        nonce = envelope[1 : 1 + _NONCE_BYTES]
        ciphertext = envelope[1 + _NONCE_BYTES :]
        try:
            plaintext = self._cipher.decrypt(nonce, ciphertext, context.associated_data())
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise ValueError("encrypted message authentication failed") from exc
