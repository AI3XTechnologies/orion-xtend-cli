"""`build` output shape and digest (ORION-688 task 3).

The digest assertion is the load-bearing one: ORION-665 *reads* this value instead of
recomputing it on every boot, so if `bundle.json`'s digest ever stops matching an
independent recomputation, the kernel trusts a wrong number.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxtend.build import build_bundle
from oxtend.validate import validate_bundle


def test_bundle_json_shape(make_source, manifest) -> None:
    source = make_source(manifest)
    bundle = build_bundle(source, skip_ui=True)
    meta = json.loads((bundle / "bundle.json").read_text())

    for key in ("scope", "version", "kind", "digest", "manifest", "built_at", "core_compat"):
        assert key in meta, f"bundle.json is missing {key!r}"
    assert meta["scope"] == "x_fixture"
    assert meta["digest"].startswith("sha256:")
    assert meta["core_compat"] == ">=0.1,<2.0"


def test_digest_matches_independent_recomputation(make_source, manifest, scoped_sql) -> None:
    from orion.kernel.digest import compute_bundle_digest

    source = make_source(
        manifest, migrations={"0001_init.sql": scoped_sql("x_fixture", "CREATE TABLE x_fixture.t (id INT);")}
    )
    bundle = build_bundle(source, skip_ui=True)
    claimed = json.loads((bundle / "bundle.json").read_text())["digest"]
    assert claimed == compute_bundle_digest(bundle)


def test_kernel_verifies_the_digest_we_wrote(make_source, manifest) -> None:
    """End-to-end on the contract that matters: what oxtend writes, the kernel accepts."""
    from orion.kernel.digest import verify_bundle_digest

    bundle = build_bundle(make_source(manifest), skip_ui=True)
    assert verify_bundle_digest(bundle)


def test_verbatim_directories_are_copied(make_source, manifest, scoped_sql) -> None:
    source = make_source(
        manifest,
        migrations={"0001_init.sql": scoped_sql("x_fixture", "CREATE TABLE x_fixture.t (id INT);")},
        fields={"region.field.yaml": {"entity": "documents", "field": {"name": "region", "type": "string"}}},
        python={"x_fixture/__init__.py": "", "x_fixture/api.py": "router = None\n"},
        dags={"x_fixture_ingest.py": "# dag\n"},
    )
    bundle = build_bundle(source, skip_ui=True)
    assert (bundle / "migrations" / "0001_init.sql").is_file()
    assert (bundle / "metadata" / "fields" / "region.field.yaml").is_file()
    assert (bundle / "backend" / "python" / "x_fixture" / "api.py").is_file()
    assert (bundle / "dags" / "x_fixture_ingest.py").is_file()


def test_absent_ui_is_not_an_error(make_source, manifest) -> None:
    bundle = build_bundle(make_source(manifest))
    assert bundle.is_dir()
    assert not (bundle / "remotes").exists()


def test_rebuild_clears_stale_files(make_source, manifest) -> None:
    """A leftover from a previous build would be hashed into the digest and shipped."""
    source = make_source(manifest)
    bundle = build_bundle(source, skip_ui=True)
    stale = bundle / "stale.txt"
    stale.write_text("left over")
    rebuilt = build_bundle(source, skip_ui=True)
    assert not (rebuilt / "stale.txt").exists()


def test_prebuilt_remote_without_ui_sources_still_ships(make_source, manifest, tmp_path) -> None:
    source = make_source(manifest)
    (source / "remotes").mkdir()
    (source / "remotes" / "panel.js").write_text("export default 1;")
    bundle = build_bundle(source, skip_ui=True)
    assert (bundle / "remotes" / "panel.js").is_file()


def test_build_refuses_an_invalid_manifest(make_source, manifest) -> None:
    from orion.kernel.errors import ManifestError

    source = make_source({**manifest, "scope": "not_prefixed"})
    with pytest.raises(ManifestError):
        build_bundle(source, skip_ui=True)


def test_validate_then_build_agree(make_source, manifest, scoped_sql) -> None:
    """Whatever validate accepts, build must be able to assemble — the two must not
    apply different rules."""
    source = make_source(
        manifest,
        migrations={"0001.sql": scoped_sql("x_fixture", "CREATE TABLE x_fixture.t (id INT);")},
        fields={"a.field.yaml": {"entity": "documents", "field": {"name": "a", "type": "string"}}},
    )
    result = validate_bundle(source)
    assert result.ok, result.errors
    assert build_bundle(source, skip_ui=True).is_dir()
