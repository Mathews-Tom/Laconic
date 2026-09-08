from __future__ import annotations

import subprocess
import sys

for index in range(240):
    instant = 100.0 + index / 10
    print(
        f"trace[{index:03d}] cache clock={instant:.1f}: half-open validity interval "
        "requires the exact expiry timestamp to be treated as expired"
    )

raise SystemExit(
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        check=False,
    ).returncode
)
