"""The `oxtend` command line (SPEC-67 §5.1, ORION-688).

    oxtend add-field     <ext_dir> --entity E --name N --type T [ui options]
    oxtend validate      <ext_dir>
    oxtend build         <ext_dir> [--out DIR] [--require-lock]
    oxtend contract-test <ext_dir> --core-version X.Y.Z [--openapi FILE]
    oxtend lock          <ext_dir>
    oxtend sign          <ext_dir|--bundle DIR> [--key FILE]
    oxtend package       <ext_dir> --registry HOST
    oxtend push          <ext_dir> --registry HOST [--allow-unsigned]
    oxtend all           <ext_dir> --registry HOST

and the local development loop:

    oxtend doctor        [--core DIR]
    oxtend mount         <ext_dir> --core DIR
    oxtend dev           <ext_dir> --core DIR
    oxtend reset         <scope> [--yes]

Exit codes are the interface CI actually consumes: 0 success, 1 validation/contract
failure, 2 tooling failure (cosign absent, no container CLI). A build tool that
returns 0 on a soft failure is a build tool that ships broken artifacts.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import click

from oxtend import __version__
from oxtend.build import BuildError, build_bundle
from oxtend.contract_test import run_contract_test
from oxtend.devloop import (
    DEFAULT_CORE_URL,
    DEFAULT_INTERNAL_KEY,
    DevLoopError,
    bundle_target,
    changed_paths,
    install_bundle,
    load_scope,
    resolve_core_version,
    snapshot,
    sync_bundle,
    touches_ui,
)
from oxtend.lock import lock_is_current, write_lock
from oxtend.manifest_vendored import (
    VendoredKernelUnavailable,
    installed_core_version,
)
from oxtend.package import PackagingError, package_bundle, push_bundle
from oxtend.sign import SigningError, sign_bundle
from oxtend.validate import validate_bundle

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_TOOLING = 2

_DIR = click.Path(exists=True, file_okay=False, path_type=Path, resolve_path=True)


def _ok(message: str) -> None:
    click.secho(f"✓ {message}", fg="green")


def _warn(message: str) -> None:
    click.secho(f"! {message}", fg="yellow")


def _fail(message: str, code: int = EXIT_INVALID) -> None:
    click.secho(f"✗ {message}", fg="red", err=True)
    sys.exit(code)


@click.group()
@click.version_option(__version__, prog_name="oxtend")
def cli() -> None:
    """Build, sign, and publish Orion Xtend bundles."""


@cli.command()
@click.argument("ext_dir", type=_DIR)
@click.option("--core-version", default=None, help="Check core.compat against this version.")
def validate(ext_dir: Path, core_version: str | None) -> None:
    """Validate the manifest, metadata, and scoped migrations."""
    try:
        result = validate_bundle(ext_dir, core_version=core_version)
    except VendoredKernelUnavailable as exc:
        _fail(str(exc), EXIT_TOOLING)
        return
    for warning in result.warnings:
        _warn(warning)
    if not result.ok:
        for error in result.errors:
            click.secho(f"✗ {error}", fg="red", err=True)
        _fail(f"{len(result.errors)} validation error(s) in {ext_dir}")
    _ok(f"{result.scope}@{result.version} ({result.kind}) is valid")


@cli.command()
@click.argument("ext_dir", type=_DIR)
@click.option("--out", "out_dir", type=click.Path(path_type=Path), default=None)
@click.option("--skip-ui", is_flag=True, help="Skip compiling ui/ (for a metadata-only rebuild).")
@click.option(
    "--require-lock",
    is_flag=True,
    help="Fail unless oxtend.lock is present and current. CI passes this on release builds.",
)
@click.pass_context
def build(ctx: click.Context, ext_dir: Path, out_dir: Path | None, skip_ui: bool, require_lock: bool) -> None:
    """Compile the UI remote and assemble the bundle directory."""
    ctx.invoke(validate, ext_dir=ext_dir, core_version=None)
    if require_lock:
        ok, reason = lock_is_current(ext_dir)
        if not ok:
            _fail(reason)
    try:
        bundle = build_bundle(
            ext_dir, out_dir, core_version=installed_core_version(), skip_ui=skip_ui
        )
    except BuildError as exc:
        _fail(str(exc))
        return
    meta = json.loads((bundle / "bundle.json").read_text())
    _ok(f"bundle assembled at {bundle} (digest {meta['digest'][:19]}…)")


@cli.command(name="contract-test")
@click.argument("ext_dir", type=_DIR)
@click.option("--core-version", required=True, help="The core version to test the contract against.")
@click.option(
    "--openapi",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Core's OpenAPI schema JSON. Without it, endpoint existence is not checked.",
)
def contract_test(ext_dir: Path, core_version: str, openapi: Path | None) -> None:
    """Assert the extension can actually run on the core it claims to support."""
    schema = json.loads(openapi.read_text()) if openapi else None
    result = run_contract_test(ext_dir, core_version=core_version, openapi=schema)
    for warning in result.warnings:
        _warn(warning)
    if not result.ok:
        for error in result.errors:
            click.secho(f"✗ {error}", fg="red", err=True)
        _fail(f"{len(result.errors)} contract violation(s) against core {core_version}")
    _ok(
        f"contract holds against core {core_version} "
        f"({result.checked_symbols} symbols, {result.checked_endpoints} endpoints, "
        f"{result.checked_topics} topics, {result.checked_field_types} field types)"
    )


@cli.command()
@click.argument("ext_dir", type=_DIR)
@click.option("--core-version", default=None)
def lock(ext_dir: Path, core_version: str | None) -> None:
    """Write oxtend.lock — resolved core version, manifest digest, UI dep tree."""
    path = write_lock(ext_dir, core_version=core_version)
    _ok(f"wrote {path}")


@cli.command()
@click.argument("ext_dir", type=_DIR)
@click.option("--bundle", "bundle_dir", type=click.Path(path_type=Path), default=None)
@click.option("--key", default=None, help="Local cosign key file. Omit for keyless OIDC (CI).")
def sign(ext_dir: Path, bundle_dir: Path | None, key: str | None) -> None:
    """cosign-sign the built bundle's bundle.json."""
    target = bundle_dir or _default_bundle_dir(ext_dir)
    try:
        sig = sign_bundle(target, key=key)
    except SigningError as exc:
        _fail(str(exc), EXIT_TOOLING)
        return
    _ok(f"signed → {sig}")


