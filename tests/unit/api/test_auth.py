from __future__ import annotations

import hashlib

import pytest

from apps.api.auth import AdminAuthenticator, AuthenticationError


def test_admin_authentication_hashes_both_inputs_and_uses_constant_time_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return left == right

    monkeypatch.setattr("apps.api.auth.hmac.compare_digest", compare)
    authenticator = AdminAuthenticator("a" * 32)

    principal = authenticator.authenticate("a" * 32)
    with pytest.raises(AuthenticationError):
        authenticator.authenticate("wrong")

    assert principal.subject == "single-machine-admin"
    assert len(compared) == 2
    assert all(len(left) == len(right) == hashlib.sha256().digest_size for left, right in compared)


@pytest.mark.parametrize("token", ("", " ", "Bearer token", "a" * 31))
def test_invalid_tokens_have_one_generic_failure(token: str) -> None:
    with pytest.raises(AuthenticationError, match="invalid administrator credentials"):
        AdminAuthenticator("a" * 32).authenticate(token)

