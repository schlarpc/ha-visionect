"""Run the panel's JavaScript suite as part of pytest.

The panel's decisions -- the command guard, the serial line discipline, the
plan builder, the identify handshake -- are JavaScript, so their tests are
JavaScript too, written against Node's built-in test runner and the same
verbatim device captures the Python library uses. Shelling out to ``node`` from
here is what keeps a plain ``pytest`` honest about the whole integration rather
than only the half of it that happens to be Python.

Skipped, not failed, when ``node`` is not installed: it is a test-time tool and
nothing the integration ships needs it. A contributor without Node still gets
the Python suite, and CI has Node.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SUITE = Path(__file__).parent / "js"

node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_panel_javascript_suite() -> None:
    """``node --test tests/js/*.test.mjs``, with the output on failure."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            str(node),
            "--test",
            "--test-reporter=tap",
            *(str(p) for p in sorted(SUITE.glob("*.test.mjs"))),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "the panel's JavaScript suite failed:\n"
            + result.stdout[-20000:]
            + "\n"
            + result.stderr[-4000:]
        )
    # A pass with zero tests would be a silent regression -- a renamed file, a
    # moved directory -- so assert the suite actually ran something.
    assert "# pass " in result.stdout
    passed = next(
        int(line.split()[-1])
        for line in result.stdout.splitlines()
        if line.strip().startswith("# pass ")
    )
    assert passed > 50, f"only {passed} JavaScript tests ran; the suite has far more"
