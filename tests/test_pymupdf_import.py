from __future__ import annotations

import subprocess
import sys


def test_extractor_import_does_not_emit_deprecated_fitz_warning() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import idx_digest.extractors"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "the `fitz` api is deprecated" not in completed.stderr.lower()
