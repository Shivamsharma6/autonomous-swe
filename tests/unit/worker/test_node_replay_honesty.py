from __future__ import annotations

import hashlib
from pathlib import Path

from agents.base import _MAX_PAYLOAD_CHARS, _bounded_json
from apps.worker.nodes import _fingerprint_worktree


def _make_worktree(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    files = {
        "src/service.py": b"print('hello')\n",
        "tests/test_service.py": b"def test_ok():\n    assert True\n",
    }
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return tmp_path, files


def test_fingerprint_matches_intact_files(tmp_path: Path) -> None:
    root, files = _make_worktree(tmp_path)
    recorded = _fingerprint_worktree(root, tuple(files))
    assert set(recorded) == set(files)
    assert all(value == hashlib.sha256(files[key]).hexdigest() for key, value in recorded.items())
    again = _fingerprint_worktree(root, tuple(recorded))
    assert again == recorded


def test_fingerprint_detects_missing_and_changed_files(tmp_path: Path) -> None:
    root, files = _make_worktree(tmp_path)
    recorded = _fingerprint_worktree(root, tuple(files))

    (root / "src/service.py").unlink()
    (root / "tests/test_service.py").write_bytes(b"changed\n")

    current = _fingerprint_worktree(root, tuple(recorded))
    assert current != recorded
    assert current["src/service.py"] == ""
    assert current["tests/test_service.py"] == hashlib.sha256(b"changed\n").hexdigest()


def test_fingerprint_ignores_paths_outside_worktree(tmp_path: Path) -> None:
    root, _ = _make_worktree(tmp_path)
    # Traversal and absolute escape targets are dropped entirely instead of
    # being recorded with placeholder hashes.
    escaped = _fingerprint_worktree(root, ("../outside.py", "/etc/passwd"))
    assert escaped == {}


def test_bounded_json_keeps_small_payloads_verbatim() -> None:
    payload = {"goal": "ship it", "refs": ["a", "b"]}
    assert _bounded_json(payload) == '{"goal": "ship it", "refs": ["a", "b"]}'


def test_bounded_json_truncates_oversize_strings_and_total() -> None:
    payload = {"blob": "x" * 200_000}
    rendered = _bounded_json(payload)
    assert len(rendered) < 20_000
    assert "truncated" in rendered

    wide = {f"key{i}": "y" * 500 for i in range(500)}
    rendered_total = _bounded_json(wide)
    assert len(rendered_total) <= _MAX_PAYLOAD_CHARS + 200
