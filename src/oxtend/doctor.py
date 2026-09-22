"""`oxtend doctor` — check the things that fail confusingly, before they do.

Each check here exists because someone lost time to it. The failures this
catches share a shape: the tool reports success and the exit code says otherwise,
or the server returns a status whose obvious reading is wrong.

    oxtend doctor --core ../orion-core

Never mutates anything. Exit 0 = every check passed or warned; exit 1 = at least
one check failed.

Called by: oxtend/cli.py (`doctor`).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from oxtend.devloop import (
    CORE_VERSION_MANIFEST,
    DEFAULT_CORE_URL,
    DEFAULT_INTERNAL_KEY,
    resolve_core_version,
)

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _check_console_encoding() -> Check:
    """Register D-110: a passing command exits 1 while printing its success mark.

    Every oxtend result line starts with U+2713 / U+2717. On Windows a console
    that is not UTF-8 raises UnicodeEncodeError on the write — *after* the work
    succeeded — so the command fails with a traceback about the checkmark and CI
    reports a build failure with no failing step. The RUNBOOK's answer was to
    make every developer remember `export PYTHONIOENCODING=utf-8`.

    oxtend.cli.main() now reconfigures both streams to UTF-8 at startup, so this
    check confirms that took effect rather than asking anyone to remember.
    """
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if encoding in {"utf8", "utf_8"}:
        return Check("console encoding", OK, f"stdout is {sys.stdout.encoding}")
    if platform.system() == "Windows":
        return Check(
            "console encoding",
            FAIL,
            f"stdout encoding is {sys.stdout.encoding!r}, not UTF-8. Output marks will "
            "raise UnicodeEncodeError and turn a successful command into exit 1 "
            "(register D-110). Set PYTHONIOENCODING=utf-8.",
        )
    return Check("console encoding", WARN, f"stdout encoding is {sys.stdout.encoding!r}")


def _check_core_checkout(core_dir: Path | None) -> Check:
    if core_dir is None:
        return Check("core checkout", WARN, "not given; pass --core to check it")
    core_dir = Path(core_dir)
    if not (core_dir / CORE_VERSION_MANIFEST).is_file():
        return Check(
            "core checkout",
            FAIL,
            f"{core_dir} has no {CORE_VERSION_MANIFEST} — is this an orion-core checkout?",
        )
    if not (core_dir / "compose.local.yaml").is_file():
        return Check(
            "core checkout",
            WARN,
            f"{core_dir} has no compose.local.yaml — the local stack predates it",
        )
    return Check("core checkout", OK, str(core_dir))


def _check_core_version(core_dir: Path | None) -> Check:
    """Register D-108: the version the tooling infers is not the version that shipped.

    `installed_core_version()` reads the orion-backend wheel's own version, which
    is 0.7.0 and has not moved since before the Xtend line began. The version
    bundles actually declare compatibility with is in .release-please-manifest.json.
    Validating against the wheel fails every bundle against its own correct compat
    range, blaming the bundle.
    """
    if core_dir is None:
        return Check("core version", WARN, "not given; pass --core to check it")
    resolved = resolve_core_version(core_dir)
    if not resolved:
        return Check(
            "core version",
            FAIL,
            f"could not read the `backend` key from {CORE_VERSION_MANIFEST}",
        )

    try:
        from oxtend.manifest_vendored import installed_core_version

        wheel = installed_core_version()
    except Exception:  # noqa: BLE001 - the wheel check has its own entry below
        wheel = None

    if wheel and wheel != resolved:
        return Check(
            "core version",
            OK,
            f"{resolved} (from {CORE_VERSION_MANIFEST}); the installed wheel says "
            f"{wheel}, which is register D-108 — oxtend uses the manifest, so you "
            "no longer need --core-version by hand",
        )
    return Check("core version", OK, resolved)


def _check_vendored_kernel() -> Check:
    try:
        from oxtend.manifest_vendored import installed_core_version, kernel_manifest_module

        kernel_manifest_module()
        return Check("vendored kernel", OK, f"orion-backend {installed_core_version()}")
    except Exception as exc:  # noqa: BLE001
        return Check(
            "vendored kernel",
            FAIL,
            f"{exc}. oxtend validates with the kernel's own parser and has no fallback; "
            "install it with `pip install -e '.[dev]'`.",
        )


def _check_node() -> Check:
    """Only needed by bundles that ship a `ui/` directory."""
    npm = shutil.which("npm")
    if npm:
        return Check("npm", OK, npm)
    return Check(
        "npm",
        WARN,
        "not on PATH — fine unless a bundle has a ui/ directory to compile; "
        "`oxtend build --skip-ui` avoids it",
    )


def _check_core_reachable(core_url: str, internal_key: str) -> list[Check]:
    """Reachability and, separately, whether the internal key is accepted.

    Split into two results on purpose. `_require_internal_key` compares the header
    against os.environ and treats an EMPTY expected value as a rejection, so a
    server started without ORION_INTERNAL_API_KEY answers 401 to a perfectly
    correct request — which reads as "my key is wrong" and sends people to edit
    the client side of a server-side problem.
    """
    url = f"{core_url.rstrip('/')}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            reachable = Check("core reachable", OK, f"{core_url} → {response.status}")
    except urllib.error.URLError as exc:
        return [
            Check(
                "core reachable",
                WARN,
                f"{core_url} is not answering ({exc.reason}). Start it with "
                "`docker compose -f compose.local.yaml up -d`.",
            )
        ]

    request = urllib.request.Request(
        f"{core_url.rstrip('/')}/kernel/extensions",
        headers={"X-Internal-Key": internal_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            installed = json.loads(response.read().decode("utf-8"))
        scopes = ", ".join(f"{r['scope']}@{r['version']}" for r in installed) or "none"
        return [reachable, Check("kernel API", OK, f"internal key accepted; installed: {scopes}")]
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return [
                reachable,
                Check(
                    "kernel API",
                    FAIL,
                    "401. Either the key does not match, or — more often — the backend "
                    "has no ORION_INTERNAL_API_KEY set at all, which rejects every "
                    "request however correct the header.",
                ),
            ]
        return [reachable, Check("kernel API", FAIL, f"HTTP {exc.code}")]
    except urllib.error.URLError as exc:
        return [reachable, Check("kernel API", FAIL, str(exc.reason))]


def _check_extensions_dir(core_dir: Path | None) -> Check:
    if core_dir is None:
        return Check("extensions dir", WARN, "not given; pass --core to check it")
    ext = Path(core_dir) / "extensions"
    if not ext.is_dir():
        return Check(
            "extensions dir",
            WARN,
            f"{ext} does not exist yet — `oxtend mount` creates it",
        )
    mounted = sorted(
        p.name for p in ext.iterdir() if p.is_dir() and (p / "oxtend.yaml").is_file()
    )
    return Check("extensions dir", OK, f"{ext} ({', '.join(mounted) or 'empty — bare core'})")


def run_doctor(
    *,
    core_dir: Path | None = None,
    core_url: str = DEFAULT_CORE_URL,
    internal_key: str | None = None,
) -> list[Check]:
    key = internal_key if internal_key is not None else os.environ.get(
        "ORION_INTERNAL_API_KEY", DEFAULT_INTERNAL_KEY
    )
    checks = [
        _check_console_encoding(),
        _check_vendored_kernel(),
        _check_core_checkout(core_dir),
        _check_core_version(core_dir),
        _check_extensions_dir(core_dir),
        _check_node(),
    ]
    checks.extend(_check_core_reachable(core_url, key))
    return checks
