from planning.service import default_repository_context


def test_repository_context_includes_browser_entrypoints_next_to_python_manifest(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="game"\nversion="0.1.0"\n')
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "index.html").write_text("<canvas></canvas>")
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "smoke.js").write_text("// browser checks")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("")
    (tmp_path / "outside").symlink_to(tmp_path.parent, target_is_directory=True)
    context = default_repository_context(str(tmp_path))
    assert "index.html" in context["repository_files"]
    assert "test/smoke.js" in context["repository_files"]
    assert "node_modules/ignored.js" not in context["repository_files"]
    assert not any(path.startswith("outside/") for path in context["repository_files"])