@cli.command()
@click.argument("ext_dir", type=_DIR)
@click.option("--registry", required=True, help="e.g. ghcr.io/ai3xtechnologies")
@click.option("--bundle", "bundle_dir", type=click.Path(path_type=Path), default=None)
def package(ext_dir: Path, registry: str, bundle_dir: Path | None) -> None:
    """Package the built bundle as an OCI image."""
    target = bundle_dir or _default_bundle_dir(ext_dir)
    try:
        tag = package_bundle(target, registry)
    except PackagingError as exc:
        _fail(str(exc), EXIT_TOOLING)
        return
    _ok(f"packaged {tag}")


@cli.command()
@click.argument("ext_dir", type=_DIR)
@click.option("--registry", required=True)
@click.option("--bundle", "bundle_dir", type=click.Path(path_type=Path), default=None)
@click.option(
    "--allow-unsigned",
    is_flag=True,
    help="Push without a signature. For local registries only — CI never passes this.",
)
def push(ext_dir: Path, registry: str, bundle_dir: Path | None, allow_unsigned: bool) -> None:
    """Push the packaged bundle image."""
    target = bundle_dir or _default_bundle_dir(ext_dir)
    try:
        tag = push_bundle(target, registry, allow_unsigned=allow_unsigned)
    except PackagingError as exc:
        _fail(str(exc), EXIT_TOOLING if "CLI" in str(exc) else EXIT_INVALID)
        return
    if allow_unsigned:
        _warn("pushed WITHOUT a signature — the kernel will refuse this bundle in prod")
    _ok(f"pushed {tag}")


