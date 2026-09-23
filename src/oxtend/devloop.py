"""`oxtend mount` / `oxtend dev` — the local bundle loop.

The kernel has always been able to reinstall a bundle into a running process:
`registry.install()` takes a per-scope advisory lock, unmounts the old routes,
evicts the scope's modules from `sys.modules`, invalidates the OpenAPI and
Starlette matcher caches, and remounts. `POST /kernel/extensions/install` exposes
it, and its request model even carries a `bundle_dir` override described as "for
the dev loop where a bundle is dropped in by hand".

Nothing drove it. The documented procedure was: validate, lock, build, `cp -a`
the build output into the core checkout's `extensions/`, edit `.env`, restart the
backend, and hand-write an INSERT into `orion.entitlement_registry`. Minutes per
iteration, and two of those steps are load-bearing traps (see `resolve_core_version`
below, and `oxtend doctor`).

This module is the missing driver:

    sync_bundle()     build straight into <core>/extensions/<scope>
    install_bundle()  POST the scope to a running core — no container restart
    watch()           poll the source tree and do both on change

Deliberately no new dependencies. HTTP is stdlib `urllib`; change detection is an
mtime walk rather than `watchfiles`. A bundle is tens to hundreds of files, so
polling costs nothing measurable and behaves identically on Windows, where the
native filesystem-event APIs are the least consistent.

Called by: oxtend/cli.py (`mount`, `dev`).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from oxtend.build import build_bundle
from oxtend.manifest_vendored import kernel_manifest_module

#: Where compose.local.yaml publishes the backend.
DEFAULT_CORE_URL = "http://localhost:8000"

#: Matches compose.local.yaml's default. Every mutating /kernel/* route compares
#: X-Internal-Key against os.environ["ORION_INTERNAL_API_KEY"], and an EMPTY
#: expected value rejects unconditionally — so a core started without it answers
#: 401 to a correct request. `oxtend doctor` checks for exactly that.
DEFAULT_INTERNAL_KEY = "orion-local-internal-key"

#: The core version the bundle is built against is read from HERE, not from the
#: installed wheel. See resolve_core_version().
CORE_VERSION_MANIFEST = ".release-please-manifest.json"
CORE_VERSION_KEY = "backend"

#: Never walked when looking for changes: build output (which we write), and the
#: usual dependency and cache trees. Without `build` here the loop would retrigger
#: on its own output, forever.
WATCH_IGNORE_DIRS = frozenset(
    {
        "build", "node_modules", ".git", "__pycache__",
        "dist", ".venv", ".pytest_cache", ".mypy_cache",
    }
)

UI_DIR = "ui"


class DevLoopError(RuntimeError):
    """Anything the developer can fix: unreachable core, bad scope, 4xx install."""


# ---------------------------------------------------------------------------
# Paths and versions
# ---------------------------------------------------------------------------


def resolve_core_version(core_dir: Path) -> str | None:
    """The core version a bundle should be validated against, read from the checkout.

    This exists because `installed_core_version()` is the wrong answer and fails
    in a way that reads as the bundle's fault.

    `importlib.metadata.version("orion-backend")` returns `backend/pyproject.toml`'s
    `version`, which is **0.7.0** and has not tracked reality since the Xtend line
    started. The version that is actually cut, tagged and pinned lives in
    `.release-please-manifest.json` under the `backend` key — **1.0.0-dev.2**.
    Every bundle declares `core.compat: ">=1.0.0-dev.2,<2.0"`, so validating against
    the wheel version fails *every* bundle against its own correct compat range,
    with an error naming the bundle rather than the tooling. That is register D-108,
    and the client RUNBOOK works around it by making the developer pass
    `--core-version 1.0.0-dev.2` by hand on every single command.

    Returns None when the file is absent or unreadable, so callers can fall back
    rather than hard-fail on a core checkout that predates the manifest.
    """
    manifest = Path(core_dir) / CORE_VERSION_MANIFEST
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get(CORE_VERSION_KEY)
    return str(value) if value else None


def extensions_root(core_dir: Path) -> Path:
    """The host directory bind-mounted to /extensions in the backend container."""
    return Path(core_dir) / "extensions"


def load_scope(ext_dir: Path) -> tuple[str, str]:
    """(scope, version) straight from the manifest — the kernel's own parser."""
    manifest = kernel_manifest_module().load_manifest(Path(ext_dir))
    return manifest.scope, manifest.version


def bundle_target(core_dir: Path, scope: str) -> Path:
    """Where this scope's bundle lives on the host: <core>/extensions/<scope>."""
    return extensions_root(core_dir) / scope


def container_bundle_dir(scope: str, extensions_dir: str = "/extensions") -> str:
    """The same directory as the backend container sees it.

    Only needed when the container's mount point is not the default; the install
    endpoint already resolves `registry.extensions_dir() / scope` when
    `bundle_dir` is omitted, which is the common case.
    """
    return f"{extensions_dir.rstrip('/')}/{scope}"


# ---------------------------------------------------------------------------
# Build → mount
# ---------------------------------------------------------------------------


