"""`_compile_ui_remote` on a machine where the Node toolchain is absent or where a
built `ui/dist` is already present (register D-111).

Two separate faults wore one symptom on Windows: `oxtend build` of a bundle with a
`ui/` tree crashed with `FileNotFoundError: [WinError 2]` from CreateProcess (because
`npm` is `npm.cmd` and bare `subprocess.run(["npm", …])` does not consult PATHEXT), and
it ran the compile unconditionally even for a bundle that ships a built remote. The fix
resolves the executable through `shutil.which` (which finds the shim), raises a *tooling*
error with a remedy instead of a bare traceback, and skips the compile when `dist` is
current. None of this needs a real Node install, so these tests run everywhere.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from oxtend import build as build_mod
from oxtend.build import (
    BuildError,
    ToolingError,
    _compile_ui_remote,
    _resolve_exe,
    _ui_dist_is_current,
)


def _make_ui(root: Path, *, with_dist: bool) -> Path:
    ui = root / "ui"
    (ui / "src").mkdir(parents=True)
    (ui / "src" / "remote-entry.ts").write_text("export const x = 1;\n")
    (ui / "package.json").write_text('{"name":"x-ui"}\n')
    if with_dist:
        (ui / "dist").mkdir()
        (ui / "dist" / "email-citation.js").write_text("// built\n")
    return ui


def test_resolve_exe_raises_tooling_error_with_a_remedy() -> None:
    with pytest.raises(ToolingError) as exc:
        _resolve_exe("definitely-not-a-real-executable-xyz")
    msg = str(exc.value)
    assert "not found on PATH" in msg
    # The remedy, not a bare "not found": install Node, or commit a built dist.
    assert "ui/dist" in msg
    # It is a tooling failure, so the CLI can map it to exit 2, not a wrong bundle.
    assert isinstance(exc.value, BuildError)


def test_dist_is_current_tracks_source_mtimes(tmp_path: Path) -> None:
    ui = _make_ui(tmp_path, with_dist=True)
    # dist was written after src above, so it is current.
    assert _ui_dist_is_current(ui) is True

    # Touch a source newer than dist → stale, must rebuild.
    future = time.time() + 10
    os.utime(ui / "src" / "remote-entry.ts", (future, future))
    assert _ui_dist_is_current(ui) is False

    # node_modules is not an input: with dist the newest real output, a newer file
    # under node_modules must not force a rebuild.
    base = time.time()
    os.utime(ui / "src" / "remote-entry.ts", (base, base))
    os.utime(ui / "dist" / "email-citation.js", (base + 5, base + 5))
    nm = ui / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    f = nm / "index.js"
    f.write_text("x")
    os.utime(f, (base + 10, base + 10))
    assert _ui_dist_is_current(ui) is True


def test_compile_skips_npm_when_dist_is_current(tmp_path: Path, monkeypatch) -> None:
    """A bundle carrying a current built remote packages without a Node toolchain."""
    ext = tmp_path / "x_fixture"
    _make_ui(ext, with_dist=True)
    out = tmp_path / "out"
    out.mkdir()

    # If the compile path were taken it would resolve npm; make that an error so the
    # test fails loudly rather than silently shelling out.
    monkeypatch.setattr(
        build_mod, "_resolve_exe", lambda name: pytest.fail(f"npm resolved for {name}")
    )
    built = _compile_ui_remote(ext, out)
    assert built is True
    assert (out / "remotes" / "email-citation.js").is_file()


def test_compile_raises_tooling_error_when_node_absent(tmp_path: Path, monkeypatch) -> None:
    """Stale/absent dist + no npm → a tooling error, not a CreateProcess traceback."""
    ext = tmp_path / "x_fixture"
    _make_ui(ext, with_dist=False)  # nothing built → compile is required
    out = tmp_path / "out"
    out.mkdir()

    monkeypatch.setattr(build_mod.shutil, "which", lambda name: None)
    with pytest.raises(ToolingError):
        _compile_ui_remote(ext, out)


def test_absent_ui_is_not_an_error(tmp_path: Path) -> None:
    ext = tmp_path / "x_fixture"
    ext.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    assert _compile_ui_remote(ext, out) is False
