from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import tarfile


def test_head_archive_imports_application_without_worktree_only_files(
    tmp_path: Path,
) -> None:
    """Break caught: local untracked modules make tests/build pass but clean HEAD fails."""
    repository = Path(__file__).resolve().parents[2]
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=repository,
        capture_output=True,
        check=True,
    ).stdout
    clean_source = tmp_path / "clean-source"
    clean_source.mkdir()
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as source:
        source.extractall(clean_source, filter="data")

    environment = os.environ | {
        "PYTHONPATH": str(clean_source / "src"),
    }
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import quote_app.app, quote_app.services.full_pipeline; "
                "import quote_app.sites.official, quote_app.sites.jd, "
                "quote_app.sites.tmall"
            ),
        ],
        cwd=clean_source,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert imported.returncode == 0, imported.stderr
    assert (clean_source / "packaging" / "quotation_app.spec").is_file()
    assert (clean_source / "resources" / "sites" / "catalog.json").is_file()
