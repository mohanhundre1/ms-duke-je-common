"""Token exchange abstraction — local (dev) vs Azure OBO (production)."""

from __future__ import annotations

import os
import time
import uuid
from abc import ABC, abstractmethod


class TokenExchanger(ABC):
    """Exchange a user token for a service-to-service token."""

    @abstractmethod
    async def exchange(self, user_token: str, target_scope: str) -> str:
        """Exchange a user token for a service-to-service token."""
        ...


class LocalTokenExchanger(TokenExchanger):
    """Dev: mints a new HS256 JWT carrying user context."""

    async def exchange(self, user_token: str, target_scope: str) -> str:
        import jwt as pyjwt

        user_claims = pyjwt.decode(user_token, options={"verify_signature": False})
        payload = {
            "sub": user_claims.get("sub"),
            "email": user_claims.get("email"),
            "iss": "delegation-agent",
            "aud": target_scope.rsplit("/", 1)[0],  # e.g. api://6b4573ad-...
            "azp": "delegation-agent",
            "scp": target_scope.rsplit("/", 1)[-1],  # e.g. Process.Execute
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "jti": uuid.uuid4().hex,
        }
        secret = os.environ.get("JWT_SECRET", "test-local-secret-key-2026")
        return pyjwt.encode(payload, secret, algorithm="HS256")


class AzureOboTokenExchanger(TokenExchanger):
    """Production: uses MSAL OBO with existing SP credentials or AKS Workload Identity.

    Credential resolution:
    1. AZURE_FEDERATED_TOKEN_FILE set → use Workload Identity (K8s)
    2. AUTH_CLIENT_SECRET set → use client secret (local Docker dev)

    Uses the existing app registration from .env:
    AUTH_CLIENT_ID=b27418fb-fba1-4441-a660-21e580aa0ec9
    AUTH_TENANT_ID=5b973f99-77df-4beb-b27d-aa0c70b8482c
    """

    def __init__(self) -> None:
        self._app = None

    def _get_app(self):
        """Lazy-init MSAL ConfidentialClientApplication."""
        if self._app is not None:
            return self._app

        from msal import ConfidentialClientApplication

        federated_token_file = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
        if federated_token_file:
            credential = {"client_assertion": self._read_federated_token}
        else:
            credential = os.environ["AUTH_CLIENT_SECRET"]

        self._app = ConfidentialClientApplication(
            client_id=os.environ["AUTH_CLIENT_ID"],
            authority=(
                f"https://login.microsoftonline.com/{os.environ['AUTH_TENANT_ID']}"
            ),
            client_credential=credential,
        )
        return self._app

    async def exchange(self, user_token: str, target_scope: str) -> str:
        app = self._get_app()
        result = app.acquire_token_on_behalf_of(
            user_assertion=user_token,
            scopes=[target_scope],
        )
        if "error" in result:
            raise RuntimeError(
                f"OBO exchange failed: {result.get('error_description', result['error'])}"
            )
        return result["access_token"]

    @staticmethod
    def _read_federated_token() -> str:
        """Read the projected service-account token (refreshed by kubelet)."""
        token_file = os.environ["AZURE_FEDERATED_TOKEN_FILE"]
        with open(token_file) as f:
            return f.read()


def get_token_exchanger() -> TokenExchanger:
    """Factory: returns the correct exchanger based on AUTH_PROVIDER env var."""
    provider = os.environ.get("AUTH_PROVIDER", "local")
    if provider == "entra_id":
        return AzureOboTokenExchanger()
    return LocalTokenExchanger()