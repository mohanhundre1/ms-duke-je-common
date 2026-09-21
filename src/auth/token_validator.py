"""Inbound token validation for domain agents."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod


class TokenValidator(ABC):
    """Validate bearer tokens on inbound requests."""

    @abstractmethod
    def validate(self, token: str) -> dict:
        """Validate bearer token, return claims or raise."""
        ...


class LocalTokenValidator(TokenValidator):
    """Dev: validates HS256 tokens signed with shared JWT_SECRET.

    In local mode the shared JWT_SECRET provides trust. Audience is
    NOT validated because inter-service token forwarding means the
    token's ``aud`` is set to the upstream service (router/delegation
    agent), not the downstream agent receiving the call. Signature
    verification via the shared secret is sufficient for trust.
    """

    def validate(self, token: str) -> dict:
        import jwt as pyjwt

        secret = os.environ.get("JWT_SECRET", "test-local-secret-key-2026")
        payload = pyjwt.decode(
            token, secret, algorithms=["HS256"],
            options={"verify_aud": False},
        )
        return payload


class EntraTokenValidator(TokenValidator):
    """Production: validates RS256 tokens via Entra JWKS endpoint.

    Supports dual AAD app-registrations for scenarios where tokens can
    arrive from either the primary or a secondary caller (e.g. a partner
    service, a companion CLI, or a phased migration between two app
    registrations).

    Required env vars (primary):
        AUTH_TENANT_ID
        AZURE_VALID_AUDIENCE
        AZURE_VALID_ISSUER
        AUTH_CLIENT_ID

    Optional env vars (secondary — enables dual validation when set):
        AUTH_CLIENT_ID_SECONDARY
        AZURE_VALID_AUDIENCE_SECONDARY

    Validation flow: the token's ``aud`` claim is inspected first. If it
    matches the secondary audience, we validate against that; otherwise
    we try the primary audience and fall back to secondary on an
    ``InvalidAudienceError``.
    """

    def __init__(self) -> None:
        import logging as _logging
        _log = _logging.getLogger(__name__)
        from jwt import PyJWKClient

        tenant_id = os.environ.get("AUTH_TENANT_ID", "")
        _log.info(
            "[EntraTokenValidator] AUTH_TENANT_ID=%s AUTH_CLIENT_ID=%s "
            "AUTH_CLIENT_ID_SECONDARY=%s AZURE_TENANT_ID=%s",
            tenant_id or "<not set>",
            os.environ.get("AUTH_CLIENT_ID", "<not set>"),
            os.environ.get("AUTH_CLIENT_ID_SECONDARY", "<not set>"),
            os.environ.get("AZURE_TENANT_ID", "<not set>"),
        )
        if not tenant_id:
            raise KeyError(
                "AUTH_TENANT_ID is not set. Check that SECRET_PROVIDER=keyvault "
                "and AZURE_KEYVAULT_URL are configured, and that the MSI has "
                "'Key Vault Secrets User' role on the vault."
            )
        self._jwks = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
        )
        # Primary AAD app-registration
        self._audience = os.environ.get(
            "AZURE_VALID_AUDIENCE",
            "api://6b4573ad-bfe3-4d57-8b9d-9b7e9fbe479f",
        )
        self._issuer = os.environ.get(
            "AZURE_VALID_ISSUER",
            f"https://sts.windows.net/{tenant_id}/",
        )
        self._allowed_azp = [
            x.strip()
            for x in os.environ.get("AUTH_CLIENT_ID", "").split(",")
            if x.strip()
        ]

        # Secondary AAD app-registration (optional). Empty string means
        # dual validation is disabled and only the primary is checked.
        self._audience_secondary = os.environ.get(
            "AZURE_VALID_AUDIENCE_SECONDARY", ""
        ).strip()
        self._allowed_azp_secondary = [
            x.strip()
            for x in os.environ.get("AUTH_CLIENT_ID_SECONDARY", "").split(",")
            if x.strip()
        ]

    def _decode_with_audience(
        self,
        token: str,
        signing_key,
        audience: str,
    ) -> dict:
        """Decode a token against a specific expected audience.

        Split out from :meth:`validate` so both the primary and secondary
        validation paths share exactly the same PyJWT options.
        """
        import jwt as pyjwt

        return pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=self._issuer,
        )

    def validate(self, token: str) -> dict:
        import logging
        import jwt as pyjwt

        _log = logging.getLogger(__name__)

        # Log the expected audience(s) from KV/env for debugging
        _log.info(
            "EntraTokenValidator: expected_audience=%s "
            "expected_audience_secondary=%s issuer=%s tenant_id=%s",
            self._audience,
            self._audience_secondary or "<disabled>",
            self._issuer,
            os.environ.get("AUTH_TENANT_ID", "?"),
        )

        # Peek at token's aud/iss/sub claim before validation so we can
        # pick the right expected audience in one pass and log a
        # diagnostic trail even if validation later fails.
        unverified_aud: str = ""
        try:
            unverified = pyjwt.decode(
                token, options={"verify_signature": False, "verify_aud": False}
            )
            unverified_aud = str(unverified.get("aud") or "")
            _log.info(
                "Token claims (pre-validation): aud=%s iss=%s sub=%s appid=%s",
                unverified_aud,
                unverified.get("iss"),
                unverified.get("sub"),
                unverified.get("appid"),
            )
        except Exception:
            pass

        signing_key = self._jwks.get_signing_key_from_jwt(token)

        # Build the ordered list of audiences to try. When the token's
        # ``aud`` claim matches the secondary, put secondary first to
        # avoid the guaranteed InvalidAudienceError on the primary.
        audiences: list[tuple[str, str]] = []
        if (
            self._audience_secondary
            and unverified_aud == self._audience_secondary
        ):
            audiences.append(("secondary", self._audience_secondary))
            audiences.append(("primary", self._audience))
        else:
            audiences.append(("primary", self._audience))
            if self._audience_secondary:
                audiences.append(("secondary", self._audience_secondary))

        last_exc: Exception | None = None
        for source, aud in audiences:
            if not aud:
                continue
            try:
                payload = self._decode_with_audience(token, signing_key, aud)
                _log.info(
                    "Token validated (%s AAD app): sub=%s appid=%s aud=%s",
                    source,
                    payload.get("sub"),
                    payload.get("appid"),
                    payload.get("aud"),
                )
                return payload
            except pyjwt.InvalidAudienceError as exc:
                _log.info(
                    "Token audience did not match %s app (expected=%s); "
                    "will try next configured audience.",
                    source,
                    aud,
                )
                last_exc = exc
                continue

        # All audiences failed. Re-raise the most recent InvalidAudienceError
        # so the caller sees the standard PyJWT exception type.
        raise last_exc or pyjwt.InvalidAudienceError(
            "Token audience did not match any configured AAD app registration."
        )


def get_token_validator() -> TokenValidator:
    """Factory: returns the correct validator based on AUTH_PROVIDER env var."""
    provider = os.environ.get("AUTH_PROVIDER", "local")
    if provider == "entra_id":
        return EntraTokenValidator()
    return LocalTokenValidator()
