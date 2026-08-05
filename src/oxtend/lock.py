"""`oxtend.lock` — reproducibility record (ORION-688 task 6).

Pins what a bundle was built against: the resolved core version, the manifest digest,
and the UI remote's dependency tree. Two reasons, both practical:

* **Rebuild determinism.** Rebuilding tag `x_orion_email/v1.4.0` six months later must
  produce the same artifact. Without a lock, `>=1.0,<2.0` resolves to whatever core is
  current and the rebuild is a different bundle with the same version number.
* **Provenance.** When an extension misbehaves on a core it "supports", the first
  question is which core it was actually built and contract-tested against. The lock
  is the answer, and it ships inside the bundle.

Called by: oxtend/cli.py (`lock`, and written by `build` when `--lock` is passed).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from oxtend.manifest_vendored import installed_core_version, kernel_manifest_module

LOCK_FILENAME = "oxtend.lock"
LOCK_VERSION = 1


def manifest_digest(ext_dir: Path) -> str:
    """Hash of the manifest as written.

    Hashes the raw file bytes rather than the parsed model: a comment or key-order
    change is a change to the artifact's inputs even when the parsed result is
    identical, and a lock that says otherwise is misleading.
    """
    raw = (Path(ext_dir) / "oxtend.yaml").read_bytes()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _ui_dependency_tree(ext_dir: Path) -> dict[str, Any]:
    """The remote's resolved dependency versions, from `package-lock.json`.

    Only direct dependencies are recorded. The full transitive tree belongs in the
    lockfile npm already maintains; duplicating it here would go stale and be believed.
    """
    ui = Path(ext_dir) / "ui"
    pkg = ui / "package.json"
    lockfile = ui / "package-lock.json"
    if not pkg.is_file():
        return {}
    declared = json.loads(pkg.read_text())
    direct = {**declared.get("dependencies", {}), **declared.get("devDependencies", {})}
    resolved: dict[str, str] = {}
    if lockfile.is_file():
        lock = json.loads(lockfile.read_text())
        packages = lock.get("packages", {})
        for name in direct:
            entry = packages.get(f"node_modules/{name}") or {}
            if version := entry.get("version"):
                resolved[name] = version
    return {
        "lockfile_present": lockfile.is_file(),
        "declared": direct,
        "resolved": resolved,
    }


def write_lock(ext_dir: Path, *, core_version: str | None = None) -> Path:
    """Write `<ext_dir>/oxtend.lock` and return its path."""
    ext_dir = Path(ext_dir)
    manifest = kernel_manifest_module().load_manifest(ext_dir)
    payload = {
        "lock_version": LOCK_VERSION,
        "scope": manifest.scope,
        "version": manifest.version,
        "manifest_digest": manifest_digest(ext_dir),
        "core": {
            "declared_compat": manifest.core.compat,
            "resolved_version": core_version or installed_core_version(),
        },
        "ui": _ui_dependency_tree(ext_dir),
        "locked_at": datetime.now(timezone.utc).isoformat(),
    }
    path = ext_dir / LOCK_FILENAME
    path.write_text(yaml.safe_dump(payload, sort_keys=True))
    return path


def read_lock(ext_dir: Path) -> dict[str, Any] | None:
    path = Path(ext_dir) / LOCK_FILENAME
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text()) or None


def lock_is_current(ext_dir: Path) -> tuple[bool, str]:
    """Whether the lock still describes this source tree.

    Returns `(ok, reason)`. Called by `build --require-lock` so a release cannot ship
    a bundle whose lock describes a previous manifest — a stale lock is worse than a
    missing one, because it is trusted.
    """
    lock = read_lock(ext_dir)
    if lock is None:
        return False, f"{LOCK_FILENAME} is absent — run `oxtend lock`"
    if lock.get("lock_version") != LOCK_VERSION:
        return False, f"{LOCK_FILENAME} is lock_version {lock.get('lock_version')}, expected {LOCK_VERSION}"
    current = manifest_digest(ext_dir)
    if lock.get("manifest_digest") != current:
        return False, (
            f"{LOCK_FILENAME} records manifest digest {lock.get('manifest_digest')} but "
            f"oxtend.yaml now hashes to {current} — re-run `oxtend lock`"
        )
    manifest = kernel_manifest_module().load_manifest(ext_dir)
    if lock.get("version") != manifest.version:
        return False, (
            f"{LOCK_FILENAME} records version {lock.get('version')} but the manifest says "
            f"{manifest.version}"
        )
    return True, "lock is current"
