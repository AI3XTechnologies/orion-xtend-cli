"""`oxtend validate` — manifest, metadata, and scoped-migration checks (ORION-688).

Every rule here is enforced by delegating to the vendored kernel modules. This file
contains **no** scope regex and **no** migration gate of its own; `tests/
test_validate_delegates.py` asserts that by scanning this source. That is the whole
design point of the unit: the CLI and the kernel cannot disagree about validity.

Called by: oxtend/cli.py (`validate`, and as the first step of `build`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from oxtend.manifest_vendored import kernel_manifest_module, kernel_migrations_module


@dataclass
class ValidationResult:
    """What `validate` found. `errors` non-empty ⇒ the bundle must not be built."""

    scope: str | None = None
    version: str | None = None
    kind: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _validate_metadata_yaml(ext_dir: Path, result: ValidationResult) -> None:
    """Every declarative YAML must parse, and must parse into the shape the kernel
    will later demand — a file that is valid YAML but missing `entity` would
    otherwise fail at install rather than at build."""
    manifest_mod = kernel_manifest_module()
    for subdir, parser in (
        ("metadata/fields", "fields"),
        ("metadata/access", "access"),
    ):
        directory = ext_dir / subdir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError as exc:
                result.errors.append(f"{path}: not valid YAML: {exc}")
                continue
            try:
                if parser == "fields":
                    from orion.kernel.fields import spec_from_yaml  # type: ignore

                    spec_from_yaml(result.scope or "", raw)
                else:
                    from orion.kernel.guard import rule_from_yaml  # type: ignore

                    rule = rule_from_yaml(result.scope or "", raw)
                    if not rule.entity:
                        result.errors.append(f"{path}: rule declares no `entity`")
            except Exception as exc:  # noqa: BLE001 — surfaced as a validation error
                result.errors.append(f"{path}: {exc}")
        del manifest_mod  # only imported to assert the vendored wheel is present


def _validate_migrations(ext_dir: Path, scope: str, result: ValidationResult) -> None:
    """Run the **kernel's** gate over every migration file.

    Calling `assert_migration_is_scoped` rather than re-deriving the rule is what
    makes `oxtend validate` and install agree. Every rejection an author sees here is
    exactly the rejection the kernel would produce.
    """
    migrations = kernel_migrations_module()
    for mf in migrations.read_migration_files(ext_dir):
        try:
            migrations.assert_migration_is_scoped(scope, mf.sql)
        except Exception as exc:  # MigrationSafetyError
            result.errors.append(f"{mf.path.name}: {exc}")


def _validate_declared_paths(ext_dir: Path, manifest: Any, result: ValidationResult) -> None:
    """A `provides` entry that names a directory or file must find it in the bundle.

    Caught here rather than at install because the failure mode is silent: an
    extension whose `metadata/fields` path is wrong installs cleanly and registers
    nothing.
    """
    if (fields := manifest.fields_provides) and not (ext_dir / fields.dir).is_dir():
        result.errors.append(f"provides knowledge-hive/fields: {fields.dir!r} is not in the bundle")
    if (access := manifest.access_provides) and not (ext_dir / access.dir).is_dir():
        result.errors.append(f"provides knowledge-hive/access: {access.dir!r} is not in the bundle")
    if (payload := manifest.payload_schema) and not (ext_dir / payload.file).is_file():
        result.errors.append(
            f"provides knowledge-hive/payload-schema: {payload.file!r} is not in the bundle"
        )
    if dags := manifest.airflow_dags:
        if not (ext_dir / dags.dir).is_dir():
            result.errors.append(f"provides knowledge-hive/airflow-dags: {dags.dir!r} is not in the bundle")
        for decl in dags.dags:
            if decl.stage_definitions and not (ext_dir / decl.stage_definitions).is_file():
                result.errors.append(
                    f"DAG {decl.id}: stage_definitions {decl.stage_definitions!r} is not in the bundle"
                )
    if manifest.backend_entrypoint and not (ext_dir / "backend" / "python").is_dir():
        result.errors.append(
            "backend_entrypoint is declared but backend/python/ is not in the bundle"
        )
    for slot in manifest.ui_slots:
        if not (ext_dir / "ui").is_dir():
            result.warnings.append(
                f"ui-slot {slot.slot} is declared but there is no ui/ directory to compile — "
                "the remote must already exist under remotes/"
            )
            break


def validate_bundle(ext_dir: Path, *, core_version: str | None = None) -> ValidationResult:
    """Validate the extension source directory at `ext_dir`.

    Returns a `ValidationResult` rather than raising, so `oxtend validate` can report
    *every* problem in one run instead of making an author fix them one per
    invocation.
    """
    ext_dir = Path(ext_dir)
    result = ValidationResult()
    manifest_mod = kernel_manifest_module()

    try:
        manifest = manifest_mod.load_manifest(ext_dir)
    except Exception as exc:  # ManifestError
        result.errors.append(str(exc))
        return result

    result.scope = manifest.scope
    result.version = manifest.version
    result.kind = manifest.kind

    if core_version:
        try:
            manifest_mod.assert_core_compatible(manifest, core_version)
        except Exception as exc:  # CoreCompatError
            result.errors.append(str(exc))

    if not manifest.capabilities.events.emit and not manifest.capabilities.events.subscribe:
        result.warnings.append(
            "no capabilities.events declared — emit() and subscribe() will both be denied "
            "at runtime (deny by default)"
        )

    _validate_declared_paths(ext_dir, manifest, result)
    _validate_metadata_yaml(ext_dir, result)
    _validate_migrations(ext_dir, manifest.scope, result)
    return result
