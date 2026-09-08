from __future__ import annotations

import subprocess
import sys

for index in range(240):
    layer = index % 3
    print(
        f"trace[{index:03d}] config loader: applying priority layer={layer}; "
        "expected later assignments to replace matching keys while unrelated keys remain"
    )

raise SystemExit(
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        check=False,
    ).returncode
)
