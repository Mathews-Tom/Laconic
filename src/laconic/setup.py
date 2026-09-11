"""Host detection and post-install verification for guided onboarding.

Laconic's install surface is honest but not discoverable: the codec, the
content-free diagnostics, and the two hosts that support them are separate
commands in separate namespaces, and nothing tells a new user which of them
their machine can actually use. Worse, every install command reports that it
wrote a file, which is not the same claim as "the codec ran".

This module supplies the two facts a guided setup needs and nothing else:
which hosts are present (:func:`detect_hosts`), and whether the runtime has
since recorded a real decision (:func:`verify_runtime`). Both are pure
functions over injected paths so the surface is testable without touching a
developer's real home directory, and neither writes anything — the CLI
composes them with the existing installers rather than reimplementing one.

The capability table is deliberately static rather than probed. What a host
supports is a property of what Laconic ships for it, not of the user's
machine: OMP has a runtime extension that can compress, Claude Code has
hooks that can only observe, and Codex has no adapter at all. Stating that
plainly is the point — a user who installs on Claude Code and expects
compression has been misled, and no amount of successful file writing tells
them otherwise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from laconic.runtime.operator import runtime_storage_status

#: OMP: ships a runtime extension that intercepts tool results, so the
#: compressing codec and the content-free diagnostics both run here.
HOST_OMP: Final = "omp"

#: Claude Code: ships Observe hook entries only. The hooks record
#: content-free receipts after a tool has already returned its full result,
#: so nothing is compressed and no token is saved.
HOST_CLAUDE_CODE: Final = "claude-code"

#: Codex: no adapter of either kind exists yet.
HOST_CODEX: Final = "codex"


@dataclass(frozen=True, slots=True)
class HostCapability:
    """What one coding-agent host can do, and whether it is present."""

    host: str
    detected: bool
    codec: bool
    observe: bool
    detail: str

    @property
    def actionable(self) -> bool:
        """Whether a setup run has anything to install for this host."""
        return self.detected and (self.codec or self.observe)

    def to_json(self) -> dict[str, object]:
        return {
            "host": self.host,
            "detected": self.detected,
            "codec": self.codec,
            "observe": self.observe,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Verification:
    """Whether the runtime has demonstrably made a decision.

    ``confirmed`` is keyed on *eligible observations*, not compressed ones.
    A pass-through is a working codec declining a payload it cannot shrink,
    which is the common and correct outcome on small results; treating it as
    failure would train a user to distrust the honest answer. Zero eligible
    observations is the only state that proves nothing ran.
    """

    sessions: int
    eligible_observations: int
    compressed_observations: int
    confirmed: bool
    detail: str

    def to_json(self) -> dict[str, object]:
        return {
            "sessions": self.sessions,
            "eligible_observations": self.eligible_observations,
            "compressed_observations": self.compressed_observations,
            "confirmed": self.confirmed,
            "detail": self.detail,
        }


def _omp_present(cwd: Path, home: Path, env: dict[str, str]) -> bool:
    override = env.get("PI_CODING_AGENT_DIR")
    candidates = [cwd / ".omp", home / ".omp"]
    if override:
        candidates.append(Path(override))
    return any(candidate.is_dir() for candidate in candidates)


def _claude_code_present(cwd: Path, home: Path) -> bool:
    return (cwd / ".claude").is_dir() or (home / ".claude").is_dir()


def _codex_present(cwd: Path, home: Path) -> bool:
    return (cwd / ".codex").is_dir() or (home / ".codex").is_dir()


def detect_hosts(
    *,
    cwd: Path,
    home: Path,
    env: dict[str, str] | None = None,
) -> tuple[HostCapability, ...]:
    """Report every known host, present or not, with its real capability.

    Absent hosts are included rather than filtered out: "Codex is not
    supported" is information a user needs, and silently omitting a host
    they use looks identical to a detection bug.
    """
    environment = dict(os.environ) if env is None else env
    omp = _omp_present(cwd, home, environment)
    claude_code = _claude_code_present(cwd, home)
    codex = _codex_present(cwd, home)
    return (
        HostCapability(
            host=HOST_OMP,
            detected=omp,
            codec=True,
            observe=True,
            detail=(
                "runtime extension intercepts tool results; the codec compresses here"
                if omp
                else "not detected (no .omp directory in this project or your home)"
            ),
        ),
        HostCapability(
            host=HOST_CLAUDE_CODE,
            detected=claude_code,
            codec=False,
            observe=True,
            detail=(
                "setup installs content-free receipt hooks only; the "
                "transforming codec hook is a separate opt-in "
                "(docs/claude-code-codec.md)"
                if claude_code
                else "not detected (no .claude directory in this project or your home)"
            ),
        ),
        HostCapability(
            host=HOST_CODEX,
            detected=codex,
            codec=False,
            observe=False,
            detail=(
                "detected, but Laconic ships no Codex adapter of either kind"
                if codex
                else "not detected, and Laconic ships no Codex adapter of either kind"
            ),
        ),
    )


def verify_runtime(data_dir: Path | None = None) -> Verification:
    """Report whether the runtime codec has recorded a decision yet."""
    status = runtime_storage_status(data_dir)
    eligible = status.eligible_observations
    compressed = status.compressed_observations
    if not status.exists or status.sessions == 0:
        detail = (
            "no runtime storage yet — the codec has not run. Start a session "
            "on a host whose codec column is yes, invoke a tool, then re-run "
            "`laconic setup --verify-only`."
        )
        return Verification(
            sessions=status.sessions,
            eligible_observations=eligible,
            compressed_observations=compressed,
            confirmed=False,
            detail=detail,
        )
    if eligible == 0:
        detail = (
            f"{status.sessions} session(s) recorded but no eligible tool result yet. "
            "The codec only sees read, bash, grep, and glob results; invoke one "
            "of those, then re-run `laconic setup --verify-only`."
        )
        return Verification(
            sessions=status.sessions,
            eligible_observations=eligible,
            compressed_observations=compressed,
            confirmed=False,
            detail=detail,
        )
    if compressed == 0:
        detail = (
            f"{eligible} eligible observation(s) decided, none compressed. The codec "
            "is running and declining payloads it cannot shrink, which is the correct "
            "outcome on small results."
        )
    else:
        detail = f"{eligible} eligible observation(s) decided, {compressed} compressed."
    return Verification(
        sessions=status.sessions,
        eligible_observations=eligible,
        compressed_observations=compressed,
        confirmed=True,
        detail=detail,
    )
