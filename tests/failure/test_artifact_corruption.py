from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pytest


@pytest.mark.asyncio
async def test_corrupted_evidence_is_quarantined_and_never_released(
    database: Any,
    tmp_path: Path,
) -> None:
    """Run the release-gating integrity assertion in the failure-injection suite too."""
    module = import_module("tests.integration.persistence.test_artifact_integrity")
    assert_corruption_recovery = cast(
        Any,
        module.test_corrupt_artifact_is_detected_quarantined_and_excluded,
    )
    await assert_corruption_recovery(database, tmp_path)
