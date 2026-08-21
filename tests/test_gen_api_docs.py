"""Ticket #8: `scripts/gen_api_docs.py` (docs/API.md's generator) is pure
static analysis over this repo's own Python source -- no SC2 client, no
game, no network. It gets a plain, marker-less test rather than
`@pytest.mark.integration`: per this project's convention (see
tests/conftest.py's module docstring and the `integration` marker in
pyproject.toml), that marker is reserved for tests that boot a real SC2
process, which this one never does.

Run as a subprocess (rather than importing scripts.gen_api_docs directly)
because `scripts/` is a standalone CLI location, not part of the `src/`
package layout `pyproject.toml`'s `pythonpath = ["src"]` puts on the import
path -- invoking it as `python scripts/gen_api_docs.py ...`, exactly how a
human or CI would, is also a more faithful test of the actual CLI contract
than reaching into its internals.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GEN_SCRIPT = REPO_ROOT / "scripts" / "gen_api_docs.py"
API_DOCS = REPO_ROOT / "docs" / "API.md"


def _run_check() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_committed_api_docs_is_up_to_date_with_source():
    """The whole point of the CI check: docs/API.md as committed must equal
    what the generator would produce right now from the real source. If
    this fails, someone changed a documented module's public surface
    without regenerating -- run `python scripts/gen_api_docs.py` and commit
    the result."""
    result = _run_check()
    assert result.returncode == 0, (
        "docs/API.md is stale relative to the current source. "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_generator_is_idempotent():
    """Running the generator twice back-to-back must produce byte-identical
    output -- the CI check's diff-based drift detection only makes sense if
    this holds."""
    gen1 = subprocess.run(
        [sys.executable, str(GEN_SCRIPT)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert gen1.returncode == 0, gen1.stderr
    first = API_DOCS.read_text()

    gen2 = subprocess.run(
        [sys.executable, str(GEN_SCRIPT)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert gen2.returncode == 0, gen2.stderr
    second = API_DOCS.read_text()

    assert first == second


def test_check_catches_real_drift(tmp_path):
    """Prove --check actually fails on a real discrepancy, not just a
    hand-wavy "should work": copy docs/API.md somewhere, corrupt the
    committed one, confirm --check reports failure, then restore it. Uses a
    throwaway backup rather than git so this test has no git dependency and
    can't leave the working tree dirty if it's interrupted mid-run."""
    backup = tmp_path / "API.md.bak"
    backup.write_text(API_DOCS.read_text())
    try:
        API_DOCS.write_text("this is not the real generated content\n")
        result = _run_check()
        assert result.returncode == 1
        assert "STALE" in result.stderr
    finally:
        API_DOCS.write_text(backup.read_text())

    # And confirm restoring it makes the check pass again.
    assert _run_check().returncode == 0
