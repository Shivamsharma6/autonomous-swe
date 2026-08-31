from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from apps.api.routes import update_model_config
from apps.api.schemas import ModelConfigRequest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "expected_key"),
    [
        ("https://old-provider.example/v1/", "old-provider-key"),
        ("https://new-provider.example/v1", ""),
    ],
)
async def test_blank_key_is_retained_only_for_the_same_provider(
    endpoint: str, expected_key: str
) -> None:
    settings = SimpleNamespace(
        model_base_url="https://old-provider.example/v1",
        model_api_key=SecretStr("old-provider-key"),
    )
    response = await update_model_config(
        ModelConfigRequest(base_url=endpoint, primary_model="selected-model"),
        SimpleNamespace(settings=settings),
    )
    assert settings.model_api_key.get_secret_value() == expected_key
    assert response.has_api_key == bool(expected_key)
