"""Signing refusal, lockfile currency, and the unsigned-push gate (ORION-688 tasks 5–7).

The push test is the one with teeth: an unsigned artifact must not be publishable
without an explicit override that CI never passes. Tests that need a real cosign
binary or a registry are marked and skipped rather than mocked into meaninglessness.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from oxtend.build import build_bundle
from oxtend.lock import lock_is_current, read_lock, write_lock
from oxtend.package import PackagingError, image_tag, push_bundle
from oxtend.sign import SigningError, is_signed, sign_bundle

_HAS_COSIGN = shutil.which("cosign") is not None


# ---------------------------------------------------------------------------
# push refuses unsigned
# ---------------------------------------------------------------------------


def test_push_refuses_unsigned_bundle(make_source, manifest) -> None:
    bundle = build_bundle(make_source(manifest), skip_ui=True)
    assert not is_signed(bundle)
    with pytest.raises(PackagingError) as exc:
        push_bundle(bundle, "ghcr.io/ai3xtechnologies")
    assert "unsigned" in str(exc.value)


def test_push_refusal_happens_before_any_registry_call(make_source, manifest, monkeypatch) -> None:
    """The refusal must not depend on a container CLI being present — otherwise the
    gate silently becomes "fails for a different reason" on a machine without docker."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    bundle = build_bundle(make_source(manifest), skip_ui=True)
    with pytest.raises(PackagingError) as exc:
        push_bundle(bundle, "ghcr.io/ai3xtechnologies")
    assert "unsigned" in str(exc.value)


def test_image_tag_namespace_by_kind(make_source, manifest) -> None:
    ext = build_bundle(make_source(manifest), skip_ui=True)
    assert image_tag(ext, "ghcr.io/ai3x") == "ghcr.io/ai3x/orion-extensions/x_fixture:1.0.0"

    client_manifest = {
        **manifest,
        "kind": "client",
        "scope": "x_dilmah",
        "capabilities": {"events": {"emit": [], "subscribe": []}},
    }
    client = build_bundle(make_source(client_manifest, name="client"), skip_ui=True)
    assert image_tag(client, "ghcr.io/ai3x") == "ghcr.io/ai3x/orion-clients/x_dilmah:1.0.0"


def test_image_tag_strips_trailing_slash(make_source, manifest) -> None:
    bundle = build_bundle(make_source(manifest), skip_ui=True)
    assert "//orion-extensions" not in image_tag(bundle, "ghcr.io/ai3x/")


# ---------------------------------------------------------------------------
# sign
# ---------------------------------------------------------------------------


