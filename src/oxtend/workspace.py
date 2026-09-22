"""Bring a whole multi-repo checkout up: core, then every bundle in it.

`oxtend mount` handles one bundle against one core. That is the inner loop. The
outer loop — a fresh clone of the umbrella, or a Monday morning — is "start the
core and put the bundles I am working on into it", and before this it was a
compose command plus one `mount` per bundle, in an order you had to know.

The workspace file records that order and that list, so it is checked in rather
than remembered. It is deliberately thin: paths, a compose file, a URL. Anything
that belongs to a bundle stays in its own `oxtend.yaml`.
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_FILE = "workspace.yaml"
DEFAULT_COMPOSE = "compose.local.yaml"
DEFAULT_URL = "http://localhost:8000"


class WorkspaceError(RuntimeError):
    """The workspace file is unusable, or a step in bringing it up failed."""


@dataclass(frozen=True)
class Workspace:
    root: Path
    core_dir: Path
    compose_file: str
    core_url: str
    bundles: list[Path] = field(default_factory=list)


def load_workspace(path: Path) -> Workspace:
    """Parse and resolve a workspace file, checking every path exists.

    Resolution is relative to the workspace file, not the working directory, so
    `oxtend workspace up -f ../workspace.yaml` behaves the same from anywhere.
    """
    if not path.is_file():
        raise WorkspaceError(
            f"no workspace file at {path}. Write one, or pass --file. "
            f"The umbrella repo's own lives at its root as {DEFAULT_FILE}."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise WorkspaceError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkspaceError(f"{path} must be a mapping, got {type(raw).__name__}")

    root = path.parent.resolve()

    core = raw.get("core") or {}
    if isinstance(core, str):  # `core: platform/orion-core` shorthand
        core = {"path": core}
    if not isinstance(core, dict) or not core.get("path"):
        raise WorkspaceError(f"{path}: `core.path` is required (the orion-core checkout)")

    core_dir = (root / str(core["path"])).resolve()
    if not core_dir.is_dir():
        raise WorkspaceError(f"{path}: core.path {core['path']} does not exist ({core_dir})")

    compose_file = str(core.get("compose") or DEFAULT_COMPOSE)
    if not (core_dir / compose_file).is_file():
        raise WorkspaceError(
            f"{path}: core.compose {compose_file!r} not found in {core_dir}. "
            f"compose.local.yaml is the standalone local stack; compose.yaml is the "
            f"30-service deployment one and is not what this command drives."
        )

    bundles: list[Path] = []
    for entry in raw.get("bundles") or []:
        # Accept a bare path or a mapping, so a bundle can grow options later
        # without breaking every existing workspace file.
        rel = entry if isinstance(entry, str) else (entry or {}).get("path")
        if not rel:
            raise WorkspaceError(f"{path}: every bundles[] entry needs a path")
        bundle = (root / str(rel)).resolve()
        if not (bundle / "oxtend.yaml").is_file():
            raise WorkspaceError(
                f"{path}: {rel} has no oxtend.yaml, so it is not a bundle "
                f"(looked in {bundle})"
            )
        bundles.append(bundle)

    return Workspace(
        root=root,
        core_dir=core_dir,
        compose_file=compose_file,
        core_url=str(core.get("url") or DEFAULT_URL).rstrip("/"),
        bundles=bundles,
    )


def compose(ws: Workspace, *args: str, check: bool = True) -> int:
    """Run `docker compose -f <file> <args>` in the core checkout."""
    cmd = ["docker", "compose", "-f", ws.compose_file, *args]
    try:
        proc = subprocess.run(cmd, cwd=ws.core_dir, check=False)
    except FileNotFoundError as exc:
        raise WorkspaceError(
            "docker is not on PATH. On Windows with Docker in WSL, run this "
            "command from inside WSL — the daemon is not reachable from the "
            "Windows shell."
        ) from exc
    if check and proc.returncode != 0:
        raise WorkspaceError(f"`{' '.join(cmd)}` exited {proc.returncode}")
    return proc.returncode


def wait_for_core(core_url: str, timeout: float = 300.0, interval: float = 3.0) -> float:
    """Block until `<core_url>/health` answers 200. Returns seconds waited.

    The backend is gated on the migrate job finishing, and from empty volumes
    that whole sequence runs about 80 seconds, so the default timeout is
    generous on purpose: a too-short wait here reads as "the stack is broken"
    when it is merely cold.
    """
    started = time.monotonic()
    deadline = started + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{core_url}/health", timeout=5) as response:
                if response.status == 200:
                    return time.monotonic() - started
                last = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(getattr(exc, "reason", exc))
        time.sleep(interval)
    raise WorkspaceError(
        f"{core_url}/health did not return 200 within {timeout:.0f}s (last: {last}). "
        f"Check `docker compose -f {DEFAULT_COMPOSE} logs orion-backend`."
    )
