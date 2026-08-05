"""The `oxtend` command line (SPEC-67 §5.1, ORION-688).

    oxtend validate      <ext_dir>
    oxtend build         <ext_dir> [--out DIR] [--require-lock]
    oxtend contract-test <ext_dir> --core-version X.Y.Z [--openapi FILE]
    oxtend lock          <ext_dir>
    oxtend sign          <ext_dir|--bundle DIR> [--key FILE]
    oxtend package       <ext_dir> --registry HOST
    oxtend push          <ext_dir> --registry HOST [--allow-unsigned]
    oxtend all           <ext_dir> --registry HOST

Exit codes are the interface CI actually consumes: 0 success, 1 validation/contract
failure, 2 tooling failure (cosign absent, no container CLI). A build tool that
returns 0 on a soft failure is a build tool that ships broken artifacts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from oxtend import __version__
from oxtend.build import BuildError, build_bundle
from oxtend.contract_test import run_contract_test
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


def _default_bundle_dir(ext_dir: Path) -> Path:
    """Where `build` put the bundle: `<ext_dir>/build/<scope>-<version>`."""
    from oxtend.manifest_vendored import kernel_manifest_module

    manifest = kernel_manifest_module().load_manifest(ext_dir)
    return ext_dir / "build" / f"{manifest.scope}-{manifest.version}"


def main() -> None:  # pragma: no cover - console_scripts entry point
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
