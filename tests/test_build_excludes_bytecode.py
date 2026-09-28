"""`oxtend build` must not ship bytecode, because the digest hashes everything.

`compute_bundle_digest` walks every file under the bundle, so a stray
`__pycache__` inside `backend/` or `dags/` becomes part of the digest. Bytecode is
written by whichever interpreter last imported the source, which meant the same
commit hashed differently depending on whether anyone had run the tests first --
and the kernel reports that as "the bundle was modified after it was built", which
names tampering for what is really a leftover directory. Register D-114 was the
same shape, and that one was a sort order.

The property worth pinning is not "the file is absent" but "the digest does not
move", so that is what the first test asserts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxtend.build import build_bundle

pytestmark = pytest.mark.usefixtures("manifest")


def _bytecode_into(root: Path) -> None:
    """Drop bytecode where a real checkout grows it: beside the source it compiles."""
    for rel in ("backend/python/x_fixture", "dags"):
        cache = root / rel / "__pycache__"
        if not (root / rel).is_dir():
            continue
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "api.cpython-313.pyc").write_bytes(b"\xcb\x0d\x0d\x0a stale bytecode")
        (cache / "api.cpython-311.pyc").write_bytes(b"\xa7\x0d\x0d\x0a other interpreter")


def _digest(bundle: Path) -> str:
    import json

    return json.loads((bundle / "bundle.json").read_text())["digest"]


def test_bytecode_does_not_change_the_digest(make_source, manifest, tmp_path) -> None:
    """The whole point: two machines, same source, same digest.

    One has run the tests and has __pycache__; the other is a fresh clone.
    """
    clean = make_source(manifest, python={"x_fixture/api.py": "router = None\n"}, name="clean")
    dirty = make_source(manifest, python={"x_fixture/api.py": "router = None\n"}, name="dirty")
    _bytecode_into(dirty)

    a = build_bundle(clean, tmp_path / "out-a", core_version="1.0.0-dev.2", skip_ui=True)
    b = build_bundle(dirty, tmp_path / "out-b", core_version="1.0.0-dev.2", skip_ui=True)

    assert _digest(a) == _digest(b), (
        "a stray __pycache__ changed the bundle digest — the kernel would refuse "
        "this bundle with 'modified after it was built'"
    )


def test_bytecode_is_not_copied_into_the_bundle(make_source, manifest, tmp_path) -> None:
    """And it is genuinely absent, not merely excluded from the hash."""
    source = make_source(manifest, python={"x_fixture/api.py": "router = None\n"})
    _bytecode_into(source)

    bundle = build_bundle(source, tmp_path / "out", core_version="1.0.0-dev.2", skip_ui=True)

    assert list(bundle.rglob("__pycache__")) == []
    assert list(bundle.rglob("*.pyc")) == []


def test_the_source_it_shadows_still_ships(make_source, manifest, tmp_path) -> None:
    """Excluding bytecode must not exclude the module it was compiled from."""
    source = make_source(manifest, python={"x_fixture/api.py": "router = None\n"})
    _bytecode_into(source)

    bundle = build_bundle(source, tmp_path / "out", core_version="1.0.0-dev.2", skip_ui=True)

    assert (bundle / "backend" / "python" / "x_fixture" / "api.py").is_file()


@pytest.mark.parametrize("junk", [".pytest_cache", ".mypy_cache", ".ruff_cache"])
def test_tool_caches_are_excluded_too(make_source, manifest, tmp_path, junk) -> None:
    """They appear inside shipped directories for the same reason bytecode does."""
    source = make_source(manifest, python={"x_fixture/api.py": "router = None\n"})
    cache = source / "backend" / junk
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "CACHEDIR.TAG").write_text("junk")

    bundle = build_bundle(source, tmp_path / "out", core_version="1.0.0-dev.2", skip_ui=True)

    assert not (bundle / "backend" / junk).exists()