@cli.command(name="all")
@click.argument("ext_dir", type=_DIR)
@click.option("--registry", required=True)
@click.option("--core-version", default=None)
@click.option("--openapi", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--key", default=None)
@click.option("--allow-unsigned", is_flag=True)
@click.pass_context
def run_all(
    ctx: click.Context,
    ext_dir: Path,
    registry: str,
    core_version: str | None,
    openapi: Path | None,
    key: str | None,
    allow_unsigned: bool,
) -> None:
    """validate → lock → build → contract-test → sign → package → push."""
    resolved_core = core_version or installed_core_version()
    ctx.invoke(validate, ext_dir=ext_dir, core_version=resolved_core)
    ctx.invoke(lock, ext_dir=ext_dir, core_version=resolved_core)
    ctx.invoke(build, ext_dir=ext_dir, out_dir=None, skip_ui=False, require_lock=True)
    ctx.invoke(contract_test, ext_dir=ext_dir, core_version=resolved_core, openapi=openapi)
    if not allow_unsigned:
        ctx.invoke(sign, ext_dir=ext_dir, bundle_dir=None, key=key)
    ctx.invoke(package, ext_dir=ext_dir, registry=registry, bundle_dir=None)
    ctx.invoke(push, ext_dir=ext_dir, registry=registry, bundle_dir=None, allow_unsigned=allow_unsigned)


@cli.command(name="add-field")
@click.argument("ext_dir", type=_DIR)
@click.option("--entity", required=True, help="Core entity: collections, documents, chunks.")
@click.option("--name", required=True, help="Field name — lower snake_case, scope-prefixed.")
@click.option(
    "--type", "field_type", default="string",
    help="string|int|float|bool|date|enum|string[].",
)
@click.option("--enum-values", default=None, help="Comma-separated values (for --type enum).")
@click.option("--required", is_flag=True, help="Reject a write that omits this field.")
@click.option("--indexed", is_flag=True, help="Create an index so the field is filter-fast.")
@click.option("--max-length", type=int, default=None, help="Max length (string / string[] only).")
@click.option("--label", default=None, help="UI label — its presence makes the field UI-visible.")
@click.option("--group", default="Custom", help="UI group heading (with --label).")
@click.option("--order", type=int, default=0, help="UI sort order in the group (with --label).")
@click.option(
    "--editor", default=None,
    help="text|textarea|number|boolean|datetime|select|list_chips.",
)
@click.option("--list-column", is_flag=True, help="Show as a list-view column (with --label).")
@click.option("--filterable", is_flag=True, help="Offer as a list-view filter (with --label).")
@click.option("--overwrite", is_flag=True, help="Replace an existing declaration of this name.")
def add_field(
    ext_dir: Path,
    entity: str,
    name: str,
    field_type: str,
    enum_values: str | None,
    required: bool,
    indexed: bool,
    max_length: int | None,
    label: str | None,
    group: str,
    order: int,
    editor: str | None,
    list_column: bool,
    filterable: bool,
    overwrite: bool,
) -> None:
    """Scaffold a valid `*.field.yaml` and wire the fields provides key if absent (SPEC-68).

    The field is validated by the kernel's own parser before anything is written, so the
    generated file is one `oxtend validate` accepts — the choices are core's, not this
    tool's.
    """
    from oxtend.manifest_vendored import VendoredKernelUnavailable
    from oxtend.scaffold import ScaffoldError
    from oxtend.scaffold import add_field as _add_field

    ui: dict | None = None
    if label is not None:
        ui = {"label": label, "group": group, "order": order}
        if editor is not None:
            ui["editor"] = editor
        if list_column:
            ui["list_column"] = True
        if filterable:
            ui["filterable"] = True
    elif editor or list_column or filterable:
        _warn("--editor/--list-column/--filterable need --label to render; ignoring them")

    values = tuple(v.strip() for v in enum_values.split(",")) if enum_values else ()

    try:
        result = _add_field(
            ext_dir, entity, name, field_type,
            enum_values=values, required=required, indexed=indexed,
            max_length=max_length, ui=ui, overwrite=overwrite,
        )
    except VendoredKernelUnavailable as exc:
        _fail(str(exc), EXIT_TOOLING)
        return
    except ScaffoldError as exc:
        _fail(str(exc))
        return

    _ok(f"wrote {result.field_path.relative_to(ext_dir)}")
    if result.wired:
        _ok("added `knowledge-hive/fields` to oxtend.yaml provides")
    elif result.manual_provides:
        _warn("no `provides:` block found — add this to oxtend.yaml:\n" + result.manual_provides)
    click.echo("Next: oxtend validate " + str(ext_dir))


# ---------------------------------------------------------------------------
# The local development loop
# ---------------------------------------------------------------------------

_CORE_OPTION = click.option(
    "--core",
    "core_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path, resolve_path=True),
    required=True,
    help="Path to the orion-core checkout whose extensions/ is bind-mounted into the backend.",
)
_URL_OPTION = click.option(
    "--core-url",
    default=DEFAULT_CORE_URL,
    show_default=True,
    help="Running backend to install into.",
)
_KEY_OPTION = click.option(
    "--internal-key",
    default=None,
    help="X-Internal-Key for /kernel/*. Defaults to $ORION_INTERNAL_API_KEY, "
    f"then {DEFAULT_INTERNAL_KEY!r} (compose.local.yaml's default).",
)


@cli.command()
@click.argument("ext_dir", type=_DIR)
@_CORE_OPTION
@click.option("--skip-ui", is_flag=True, help="Skip compiling ui/ (for a metadata-only rebuild).")
@click.option("--install/--no-install", default=True, help="Also install into a running core.")
@_URL_OPTION
@_KEY_OPTION
def mount(
    ext_dir: Path,
    core_dir: Path,
    skip_ui: bool,
    install: bool,
    core_url: str,
    internal_key: str | None,
) -> None:
    """Build a bundle straight into <core>/extensions/<scope> and install it.

    Replaces the RUNBOOK's validate → lock → build → `cp -a` → edit .env → restart
    sequence. The mounted directory IS the build output, so there is no copy step
    to forget and no stale `build/` tree to ship by accident.

    --core-version is resolved from the core checkout rather than from the
    installed wheel, which is register D-108: the wheel says 0.7.0, every bundle
    requires >=1.0.0-dev.2, and the resulting failure names the bundle.
    """
    core_version = resolve_core_version(core_dir)
    ctx = click.get_current_context()
    ctx.invoke(validate, ext_dir=ext_dir, core_version=core_version)

    try:
        target = sync_bundle(ext_dir, core_dir, skip_ui=skip_ui, core_version=core_version)
    except BuildError as exc:
        _fail(str(exc))
        return
    scope, version = load_scope(ext_dir)
    _ok(f"mounted {scope}@{version} → {target}")

    if not install:
        return
    try:
        result = install_bundle(scope, core_url=core_url, internal_key=internal_key)
    except DevLoopError as exc:
        _fail(str(exc))
        return
    _ok(f"installed {result['scope']}@{result['version']} ({result['kind']}) — {result['status']}")


@cli.command()
@click.argument("ext_dir", type=_DIR)
@_CORE_OPTION
@_URL_OPTION
@_KEY_OPTION
@click.option("--interval", default=0.7, show_default=True, help="Seconds between polls.")
@click.option(
    "--once", is_flag=True, help="Sync and install a single time, then exit (for scripts and CI)."
)
def dev(
    ext_dir: Path,
    core_dir: Path,
    core_url: str,
    internal_key: str | None,
    interval: float,
    once: bool,
) -> None:
    """Watch a bundle and reinstall it into the running core on every change.

    No container restart. The kernel unmounts the scope's routes, evicts its
    modules from sys.modules, invalidates the OpenAPI and route-matcher caches and
    remounts — all under a per-scope advisory lock. That path already existed and
    was exposed over HTTP; this is the thing that calls it.

    A change confined to metadata, migrations, prompts or Python passes --skip-ui,
    so npm only runs when something under ui/ actually moved.

    Note what this does NOT reload: core's own source. That is uvicorn --reload's
    job, which compose.local.yaml turns on.
    """
    scope, version = load_scope(ext_dir)
    click.echo(f"watching {ext_dir} → {bundle_target(core_dir, scope)}")

    def cycle(skip_ui: bool) -> bool:
        started = time.monotonic()
        try:
            sync_bundle(
                ext_dir, core_dir, skip_ui=skip_ui, core_version=resolve_core_version(core_dir)
            )
            result = install_bundle(scope, core_url=core_url, internal_key=internal_key)
        except (BuildError, DevLoopError) as exc:
            # Never fatal in watch mode. A bundle mid-edit is invalid more often
            # than not, and a loop that exits on the first bad save is a loop
            # nobody leaves running.
            _warn(f"{exc}")
            return False
        _ok(
            f"{result['scope']}@{result['version']} reinstalled "
            f"in {time.monotonic() - started:.1f}s"
        )
        return True

    ok = cycle(skip_ui=False)
    if once:
        sys.exit(EXIT_OK if ok else EXIT_INVALID)

    state = snapshot(ext_dir)
    try:
        while True:
            time.sleep(interval)
            current = snapshot(ext_dir)
            changed = changed_paths(state, current)
            if not changed:
                continue
            state = current
            preview = ", ".join(sorted(changed)[:3])
            more = f" (+{len(changed) - 3} more)" if len(changed) > 3 else ""
            click.echo(f"  changed: {preview}{more}")
            cycle(skip_ui=not touches_ui(changed))
            # Re-snapshot: the build writes into <core>/extensions, but a UI
            # compile also writes ui/dist and ui/node_modules inside the source
            # tree, which would otherwise register as a change and loop.
            state = snapshot(ext_dir)
    except KeyboardInterrupt:
        click.echo("\nstopped watching")


@cli.command()
@click.argument("scope")
@click.option(
    "--database-url",
    default=None,
    help="Async DSN. Defaults to $ORION_DEV_DATABASE_URL, then compose.local.yaml's "
    "postgres on localhost:15432.",
)
@click.option("--keep-schema", is_flag=True, help="Delete ledger rows but leave the scope schema.")
@click.option(
    "--revoke-entitlement", is_flag=True, help="Also delete the scope's entitlement rows."
)
@click.option("--yes", is_flag=True, help="Do not prompt.")
@click.option("--force", is_flag=True, help="Allow a non-loopback database. Think first.")
def reset(
    scope: str,
    database_url: str | None,
    keep_schema: bool,
    revoke_entitlement: bool,
    yes: bool,
    force: bool,
) -> None:
    """Undo a scope's install so an edited migration can be applied again.

    Bundle migrations are append-only and checksum-ledgered, so re-running a file
    whose bytes changed raises MigrationChecksumMismatch. Correct in production;
    impossible to write a migration under. This restores the never-installed state
    for one scope: its ledger rows, its derived registry rows, and its schema.
    """
    import asyncio
    import os

    from oxtend.reset import ResetError, assert_local, assert_safe_scope, reset_scope

    dsn = (
        database_url
        or os.environ.get("ORION_DEV_DATABASE_URL")
        or "postgresql+asyncpg://orion:orion-local@localhost:15432/orion"
    )
    try:
        assert_safe_scope(scope)
        assert_local(dsn, force=force)
    except ResetError as exc:
        _fail(str(exc))
        return

    if not yes:
        what = "ledger rows" if keep_schema else f'ledger rows AND schema "{scope}"'
        click.confirm(f"Delete {what} for {scope}?", abort=True)

    try:
        result = asyncio.run(
            reset_scope(
                dsn,
                scope,
                drop_schema=not keep_schema,
                revoke_entitlement=revoke_entitlement,
            )
        )
    except ResetError as exc:
        _fail(str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim; usually connectivity
        _fail(f"reset failed: {exc}", EXIT_TOOLING)
        return

    for table, count in result.items():
        if table == "schema_dropped":
            continue
        click.echo(f"  {table}: {count} row(s)")
    if result.get("schema_dropped"):
        click.echo(f'  schema "{scope}": dropped')
    _ok(f"{scope} reset — reinstall it with `oxtend mount`")


@cli.command()
@click.option(
    "--core",
    "core_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path, resolve_path=True),
    default=None,
    help="Path to the orion-core checkout, for the checks that need one.",
)
@_URL_OPTION
@_KEY_OPTION
def doctor(core_dir: Path | None, core_url: str, internal_key: str | None) -> None:
    """Check the things that otherwise fail confusingly."""
    from oxtend.doctor import FAIL, OK, WARN, run_doctor

    checks = run_doctor(core_dir=core_dir, core_url=core_url, internal_key=internal_key)
    colours = {OK: "green", WARN: "yellow", FAIL: "red"}
    marks = {OK: "✓", WARN: "!", FAIL: "✗"}
    for check in checks:
        click.secho(f"{marks[check.status]} {check.name}: {check.detail}", fg=colours[check.status])
    failures = sum(1 for c in checks if c.status == FAIL)
    if failures:
        _fail(f"{failures} check(s) failed")
    _ok("all checks passed")


def _default_bundle_dir(ext_dir: Path) -> Path:
    """Where `build` put the bundle: `<ext_dir>/build/<scope>-<version>`."""
    from oxtend.manifest_vendored import kernel_manifest_module

    manifest = kernel_manifest_module().load_manifest(ext_dir)
    return ext_dir / "build" / f"{manifest.scope}-{manifest.version}"


def main() -> None:  # pragma: no cover - console_scripts entry point
    # Register D-110, fixed at the source rather than documented as a workaround.
    #
    # Every result line here starts with U+2713 or U+2717. On a Windows console
    # that is not UTF-8, writing one raises UnicodeEncodeError *after* the command
    # has already done its work — so a successful build exits 1 with a traceback
    # about a checkmark, and CI reports a failure with no failing step. The
    # RUNBOOK's answer was to have every developer remember
    # `export PYTHONIOENCODING=utf-8` before every invocation.
    #
    # errors="replace" rather than "strict": if a stream still cannot represent a
    # mark, the right outcome is a mangled glyph, never a failed command.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            # Not a reconfigurable TextIOWrapper — captured output under pytest,
            # or a pipe someone replaced. Nothing to do and nothing to report.
            pass
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
