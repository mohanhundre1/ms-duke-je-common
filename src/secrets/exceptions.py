"""Exceptions raised by the secrets package."""

from __future__ import annotations


class SecretProviderError(Exception):
    """Raised when a provider cannot be initialised or fails to communicate with its backend."""


class SecretNotFoundError(KeyError):
    """Raised by :func:`get` when a secret name cannot be resolved by the active provider."""

    def __init__(self, name: str, provider: str) -> None:
        super().__init__(name)
        self.name = name
        self.provider = provider

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Secret {self.name!r} not found via provider {self.provider!r}"