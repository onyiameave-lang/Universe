"""Ed25519 verification and durable single-use nonce consumption for Aegis.

This verifier is deliberately configuration-driven. With no trusted issuer
public keys configured, authorization fails closed; the verifier never creates
or trusts signing keys itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger("forge.authorization")


def _decode_base64url(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


class Ed25519AuthorizationVerifier:
    """Verify signed Pydantic authorization artifacts and consume their nonce.

    Public key values may be raw 32-byte keys or base64url strings. Signatures
    must be base64url-encoded Ed25519 signatures over the canonical UTF-8 JSON
    representation returned by ``signature_payload``.
    """

    def __init__(
        self,
        trusted_issuers: Mapping[str, Union[bytes, str]],
        audience: str,
        database_path: str | Path | None = None,
        max_token_age_seconds: int = 300,
    ):
        if not audience.strip():
            raise ValueError("audience must be non-empty")
        if max_token_age_seconds <= 0:
            raise ValueError("max_token_age_seconds must be positive")
        if any(not isinstance(issuer, str) or not issuer.strip() for issuer in trusted_issuers):
            raise ValueError("trusted issuer identifiers must be non-empty strings")
        self._audience = audience
        self._max_token_age_seconds = max_token_age_seconds
        self._keys = {
            issuer: self._load_public_key(key)
            for issuer, key in trusted_issuers.items()
        }
        self._database_path = (
            Path(database_path).resolve()
            if database_path is not None
            else Path(__file__).resolve().parents[1] / "security" / "authorization_nonces.sqlite3"
        )
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize_database()

    @staticmethod
    def _load_public_key(value: Union[bytes, str]) -> Ed25519PublicKey:
        key_bytes = _decode_base64url(value) if isinstance(value, str) else value
        if not isinstance(key_bytes, bytes) or len(key_bytes) != 32:
            raise ValueError("Ed25519 public keys must be exactly 32 raw bytes")
        return Ed25519PublicKey.from_public_bytes(key_bytes)

    @staticmethod
    def signature_payload(authorization: Any) -> bytes:
        """Canonical payload Aegis must sign; signature itself is excluded."""
        if not hasattr(authorization, "model_dump"):
            raise TypeError("authorization must be a Pydantic model")
        payload = authorization.model_dump(mode="json", exclude={"signature"})
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS authorization_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS consumed_authorizations "
                "(issuer TEXT NOT NULL, nonce TEXT NOT NULL, authorization_id TEXT NOT NULL UNIQUE, "
                "audience TEXT NOT NULL, consumed_at TEXT NOT NULL, claims_digest TEXT NOT NULL, "
                "token_digest TEXT NOT NULL, PRIMARY KEY (issuer, nonce))"
            )
            connection.execute("BEGIN IMMEDIATE")
            schema = connection.execute(
                "SELECT value FROM authorization_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if schema is not None and schema["value"] != "1":
                raise RuntimeError(
                    f"Unsupported authorization nonce database schema: {schema['value']}"
                )
            if schema is None:
                connection.execute(
                    "INSERT INTO authorization_metadata (key, value) "
                    "VALUES ('schema_version', '1')"
                )
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Authorization nonce database integrity failed: {integrity}")
            connection.commit()

    def verify_and_consume(self, authorization: Any, required_claims: Dict[str, Any]) -> bool:
        """Return false on any bad claim, signature, replay, or persistence error."""
        try:
            issuer = authorization.issuer
            nonce = authorization.nonce
            auth_id = authorization.authorization_id
            signature = authorization.signature
            issued_at = authorization.issued_at
            expires_at = authorization.expires_at
            if not all(isinstance(value, str) and value.strip()
                       for value in (issuer, nonce, auth_id, signature)):
                return False
            if issuer not in self._keys:
                return False
            if getattr(authorization, "audience", None) != self._audience:
                return False
            if getattr(authorization, "decision", None) != "AUTHORIZED":
                return False
            if authorization.claims != required_claims:
                return False
            if not isinstance(issued_at, datetime) or not isinstance(expires_at, datetime):
                return False
            if (
                issued_at.tzinfo is None or issued_at.utcoffset() is None
                or expires_at.tzinfo is None or expires_at.utcoffset() is None
            ):
                return False
            now = datetime.now(timezone.utc)
            age = (now - issued_at.astimezone(timezone.utc)).total_seconds()
            if age < 0 or age > self._max_token_age_seconds:
                return False
            if expires_at.astimezone(timezone.utc) <= now:
                return False
            if expires_at <= issued_at:
                return False
            lifetime = (expires_at - issued_at).total_seconds()
            if lifetime > self._max_token_age_seconds:
                return False

            payload = self.signature_payload(authorization)
            signature_bytes = _decode_base64url(signature)
            if len(signature_bytes) != 64:
                return False
            self._keys[issuer].verify(signature_bytes, payload)
            claims_digest = hashlib.sha256(
                json.dumps(required_claims, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
            ).hexdigest()
            token_digest = hashlib.sha256(payload + signature_bytes).hexdigest()

            with self._lock, closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                # Recheck expiry after signature verification and before nonce use.
                if datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc):
                    connection.rollback()
                    return False
                connection.execute(
                    "INSERT INTO consumed_authorizations "
                    "(issuer, nonce, authorization_id, audience, consumed_at, "
                    "claims_digest, token_digest) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (issuer, nonce, auth_id, self._audience,
                     datetime.now(timezone.utc).isoformat(), claims_digest, token_digest),
                )
                connection.commit()
            return True
        except (InvalidSignature, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
            log.warning("authorization rejected (%s)", type(exc).__name__)
            return False
        except Exception as exc:
            log.error("authorization verification failed (%s)", type(exc).__name__)
            return False
