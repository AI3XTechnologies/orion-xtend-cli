"""The local development loop — `oxtend mount` / `oxtend dev` (devloop.py).

Two groups here, split by what they need:

* pure-function tests (version resolution, change detection, error rendering) run
  anywhere — they are the ones that pin the D-108 and 401 behaviours;
* `sync_bundle` needs core importable, like the rest of this suite, because it
  builds through the kernel's own manifest parser and digest function.

`install_bundle` is exercised against a real loopback HTTP server rather than a
mocked `urlopen`. The thing worth testing is that we send the header the kernel
checks and read the envelope it returns; a mock of our own call would assert that
we call ourselves the way we call ourselves.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from oxtend.devloop import (
    CORE_VERSION_KEY,
    CORE_VERSION_MANIFEST,
    DevLoopError,
    bundle_target,
    changed_paths,
    container_bundle_dir,
    install_bundle,
    resolve_core_version,
    snapshot,
    sync_bundle,
    touches_ui,
)

# ---------------------------------------------------------------------------
# Core version — register D-108
# ---------------------------------------------------------------------------


def test_core_version_comes_from_the_release_manifest(tmp_path: Path) -> None:
    """The wheel says 0.7.0; what ships is 1.0.0-dev.2. We must read the latter.

    This is the whole of D-108: bundles declare `compat: ">=1.0.0-dev.2,<2.0"`, so
    validating against the wheel version fails every bundle against its own correct
    range and blames the bundle.
    """
    (tmp_path / CORE_VERSION_MANIFEST).write_text(
        json.dumps({CORE_VERSION_KEY: "1.0.0-dev.2", "chat-ui": "1.0.0-dev.1"})
    )
    assert resolve_core_version(tmp_path) == "1.0.0-dev.2"


def test_core_version_is_none_when_absent_or_unreadable(tmp_path: Path) -> None:
    """Callers fall back rather than hard-fail on a checkout without the manifest."""
    assert resolve_core_version(tmp_path) is None
    (tmp_path / CORE_VERSION_MANIFEST).write_text("{not json")
    assert resolve_core_version(tmp_path) is None


def test_bundle_target_is_the_scope_dir_under_extensions(tmp_path: Path) -> None:
    assert bundle_target(tmp_path, "x_demo") == tmp_path / "extensions" / "x_demo"


def test_container_bundle_dir_is_posix_regardless_of_host(tmp_path: Path) -> None:
    """The path is sent to a Linux container; a Windows separator would not resolve."""
    assert container_bundle_dir("x_demo") == "/extensions/x_demo"
    assert container_bundle_dir("x_demo", "/mnt/bundles/") == "/mnt/bundles/x_demo"


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def test_snapshot_skips_build_output(tmp_path: Path) -> None:
    """`build/` must never be watched: it is our own output, and watching it loops."""
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "a.yaml").write_text("a")
    (tmp_path / "build" / "x-1.0.0").mkdir(parents=True)
    (tmp_path / "build" / "x-1.0.0" / "a.yaml").write_text("a")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("x")

    keys = set(snapshot(tmp_path))
    assert keys == {str(Path("metadata/a.yaml"))}


def test_changed_paths_detects_edit_add_and_delete(tmp_path: Path) -> None:
    before = {"a": 1.0, "b": 2.0}
    assert changed_paths(before, {"a": 1.0, "b": 2.0}) == set()
    assert changed_paths(before, {"a": 9.0, "b": 2.0}) == {"a"}
    assert changed_paths(before, {"a": 1.0, "b": 2.0, "c": 3.0}) == {"c"}
    assert changed_paths(before, {"a": 1.0}) == {"b"}


def test_touches_ui_decides_whether_npm_runs() -> None:
    """A metadata-only change must skip the UI compile — that is the speed of the loop."""
    assert touches_ui({"ui/src/Panel.tsx"})
    assert touches_ui({str(Path("ui/src/Panel.tsx"))})
    assert not touches_ui({"metadata/fields/a.field.yaml", "migrations/001.sql"})
    assert not touches_ui(set())


# ---------------------------------------------------------------------------
# install_bundle — against a real server
# ---------------------------------------------------------------------------


class _KernelStub(BaseHTTPRequestHandler):
    """Stands in for POST /kernel/extensions/install. Set `.behaviour` per test."""

    behaviour = "ok"
    seen: dict = {}

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        length = int(self.headers.get("Content-Length", 0))
        _KernelStub.seen = {
            "path": self.path,
            "key": self.headers.get("X-Internal-Key"),
            "body": json.loads(self.rfile.read(length) or b"{}"),
        }
        if _KernelStub.behaviour == "ok":
            self._send(
                200,
                {"scope": "x_demo", "version": "0.1.0", "kind": "extension", "status": "active"},
            )
        elif _KernelStub.behaviour == "401":
            self._send(401, {"detail": "Missing or invalid X-Internal-Key"})
        elif _KernelStub.behaviour == "403":
            self._send(
                403,
                {"detail": {"code": "not_entitled", "message": "x_demo is not entitled"}},
            )
        else:
            self._send(400, {"detail": {"code": "manifest_invalid", "message": "bad manifest"}})

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # silence the test run
        return


@pytest.fixture
def kernel_stub():
    server = HTTPServer(("127.0.0.1", 0), _KernelStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_install_sends_the_scope_and_the_internal_key(kernel_stub: str) -> None:
    _KernelStub.behaviour = "ok"
    result = install_bundle("x_demo", core_url=kernel_stub, internal_key="k")

    assert result["status"] == "active"
    assert _KernelStub.seen["path"] == "/kernel/extensions/install"
    assert _KernelStub.seen["key"] == "k"
    # bundle_dir omitted, so the endpoint resolves extensions_dir()/scope itself —
    # a host path sent here would not exist inside the container.
    assert _KernelStub.seen["body"] == {"scope": "x_demo"}


def test_install_passes_bundle_dir_when_given(kernel_stub: str) -> None:
    _KernelStub.behaviour = "ok"
    install_bundle("x_demo", core_url=kernel_stub, internal_key="k", bundle_dir="/mnt/b/x_demo")
    assert _KernelStub.seen["body"]["bundle_dir"] == "/mnt/b/x_demo"


def test_401_names_the_server_side_cause(kernel_stub: str) -> None:
    """An unset ORION_INTERNAL_API_KEY on the SERVER rejects every correct header.

    `_require_internal_key` compares against os.environ and treats an empty
    expected value as a rejection, so the obvious reading of 401 — "my key is
    wrong" — sends people to fix the client side of a server-side problem.
    """
    _KernelStub.behaviour = "401"
    with pytest.raises(DevLoopError) as exc:
        install_bundle("x_demo", core_url=kernel_stub, internal_key="k")
    assert "UNSET" in str(exc.value)


def test_403_points_at_the_dev_mode_switches(kernel_stub: str) -> None:
    _KernelStub.behaviour = "403"
    with pytest.raises(DevLoopError) as exc:
        install_bundle("x_demo", core_url=kernel_stub, internal_key="k")
    assert "not entitled" in str(exc.value)
    assert "ORION_ENTITLEMENTS_ENFORCED" in str(exc.value)


def test_4xx_surfaces_the_kernel_error_message(kernel_stub: str) -> None:
    _KernelStub.behaviour = "400"
    with pytest.raises(DevLoopError) as exc:
        install_bundle("x_demo", core_url=kernel_stub, internal_key="k")
    assert "bad manifest" in str(exc.value)


def test_unreachable_core_says_so_and_suggests_the_fix() -> None:
    with pytest.raises(DevLoopError) as exc:
        # Port 1 is reserved and never listening.
        install_bundle("x_demo", core_url="http://127.0.0.1:1", internal_key="k", timeout=2)
    assert "cannot reach core" in str(exc.value)
    assert "compose.local.yaml" in str(exc.value)


# ---------------------------------------------------------------------------
# sync_bundle — needs core importable
# ---------------------------------------------------------------------------


def test_sync_builds_into_the_extensions_mount(tmp_path: Path, manifest, make_source) -> None:
    """The mounted directory IS the build output — no copy step to forget."""
    source = make_source(
        manifest,
        migrations={"001_a.sql": "SET LOCAL search_path = x_fixture;\nSELECT 1;"},
    )
    core = tmp_path / "core"
    (core / "extensions").mkdir(parents=True)
    (core / CORE_VERSION_MANIFEST).write_text(json.dumps({CORE_VERSION_KEY: "1.0.0-dev.2"}))

    out = sync_bundle(source, core, skip_ui=True)

    assert out == core / "extensions" / "x_fixture"
    assert (out / "oxtend.yaml").is_file()
    assert (out / "migrations" / "001_a.sql").is_file()
    meta = json.loads((out / "bundle.json").read_text())
    # The core version came from the checkout, not from the installed wheel.
    assert meta["built_against_core"] == "1.0.0-dev.2"


def test_sync_removes_files_deleted_from_source(tmp_path: Path, manifest, make_source) -> None:
    """A replace, not a merge.

    `cp -a` over the top — the documented procedure — leaves a file you deleted
    from the source sitting in the mount, and the kernel keeps installing it.
    """
    source = make_source(
        manifest,
        migrations={"001_a.sql": "SET LOCAL search_path = x_fixture;\nSELECT 1;"},
    )
    core = tmp_path / "core"
    (core / "extensions").mkdir(parents=True)
    (core / CORE_VERSION_MANIFEST).write_text(json.dumps({CORE_VERSION_KEY: "1.0.0-dev.2"}))

    out = sync_bundle(source, core, skip_ui=True)
    assert (out / "migrations" / "001_a.sql").is_file()

    (source / "migrations" / "001_a.sql").unlink()
    out = sync_bundle(source, core, skip_ui=True)
    assert not (out / "migrations" / "001_a.sql").exists()
