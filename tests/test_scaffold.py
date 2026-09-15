"""`oxtend add-field` — the scaffold command (SPEC-68).

These exercise the real kernel: `add_field` validates the generated declaration through
`orion.kernel.fields.spec_from_yaml`, the same function the kernel runs at install, so a
green test here means the file `oxtend validate` will accept. Like the other tests in this
repo they need core importable (the CORE_READ_TOKEN checkout in CI); they run wherever the
validate tests do.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from oxtend.scaffold import ScaffoldError, add_field

_MANIFEST_WITH_PROVIDES = textwrap.dedent(
    """
    schema_version: 1
    kind: extension
    scope: x_fixture
    name: Fixture
    version: 1.0.0
    core: {api: v1, compat: '>=0.1,<2.0'}
    provides:
      # a comment that must survive the wire
      - knowledge-hive/prompts: {dir: prompt_config}
    """
).lstrip()


def _bundle(tmp_path: Path, manifest_text: str) -> Path:
    root = tmp_path / "x_fixture"
    root.mkdir()
    (root / "oxtend.yaml").write_text(manifest_text, encoding="utf-8")
    return root


def test_writes_a_valid_field_file(tmp_path: Path) -> None:
    root = _bundle(tmp_path, _MANIFEST_WITH_PROVIDES)
    result = add_field(
        root, "collections", "x_fixture_region", "enum",
        enum_values=("EU", "APAC"), indexed=True,
        ui={"label": "Region", "group": "Governance", "editor": "select", "filterable": True},
    )
    assert result.field_path.name == "x_fixture_region.field.yaml"
    body = result.field_path.read_text()
    # Reference layout, so the kernel and a hand-written bundle read it the same way.
    assert "entity: knowledge-hive/collections" in body
    assert "type: enum" in body
    assert "label: Region" in body


def test_wiring_preserves_manifest_comments(tmp_path: Path) -> None:
    """The provides key is added by a text insert, not a YAML round-trip, so every comment
    explaining the other keys stays put — clobbering them is the failure mode this avoids."""
    root = _bundle(tmp_path, _MANIFEST_WITH_PROVIDES)
    result = add_field(root, "collections", "x_fixture_region", "string")
    assert result.wired is True
    manifest = (root / "oxtend.yaml").read_text()
    assert "knowledge-hive/fields" in manifest
    assert "a comment that must survive the wire" in manifest
    assert "knowledge-hive/prompts" in manifest


def test_an_already_declared_fields_key_is_left_alone(tmp_path: Path) -> None:
    manifest_text = _MANIFEST_WITH_PROVIDES + "  - knowledge-hive/fields: {dir: metadata/fields}\n"
    root = _bundle(tmp_path, manifest_text)
    result = add_field(root, "collections", "x_fixture_region", "string")
    assert result.wired is False
    # The key must appear exactly once — no duplicate provides entry.
    assert (root / "oxtend.yaml").read_text().count("knowledge-hive/fields") == 1


def test_no_provides_block_yields_a_manual_instruction_not_a_rewrite(tmp_path: Path) -> None:
    """A manifest with no `provides:` is not restructured blind — the author is handed the
    exact lines instead, so a comment-rich file is never rewritten from a parsed tree."""
    manifest_text = textwrap.dedent(
        """
        schema_version: 1
        kind: extension
        scope: x_fixture
        name: Fixture
        version: 1.0.0
        core: {api: v1, compat: '>=0.1,<2.0'}
        """
    ).lstrip()
    root = _bundle(tmp_path, manifest_text)
    result = add_field(root, "collections", "x_fixture_region", "string")
    assert result.wired is False
    assert result.manual_provides is not None
    assert "knowledge-hive/fields" in result.manual_provides


def test_a_field_the_kernel_would_reject_is_refused_before_writing(tmp_path: Path) -> None:
    """No re-implemented rule: the rejection is core's, and it fires before any file is
    written, so a bad option cannot leave a half-scaffolded bundle."""
    root = _bundle(tmp_path, _MANIFEST_WITH_PROVIDES)
    with pytest.raises(ScaffoldError, match="select but is not an enum"):
        add_field(
            root, "collections", "tier", "string",
            ui={"label": "Tier", "editor": "select"},
        )
    assert not (root / "metadata/fields/tier.field.yaml").exists()


def test_an_entity_core_does_not_extend_is_refused(tmp_path: Path) -> None:
    root = _bundle(tmp_path, _MANIFEST_WITH_PROVIDES)
    with pytest.raises(ScaffoldError, match="does not extend"):
        add_field(root, "invoices", "paid_on", "date")


def test_existing_file_is_not_clobbered_without_overwrite(tmp_path: Path) -> None:
    root = _bundle(tmp_path, _MANIFEST_WITH_PROVIDES)
    add_field(root, "collections", "x_fixture_region", "string")
    with pytest.raises(ScaffoldError, match="already exists"):
        add_field(root, "collections", "x_fixture_region", "string")
    # With the flag it succeeds.
    result = add_field(root, "collections", "x_fixture_region", "int", overwrite=True)
    assert "type: int" in result.field_path.read_text()
