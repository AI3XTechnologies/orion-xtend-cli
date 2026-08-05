"""`oxtend build` — compile the remote and assemble the bundle (ORION-688 task 3).

Produces the directory the kernel installs: declarative metadata, scoped SQL, DAGs,
backend code, a compiled UI remote, and `bundle.json`.

The digest is computed **once, here**, using the kernel's own
`orion.kernel.digest.compute_bundle_digest`. ORION-665 reads it rather than
recomputing on every boot, which only works if both sides hash the same way — so this
imports the kernel function instead of reimplementing sha256-over-a-walk.

Called by: oxtend/cli.py (`build`, `package`, `all`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oxtend.manifest_vendored import kernel_manifest_module

#: Copied verbatim into the bundle when present.
VERBATIM_DIRS = ("metadata", "migrations", "policies", "backend", "dags", "config", "assets", "prompt_config", "naming", "helm", "ops")

BUNDLE_JSON = "bundle.json"


class BuildError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise BuildError(
            f"{' '.join(cmd)} failed in {cwd}:\n{(result.stderr or result.stdout)[-2000:]}"
        )


def _compile_ui_remote(ext_dir: Path, out_dir: Path) -> bool:
    """Compile `ui/` into `remotes/`. Absent `ui/` is not an error.

    Uses `npm ci` when a lockfile exists: a build tool that resolves fresh versions
    on every run cannot produce a reproducible bundle, which is the point of
    `oxtend.lock`.
    """
    ui_dir = ext_dir / "ui"
    if not ui_dir.is_dir():
        return False
    if (ui_dir / "package-lock.json").exists():
        _run(["npm", "ci", "--silent"], cwd=ui_dir)
    elif not (ui_dir / "node_modules").is_dir():
        _run(["npm", "install", "--silent"], cwd=ui_dir)
    _run(["npm", "run", "build", "--silent"], cwd=ui_dir)

    dist = ui_dir / "dist"
    if not dist.is_dir():
        raise BuildError(f"{ui_dir}/dist not produced by `npm run build`")
    remotes = out_dir / "remotes"
    remotes.mkdir(parents=True, exist_ok=True)
    for item in dist.rglob("*"):
        if item.is_file():
            target = remotes / item.relative_to(dist)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    return True


def build_bundle(
    ext_dir: Path,
    out_dir: Path | None = None,
    *,
    core_version: str | None = None,
    skip_ui: bool = False,
) -> Path:
    """Assemble the bundle and return its directory."""
    manifest_mod = kernel_manifest_module()
    ext_dir = Path(ext_dir)
    manifest = manifest_mod.load_manifest(ext_dir)

    out_dir = Path(out_dir) if out_dir else ext_dir / "build" / f"{manifest.scope}-{manifest.version}"
    if out_dir.exists():
        # A stale file from a previous build would be hashed into the digest and
        # shipped — always start from empty.
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    ui_built = False if skip_ui else _compile_ui_remote(ext_dir, out_dir)

    for sub in VERBATIM_DIRS:
        src = ext_dir / sub
        if src.is_dir():
            shutil.copytree(src, out_dir / sub)
    # A pre-built remote committed to the repo (no ui/ sources) still ships.
    if not ui_built and (ext_dir / "remotes").is_dir():
        shutil.copytree(ext_dir / "remotes", out_dir / "remotes", dirs_exist_ok=True)

    shutil.copy2(ext_dir / "oxtend.yaml", out_dir / "oxtend.yaml")
    for extra in ("oxtend.lock", "README.md", "RUNBOOK.md", "LICENSE"):
        if (ext_dir / extra).is_file():
            shutil.copy2(ext_dir / extra, out_dir / extra)

    from orion.kernel.digest import compute_bundle_digest  # type: ignore

    digest = compute_bundle_digest(out_dir)
    bundle_meta: dict[str, Any] = {
        "scope": manifest.scope,
        "version": manifest.version,
        "kind": manifest.kind,
        "digest": digest,
        "manifest": manifest.model_dump(mode="json"),
        # UTC and explicit: a bundle's build time is read by humans comparing two
        # artifacts, and a local-time stamp makes that comparison wrong by hours.
        "built_at": datetime.now(timezone.utc).isoformat(),
        "core_compat": manifest.core.compat,
        "built_against_core": core_version,
        "ui_remote": ui_built or (out_dir / "remotes").is_dir(),
    }
    (out_dir / BUNDLE_JSON).write_text(json.dumps(bundle_meta, indent=2, sort_keys=True) + "\n")
    return out_dir
