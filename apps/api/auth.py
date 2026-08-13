from __future__ import annotations

import hashlib
import hmac

from pydantic import Field

from domain.models import ContractModel


class AuthenticationError(PermissionError):
    pass


class AdminPrincipal(ContractModel):
    subject: str = Field(min_length=1, max_length=100)


class AdminAuthenticator:
    """Authenticate the single-machine operator without comparing raw token lengths."""

    def __init__(self, token: str) -> None:
        if len(token) < 32:
            raise ValueError("administrator token must contain at least 32 characters")
        self._token_digest = hashlib.sha256(token.encode("utf-8")).digest()

    def authenticate(self, presented_token: str) -> AdminPrincipal:
        candidate = hashlib.sha256(presented_token.encode("utf-8")).digest()
        if not presented_token.strip() or not hmac.compare_digest(
            self._token_digest,
            candidate,
        ):
            raise AuthenticationError("invalid administrator credentials")
        return AdminPrincipal(subject="single-machine-admin")

