from __future__ import annotations

import subprocess
import sys

for index in range(240):
    edge = f"node-{index % 12}->node-{(index + 1) % 12}"
    print(
        f"trace[{index:03d}] dependency edge={edge}: item-to-dependency input must emit "
        "the dependency before decrementing the dependent"
    )

raise SystemExit(
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        check=False,
    ).returncode
)
