"""`oxtend reset` — undo a scope's install so it can be installed again.

Bundle migrations are append-only and checksum-ledgered: `registry.install()`
records `(scope, filename, checksum)` in `orion.extension_migrations_applied`, and
re-running a file whose bytes changed raises `MigrationChecksumMismatch`. That is
exactly right in production — an applied migration is history and editing history
silently is how two deployments end up with different schemas from the same
version string.

It is also completely at odds with writing one. The first draft of a migration is
wrong; you find out by applying it. With no way back, the developer's options were
to append `002_fix_001.sql`, `003_fix_002.sql`, … into a bundle that has never
shipped, or to reach into Postgres by hand. Both happened.

This restores the "never installed" state for ONE scope: its ledger rows, the
registry rows derived from its manifest, and its schema.

    oxtend reset x_demo --yes

Destructive and local-only by construction — it refuses a DSN that is not
loopback unless `--force` is passed, because the one thing this must never be is
convenient to point at a shared database.

Called by: oxtend/cli.py (`reset`).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from oxtend.manifest_vendored import kernel_manifest_module

#: Cleared in this order — children before the install row that owns them, so an
#: interrupted reset never leaves a scope that reads as installed but has had its
#: fields or slots removed underneath it.
#:
#: entitlement_registry is deliberately LAST and separately flagged: a grant is
#: not part of the install, and under ORION_DEV_MODE the kernel re-grants anyway.
SCOPED_TABLES = (
    "orion.extension_migrations_applied",
    "orion.custom_field_registry",
    "orion.access_rule_registry",
    "orion.ui_slot_registry",
    "orion.extension_installs",
)

ENTITLEMENT_TABLE = "orion.entitlement_registry"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "", None})


class ResetError(RuntimeError):
    pass


def assert_safe_scope(scope: str) -> None:
    """Reject anything that is not a scope before it reaches SQL.

    Uses the kernel's own `SCOPE_RE` rather than a copy. A local regex here would
    be the exact drift this repo has a source-scanning test to prevent — and the
    stakes are higher than for validation, because this string is interpolated
    into `DROP SCHEMA`. If the kernel ever widens what a scope may contain, this
    guard widens with it, in step.
    """
    if not kernel_manifest_module().SCOPE_RE.match(scope or ""):
        pattern = kernel_manifest_module().SCOPE_RE.pattern
        raise ResetError(
            f"{scope!r} is not a valid scope (expected {pattern}). "
            "Refusing to build SQL from it."
        )


def is_loopback(dsn: str) -> bool:
    """Whether this DSN points at the developer's own machine."""
    try:
        host = urlsplit(dsn).hostname
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def assert_local(dsn: str, *, force: bool) -> None:
    if force or is_loopback(dsn):
        return
    host = urlsplit(dsn).hostname
    raise ResetError(
        f"refusing to reset against {host!r}, which is not loopback. This drops a schema "
        "and deletes ledger rows; on a shared or deployed database that is an incident, "
        "not an inconvenience. Pass --force if you are certain."
    )


async def reset_scope(
    dsn: str,
    scope: str,
    *,
    drop_schema: bool = True,
    revoke_entitlement: bool = False,
) -> dict[str, int | bool]:
    """Delete the scope's rows and (optionally) drop its schema.

    Returns a per-table row count plus `schema_dropped`, so the caller can report
    what actually happened rather than claiming success uniformly. Resetting a
    scope that was never installed is a no-op that reports all zeros — not an
    error, because the whole point is to reach a known state.

    Runs in ONE transaction. A half-reset — ledger cleared, schema still holding
    the old tables — reinstalls into a dirty schema and fails in a way that looks
    nothing like its cause.
    """
    assert_safe_scope(scope)

    # Imported lazily: SQLAlchemy and asyncpg arrive transitively via the pinned
    # orion-backend wheel, and every other oxtend command runs without touching a
    # database. Importing at module scope would make `oxtend --help` pay for it.
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    results: dict[str, int | bool] = {}
    try:
        async with engine.begin() as conn:
            for table in SCOPED_TABLES:
                result = await conn.execute(
                    text(f"DELETE FROM {table} WHERE scope = :scope"), {"scope": scope}
                )
                results[table] = result.rowcount or 0

            if revoke_entitlement:
                result = await conn.execute(
                    text(f"DELETE FROM {ENTITLEMENT_TABLE} WHERE scope = :scope"),
                    {"scope": scope},
                )
                results[ENTITLEMENT_TABLE] = result.rowcount or 0

            if drop_schema:
                # The scope IS the schema name (kernel/db_roles.py quotes the scope
                # directly, and the migration gate rejects any identifier resolving
                # outside it). assert_safe_scope above is what makes this
                # interpolation safe; a schema name cannot be a bind parameter.
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{scope}" CASCADE'))
                results["schema_dropped"] = True
            else:
                results["schema_dropped"] = False
    finally:
        await engine.dispose()

    return results
