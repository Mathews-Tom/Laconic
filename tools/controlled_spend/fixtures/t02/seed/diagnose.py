from __future__ import annotations

import subprocess
import sys

for index in range(240):
    quote_state = "inside-double-quote" if index % 2 else "outside-quote"
    print(
        f"trace[{index:03d}] scanner state={quote_state}: comment marker must be classified "
        "after quote and escape state are known"
    )

raise SystemExit(
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        check=False,
    ).returncode
)
