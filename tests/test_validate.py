"""`oxtend validate` behaviour (ORION-688 task 2).

The rules themselves are the kernel's and are tested there. What is tested here is
that the CLI *reports* them well: every problem in one run, declared-but-absent paths
caught before install rather than after, and the deny-by-default events warning.
"""

from __future__ import annotations

import pytest

from oxtend.validate import validate_bundle


def test_valid_bundle_passes(make_source, manifest, scoped_sql) -> None:
    source = make_source(
        manifest,
        migrations={"0001.sql": scoped_sql("x_fixture", "CREATE TABLE x_fixture.t (id INT);")},
    )
    result = validate_bundle(source)
    assert result.ok, result.errors
    assert (result.scope, result.version, result.kind) == ("x_fixture", "1.0.0", "extension")


def test_missing_manifest_is_reported(tmp_path) -> None:
    (tmp_path / "empty").mkdir()
    result = validate_bundle(tmp_path / "empty")
    assert not result.ok
    assert "oxtend.yaml" in result.errors[0]


def test_out_of_scope_migration_is_reported_with_the_filename(
    make_source, manifest, scoped_sql
) -> None:
    source = make_source(
        manifest,
        migrations={"0002_bad.sql": scoped_sql("x_fixture", "UPDATE public.documents SET title='x';")},
    )
    result = validate_bundle(source)
    assert not result.ok
    assert any("0002_bad.sql" in e for e in result.errors)


def test_all_errors_are_reported_in_one_run(make_source, manifest, scoped_sql) -> None:
    """An author should not have to run validate once per mistake."""
    source = make_source(
        {**manifest, "provides": [{"knowledge-hive/fields": {"dir": "metadata/does-not-exist"}}]},
        migrations={
            "0001_bad.sql": scoped_sql("x_fixture", "GRANT ALL ON public.documents TO orion;"),
            "0002_bad.sql": scoped_sql("x_fixture", "DROP TABLE public.documents;"),
        },
    )
    result = validate_bundle(source)
    assert len(result.errors) >= 3, result.errors


def test_declared_but_absent_paths_are_errors(make_source, manifest) -> None:
    """The silent failure this catches: a wrong `metadata/fields` path installs
    cleanly and registers nothing."""
    source = make_source(
        {**manifest, "provides": [{"knowledge-hive/fields": {"dir": "metadata/typo"}}]}
    )
    result = validate_bundle(source)
    assert not result.ok
    assert any("metadata/typo" in e for e in result.errors)


def test_missing_stage_definitions_file_is_an_error(make_source, manifest) -> None:
    source = make_source(
        {
            **manifest,
            "provides": [
                {
                    "knowledge-hive/airflow-dags": {
                        "dir": "dags/",
                        "dags": [
                            {"id": "x_fixture_ingest", "stage_definitions": "dags/stages/missing.yaml"}
                        ],
                    }
                }
            ],
        },
        dags={"x_fixture_ingest.py": "# dag\n"},
    )
    result = validate_bundle(source)
    assert not result.ok
    assert any("missing.yaml" in e for e in result.errors)


def test_backend_entrypoint_without_python_dir_is_an_error(make_source, manifest) -> None:
    source = make_source({**manifest, "backend_entrypoint": "x_fixture.api:router"})
    result = validate_bundle(source)
    assert not result.ok
    assert any("backend/python" in e for e in result.errors)


def test_no_events_declared_warns_about_deny_by_default(make_source, manifest) -> None:
    quiet = {**manifest}
    quiet.pop("capabilities", None)
    result = validate_bundle(make_source(quiet))
    assert result.ok
    assert any("deny by default" in w for w in result.warnings)


def test_malformed_field_yaml_is_reported(make_source, manifest) -> None:
    source = make_source(
        {**manifest, "provides": [{"knowledge-hive/fields": {"dir": "metadata/fields"}}]},
        fields={"broken.field.yaml": {"field": {"name": "no_entity"}}},
    )
    result = validate_bundle(source)
    assert not result.ok
    assert any("broken.field.yaml" in e for e in result.errors)


def test_core_version_mismatch_is_reported_when_asked(make_source, manifest) -> None:
    result = validate_bundle(make_source(manifest), core_version="9.9.9")
    assert not result.ok
    assert any("9.9.9" in e for e in result.errors)


def test_client_bundle_validates(make_source, manifest) -> None:
    client = {
        **manifest,
        "kind": "client",
        "scope": "x_dilmah",
        "name": "Dilmah",
        "capabilities": {"events": {"emit": [], "subscribe": []}},
    }
    result = validate_bundle(make_source(client))
    assert result.ok, result.errors
    assert result.kind == "client"
