"""`oxtend add-field` — scaffold a valid `*.field.yaml` (SPEC-68, OXT-scaffold).

The one authoring shortcut in an otherwise validate/build/ship tool: it writes a field
declaration a scope can drop into a bundle, and — if the bundle does not yet declare the
`knowledge-hive/fields` provides key — wires that key in.

Two rules from this repo's CLAUDE.md shape it:

* **No re-implemented kernel rules.** The field it writes is validated by
  `orion.kernel.fields.spec_from_yaml` — the very function the kernel runs at install — so a
  bad type, an unknown editor, a `select` on a non-enum, or an unsafe name fails *here*,
  with core's own message, and no rule is duplicated in this repo. The generated file is
  therefore always one `oxtend validate` would accept.
* **Do not clobber a hand-written manifest.** `oxtend.yaml` carries comments explaining
  every provides key; a YAML round-trip would erase them. So the provides key is added by a
  targeted text insert after the `provides:` line, leaving every other byte untouched — and
  when that cannot be done safely, the command prints the exact line to add rather than
  guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from oxtend.manifest_vendored import kernel_manifest_module

#: Default location for field declarations, matching every shipped bundle.
_DEFAULT_FIELDS_DIR = "metadata/fields"

_PROVIDES_LINE = "  - knowledge-hive/fields: {{dir: {dir}}}"


class ScaffoldError(Exception):
    """A scaffold could not be produced — a bad field, or a manifest we will not rewrite."""


@dataclass
class AddFieldResult:
    field_path: Path
    wired: bool
    #: Set when the provides key was neither present nor safely inserted — the exact line
    #: the author must add themselves.
    manual_provides: str | None = None


def _field_body(
    entity: str,
    name: str,
    field_type: str,
    *,
    enum_values: tuple[str, ...],
    required: bool,
    indexed: bool,
    max_length: int | None,
    ui: dict[str, Any] | None,
) -> dict[str, Any]:
    """The `*.field.yaml` body, in the reference layout (`entity:` + nested `field:`)."""
    field: dict[str, Any] = {"name": name, "type": field_type}
    if enum_values:
        field["enum_values"] = list(enum_values)
    if required:
        field["required"] = True
    if indexed:
        field["indexed"] = True
    if max_length is not None:
        field["max_length"] = max_length
    # `knowledge-hive/<entity>` matches the shipped convention; the kernel strips the prefix.
    body: dict[str, Any] = {"entity": f"knowledge-hive/{entity}", "field": field}
    if ui is not None:
        body["ui"] = ui
    return body


def _wire_provides(manifest_path: Path, fields_dir: str) -> tuple[bool, str | None]:
    """Add the fields provides key by text insert, preserving comments.

    Returns `(wired, manual_instruction)`. `wired` is True when the file was edited; when it
    is False, `manual_instruction` is the exact line to add by hand — because a manifest with
    no `provides:` line at all is not one this command will restructure blind.
    """
    text = manifest_path.read_text(encoding="utf-8")
    line = _PROVIDES_LINE.format(dir=fields_dir)
    if "knowledge-hive/fields" in text:
        return False, None  # already declared — nothing to do

    lines = text.splitlines()
    for i, raw in enumerate(lines):
        if raw.rstrip() == "provides:" or raw.rstrip().startswith("provides:"):
            lines.insert(i + 1, line)
            manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True, None
    # No `provides:` block to extend. Rewriting the whole manifest would drop its comments,
    # so hand the author the two lines to add instead of guessing where they go.
    return False, f"provides:\n{line}"


def add_field(
    ext_dir: Path,
    entity: str,
    name: str,
    field_type: str,
    *,
    enum_values: tuple[str, ...] = (),
    required: bool = False,
    indexed: bool = False,
    max_length: int | None = None,
    ui: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> AddFieldResult:
    """Write `<ext_dir>/<fields_dir>/<name>.field.yaml` and wire the provides key if absent.

    Raises `ScaffoldError` on a field the kernel would reject, or on an existing file when
    `overwrite` is false — a scaffold that silently overwrites a hand-edited declaration is
    a scaffold that eats work.
    """
    manifest_mod = kernel_manifest_module()
    try:
        manifest = manifest_mod.load_manifest(ext_dir)
    except Exception as exc:  # ManifestError — no valid manifest to attach a field to
        raise ScaffoldError(f"cannot read {ext_dir}/oxtend.yaml: {exc}") from exc

    fields_dir = _DEFAULT_FIELDS_DIR
    if getattr(manifest, "fields_provides", None) is not None:
        fields_dir = manifest.fields_provides.dir

    body = _field_body(
        entity, name, field_type,
        enum_values=enum_values, required=required, indexed=indexed,
        max_length=max_length, ui=ui,
    )

    # Validate through the kernel's own parser before writing anything — the generated file
    # is exactly what `register_field` will accept, and a bad option fails now rather than at
    # `oxtend validate` or, worse, at install.
    try:
        from orion.kernel.fields import spec_from_yaml  # type: ignore  # noqa: PLC0415

        spec_from_yaml(manifest.scope, body)
    except Exception as exc:  # ManifestError from core
        raise ScaffoldError(str(exc)) from exc

    dest_dir = ext_dir / fields_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    field_path = dest_dir / f"{name}.field.yaml"
    if field_path.exists() and not overwrite:
        raise ScaffoldError(
            f"{field_path} already exists — pass --overwrite to replace it"
        )
    field_path.write_text(
        yaml.safe_dump(body, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

    wired, manual = _wire_provides(ext_dir / "oxtend.yaml", fields_dir)
    return AddFieldResult(field_path=field_path, wired=wired, manual_provides=manual)
