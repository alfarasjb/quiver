import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point every test at a temp memory folder, temp dir and a clean git config."""
    monkeypatch.setenv("QUIVER_HOME", str(tmp_path / "mem"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))  # keeps nudge markers out of $TMPDIR
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@t")