def sync_bundle(
    ext_dir: Path,
    core_dir: Path,
    *,
    skip_ui: bool = False,
    core_version: str | None = None,
) -> Path:
    """Build `ext_dir` directly into `<core>/extensions/<scope>` and return that path.

    No intermediate `build/<scope>-<version>/` and no `cp -a` afterwards: the
    mounted directory IS the build output. `build_bundle` already clears its
    output directory first, so this is a replace rather than a merge — a file you
    delete from the source disappears from the mount, which a copy-over-the-top
    would have left behind and silently kept installing.
    """
    ext_dir = Path(ext_dir)
    scope, _version = load_scope(ext_dir)
    target = bundle_target(core_dir, scope)
    target.parent.mkdir(parents=True, exist_ok=True)
    return build_bundle(
        ext_dir,
        target,
        core_version=core_version or resolve_core_version(core_dir),
        skip_ui=skip_ui,
    )


# ---------------------------------------------------------------------------
# Install into a running core
# ---------------------------------------------------------------------------


def install_bundle(
    scope: str,
    *,
    core_url: str = DEFAULT_CORE_URL,
    internal_key: str | None = None,
    bundle_dir: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """POST /kernel/extensions/install and return the parsed response.

    `bundle_dir` is a path **inside the backend container**, not on the host.
    Omitted (the normal case) the endpoint resolves `extensions_dir()/scope`
    itself, which is exactly where sync_bundle just wrote.

    The install timeout is generous because the work behind it is not trivial:
    digest computation over the whole bundle, scoped SQL migrations, and importing
    the bundle's Python.
    """
    key = internal_key if internal_key is not None else os.environ.get(
        "ORION_INTERNAL_API_KEY", DEFAULT_INTERNAL_KEY
    )
    payload: dict[str, object] = {"scope": scope}
    if bundle_dir:
        payload["bundle_dir"] = bundle_dir

    request = urllib.request.Request(
        f"{core_url.rstrip('/')}/kernel/extensions/install",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Internal-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DevLoopError(_describe_http_error(exc, scope, core_url)) from exc
    except urllib.error.URLError as exc:
        raise DevLoopError(
            f"cannot reach core at {core_url}: {exc.reason}. "
            "Is the local stack up? "
            "`docker compose -f compose.local.yaml ps`"
        ) from exc


def _describe_http_error(exc: urllib.error.HTTPError, scope: str, core_url: str) -> str:
    """Turn the kernel's error envelope into something worth reading.

    The install endpoint maps its exception types onto distinct statuses
    (403 entitlement, 400 manifest, 409 kernel) and puts a `{code, message}` object
    in `detail`. Rendering the raw body would bury that in JSON, and 401 in
    particular has one overwhelmingly likely cause worth naming outright.
    """
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - diagnostics only; never mask the real error
        body = {}
    detail = body.get("detail", body)
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("code") or str(detail)
    else:
        message = str(detail) if detail else exc.reason

    if exc.code == 401:
        return (
            f"core at {core_url} rejected the internal key (401). The key must match "
            "ORION_INTERNAL_API_KEY in the backend's environment; note that an UNSET "
            "value on the server rejects every request, however correct the header."
        )
    if exc.code == 403:
        return (
            f"{scope} is not entitled ({message}). Locally, set ORION_DEV_MODE=1 and "
            "ORION_ENTITLEMENTS_ENFORCED=false — compose.local.yaml does both."
        )
    return f"install of {scope} failed ({exc.code}): {message}"


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def snapshot(ext_dir: Path) -> dict[str, float]:
    """{relative path: mtime} for every source file that should trigger a rebuild.

    Skips WATCH_IGNORE_DIRS — most importantly `build`, which is our own output
    and would otherwise make every rebuild trigger the next one.
    """
    ext_dir = Path(ext_dir)
    out: dict[str, float] = {}
    for path in ext_dir.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.relative_to(ext_dir).parts)
        if parts & WATCH_IGNORE_DIRS:
            continue
        try:
            out[str(path.relative_to(ext_dir))] = path.stat().st_mtime
        except OSError:
            # Vanished between the walk and the stat — an editor's atomic save.
            # The next poll sees the settled tree.
            continue
    return out


def changed_paths(before: dict[str, float], after: dict[str, float]) -> set[str]:
    """Added, removed, or touched, as relative path strings."""
    changed = {p for p, m in after.items() if before.get(p) != m}
    changed |= set(before) - set(after)
    return changed


def touches_ui(paths: set[str]) -> bool:
    """Whether a rebuild has to re-run the (slow) `npm run build` step.

    `build_bundle` copies VERBATIM_DIRS unchanged and only compiles `ui/` into
    `remotes/`. So a change confined to metadata, migrations, prompts or Python
    can pass --skip-ui and skip npm entirely, which is the difference between a
    sub-second rebuild and a multi-second one.
    """
    return any(p.split(os.sep)[0] == UI_DIR or p.split("/")[0] == UI_DIR for p in paths)