def test_sign_requires_a_built_bundle(tmp_path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(SigningError) as exc:
        sign_bundle(tmp_path / "empty")
    assert "oxtend build" in str(exc.value)


@pytest.mark.skipif(_HAS_COSIGN, reason="cosign is installed; this asserts the absent case")
def test_missing_cosign_is_a_clear_error(make_source, manifest) -> None:
    """oxtend never fakes a signature."""
    bundle = build_bundle(make_source(manifest), skip_ui=True)
    with pytest.raises(SigningError) as exc:
        sign_bundle(bundle)
    assert "cosign" in str(exc.value)


@pytest.mark.requires_cosign
@pytest.mark.skipif(not _HAS_COSIGN, reason="needs a real cosign binary")
def test_sign_then_verify_roundtrip(make_source, manifest, tmp_path, monkeypatch) -> None:
    """Signed bundle verifies; a tampered one does not. Runs only where cosign exists,
    because a mocked cosign proves nothing about the real verification path."""
    import subprocess

    from oxtend.sign import verify_bundle

    key_base = tmp_path / "cosign"
    subprocess.run(
        ["cosign", "generate-key-pair", "--output-key-prefix", str(key_base)],
        check=True,
        capture_output=True,
        # Extend the environment rather than replace it: a bare env has no PATH, so the
        # test only found cosign where it happened to sit in a default system directory.
        env={**os.environ, "COSIGN_PASSWORD": ""},
    )
    bundle = build_bundle(make_source(manifest), skip_ui=True)
    # sign_bundle passes its own environment to cosign, which prompts for the key's
    # passphrase (and fails without a terminal) unless COSIGN_PASSWORD is set there too.
    monkeypatch.setenv("COSIGN_PASSWORD", "")
    sign_bundle(bundle, key=f"{key_base}.key")
    assert verify_bundle(bundle, key=f"{key_base}.pub") is True
    # cosign writes 0600; the kernel reads the signature as the app user, not the signer.
    assert (bundle / "bundle.json.sig").stat().st_mode & 0o777 == 0o644

    meta = json.loads((bundle / "bundle.json").read_text())
    meta["digest"] = "sha256:" + "0" * 64
    (bundle / "bundle.json").write_text(json.dumps(meta))
    assert verify_bundle(bundle, key=f"{key_base}.pub") is False


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------


def test_lock_records_core_and_manifest_digest(make_source, manifest) -> None:
    source = make_source(manifest)
    write_lock(source, core_version="0.7.0")
    lock = read_lock(source)
    assert lock is not None
    assert lock["scope"] == "x_fixture"
    assert lock["core"]["resolved_version"] == "0.7.0"
    assert lock["core"]["declared_compat"] == ">=0.1,<2.0"
    assert lock["manifest_digest"].startswith("sha256:")


def test_lock_is_current_after_writing(make_source, manifest) -> None:
    source = make_source(manifest)
    write_lock(source, core_version="0.7.0")
    ok, reason = lock_is_current(source)
    assert ok, reason


def test_lock_goes_stale_when_the_manifest_changes(make_source, manifest) -> None:
    """A stale lock is worse than a missing one, because it is trusted."""
    import yaml

    source = make_source(manifest)
    write_lock(source, core_version="0.7.0")
    bumped = {**manifest, "version": "1.1.0"}
    (source / "oxtend.yaml").write_text(yaml.safe_dump(bumped, sort_keys=False))

    ok, reason = lock_is_current(source)
    assert not ok
    assert "oxtend.lock" in reason


def test_missing_lock_is_reported_not_assumed(make_source, manifest) -> None:
    ok, reason = lock_is_current(make_source(manifest))
    assert not ok
    assert "absent" in reason


def test_build_ships_the_lock_into_the_bundle(make_source, manifest) -> None:
    """Provenance has to travel with the artifact, not stay in the source repo."""
    source = make_source(manifest)
    write_lock(source, core_version="0.7.0")
    bundle = build_bundle(source, skip_ui=True)
    assert (bundle / "oxtend.lock").is_file()


# ---------------------------------------------------------------------------
# the lock is reproducible, and build honours it (register D-112, D-109)
# ---------------------------------------------------------------------------


def test_relocking_an_untouched_tree_does_not_change_the_bundle_digest(
    make_source, manifest
) -> None:
    """Re-running `oxtend lock` on unchanged source must not change the artifact.

    The lock ships inside the bundle and is hashed into the digest, so a `locked_at`
    refreshed on every run made "rebuilding a tag six months later produces the same
    artifact" false for anyone who followed the documented lock-then-build flow.
    """
    src = make_source(manifest)
    write_lock(src, core_version="1.0.0-dev.2")
    first_locked_at = read_lock(src)["locked_at"]
    first_digest = json.loads((build_bundle(src, skip_ui=True) / "bundle.json").read_text())[
        "digest"
    ]

    write_lock(src, core_version="1.0.0-dev.2")
    assert read_lock(src)["locked_at"] == first_locked_at
    second_digest = json.loads((build_bundle(src, skip_ui=True) / "bundle.json").read_text())[
        "digest"
    ]
    assert second_digest == first_digest


def test_locked_at_still_moves_when_the_lock_describes_something_new(
    make_source, manifest
) -> None:
    """The carried-over stamp must not become a stamp that never updates — it has to
    track the content it describes, or it is worse than having none."""
    src = make_source(manifest)
    write_lock(src, core_version="1.0.0-dev.2")
    first = read_lock(src)["locked_at"]
    write_lock(src, core_version="1.0.0-dev.3")
    second = read_lock(src)
    assert second["locked_at"] != first
    assert second["core"]["resolved_version"] == "1.0.0-dev.3"


def test_build_require_lock_stamps_the_locks_core_version_not_the_installed_one(
    make_source, manifest, monkeypatch
) -> None:
    """`build --require-lock` must not overwrite the lock's resolved core version with
    whatever core happens to be installed on the build host — that makes the bundle
    contradict the lock it ships, which is the lock's whole purpose."""
    from click.testing import CliRunner

    from oxtend.cli import cli

    src = make_source(manifest)
    write_lock(src, core_version="1.0.0-dev.2")
    monkeypatch.setattr("oxtend.cli.installed_core_version", lambda: "1.0.0-dev.9")

    result = CliRunner().invoke(cli, ["build", str(src), "--require-lock", "--skip-ui"])
    assert result.exit_code == 0, result.output

    bundle = src / "build" / f"{manifest['scope']}-{manifest['version']}"
    meta = json.loads((bundle / "bundle.json").read_text())
    assert meta["built_against_core"] == "1.0.0-dev.2"
