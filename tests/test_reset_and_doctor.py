"""`oxtend reset` guards and `oxtend doctor` checks.

`reset_scope` itself needs a live Postgres and is exercised by the local
verification run, not here. What IS tested here is everything that stands between
a typo and a dropped schema — the guards are the part that must not regress,
because the failure mode is destructive and silent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from oxtend.doctor import FAIL, OK, WARN, run_doctor
from oxtend.reset import (
    SCOPED_TABLES,
    ResetError,
    assert_local,
    assert_safe_scope,
    is_loopback,
)

# ---------------------------------------------------------------------------
# reset guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["x_demo", "x_orion_email", "x_ab", "x_" + "a" * 48])
def test_valid_scopes_are_accepted(scope: str) -> None:
    assert_safe_scope(scope)


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "demo",                      # no x_ prefix
        "X_DEMO",                    # kernel scopes are lower-case
        "x_",                        # too short
        "x_" + "a" * 49,             # too long
        "x_demo; DROP SCHEMA orion", # the reason this check exists
        "x_demo--",
        'x_demo"',
        "public",
        None,
    ],
)
def test_bad_scopes_are_refused_before_reaching_sql(scope: str) -> None:
    """The scope is interpolated into DROP SCHEMA — it cannot be a bind parameter.

    That makes this regex the only thing between a mistyped argument and a dropped
    schema, so it is checked here rather than trusted from the manifest.
    """
    with pytest.raises(ResetError):
        assert_safe_scope(scope)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql+asyncpg://orion:p@localhost:15432/orion",
        "postgresql+asyncpg://orion:p@127.0.0.1:5432/orion",
    ],
)
def test_loopback_dsns_are_allowed(dsn: str) -> None:
    assert is_loopback(dsn)
    assert_local(dsn, force=False)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql+asyncpg://orion:p@10.50.33.57:5432/orion",
        "postgresql+asyncpg://orion:p@orion-xtend.ai3x.tech:5432/orion",
        "postgresql+asyncpg://orion:p@orion-postgres:5432/orion",
    ],
)
def test_remote_dsns_are_refused_without_force(dsn: str) -> None:
    """Dropping a schema on a shared box is an incident, not an inconvenience."""
    assert not is_loopback(dsn)
    with pytest.raises(ResetError) as exc:
        assert_local(dsn, force=False)
    assert "--force" in str(exc.value)
    # ...and --force is the documented, explicit way through.
    assert_local(dsn, force=True)


def test_install_row_is_deleted_after_its_children() -> None:
    """Order matters: an interrupted reset must not leave a live install row whose
    fields and slots have already been removed underneath it."""
    assert SCOPED_TABLES[-1] == "orion.extension_installs"
    assert "orion.extension_migrations_applied" in SCOPED_TABLES
    # Entitlements are not part of the install and are opt-in to clear.
    assert "orion.entitlement_registry" not in SCOPED_TABLES


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _by_name(checks, name):
    return next(c for c in checks if c.name == name)


def test_doctor_reads_the_core_version_from_the_checkout(tmp_path: Path) -> None:
    (tmp_path / ".release-please-manifest.json").write_text('{"backend": "1.0.0-dev.2"}')
    (tmp_path / "compose.local.yaml").write_text("name: orion-local\n")
    (tmp_path / "extensions").mkdir()

    checks = run_doctor(core_dir=tmp_path, core_url="http://127.0.0.1:1")

    assert _by_name(checks, "core checkout").status == OK
    version = _by_name(checks, "core version")
    assert version.status == OK
    assert "1.0.0-dev.2" in version.detail


def test_doctor_rejects_a_directory_that_is_not_core(tmp_path: Path) -> None:
    checks = run_doctor(core_dir=tmp_path, core_url="http://127.0.0.1:1")
    assert _by_name(checks, "core checkout").status == FAIL


def test_doctor_warns_rather_than_fails_when_core_is_not_running(tmp_path: Path) -> None:
    """Not having the stack up is a normal state, not a broken machine."""
    (tmp_path / ".release-please-manifest.json").write_text('{"backend": "1.0.0-dev.2"}')
    checks = run_doctor(core_dir=tmp_path, core_url="http://127.0.0.1:1")
    reachable = _by_name(checks, "core reachable")
    assert reachable.status == WARN
    assert "compose.local.yaml" in reachable.detail
    # ...and it does not then claim anything about the kernel API it never reached.
    assert not any(c.name == "kernel API" for c in checks)


def test_doctor_reports_mounted_bundles(tmp_path: Path) -> None:
    (tmp_path / ".release-please-manifest.json").write_text('{"backend": "1.0.0-dev.2"}')
    ext = tmp_path / "extensions" / "x_demo"
    ext.mkdir(parents=True)
    (ext / "oxtend.yaml").write_text("scope: x_demo\n")
    (tmp_path / "extensions" / "not_a_bundle").mkdir()

    checks = run_doctor(core_dir=tmp_path, core_url="http://127.0.0.1:1")
    detail = _by_name(checks, "extensions dir").detail
    assert "x_demo" in detail
    # A directory without a manifest is invisible to the kernel, so it is invisible here.
    assert "not_a_bundle" not in detail


def test_doctor_checks_the_console_encoding() -> None:
    """Register D-110. main() reconfigures the streams, so under pytest this passes;
    the check exists so a console that somehow is not UTF-8 says so in words rather
    than by making a successful command exit 1."""
    check = _by_name(run_doctor(core_url="http://127.0.0.1:1"), "console encoding")
    assert check.status in {OK, WARN, FAIL}
    if check.status == FAIL:
        assert "D-110" in check.detail
    assert sys.stdout is not None
