"""Contract-test coverage (ORION-688 task 4).

Each test is one class of "validates fine, cannot actually run": a symbol that moved,
an endpoint that no longer exists, a topic nobody declared, a field type core cannot
store. These are the failures a compat range is supposed to prevent and, without this
command, does not.
"""

from __future__ import annotations

import pytest

from oxtend.contract_test import run_contract_test

CORE = "0.7.0"


def test_valid_extension_passes(make_source, manifest, core_openapi) -> None:
    source = make_source(
        manifest,
        python={
            "x_fixture/__init__.py": "",
            "x_fixture/api.py": (
                "from orion.kernel.manifest import Manifest\n"
                "CORE_PATH = '/api/v1/collections'\n"
            ),
        },
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert result.ok, result.errors
    assert result.checked_symbols >= 1
    assert result.checked_endpoints >= 1


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


def test_nonexistent_core_symbol_fails(make_source, manifest, core_openapi) -> None:
    source = make_source(
        manifest,
        python={
            "x_fixture/__init__.py": "",
            "x_fixture/api.py": "from orion.kernel.manifest import ThisWasRenamed\n",
        },
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert not result.ok
    assert any("ThisWasRenamed" in e for e in result.errors)


def test_nonexistent_core_module_fails(make_source, manifest, core_openapi) -> None:
    source = make_source(
        manifest,
        python={"x_fixture/__init__.py": "", "x_fixture/api.py": "from orion.kernel.gone import x\n"},
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert not result.ok
    assert any("orion.kernel.gone" in e for e in result.errors)


def test_non_orion_imports_are_ignored(make_source, manifest, core_openapi) -> None:
    """The contract is about *core*; an extension's own third-party deps are its
    business and are resolved by its own packaging."""
    source = make_source(
        manifest,
        python={"x_fixture/__init__.py": "", "x_fixture/api.py": "import some_third_party_lib\n"},
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert result.ok, result.errors


def test_unparseable_python_is_reported(make_source, manifest, core_openapi) -> None:
    source = make_source(
        manifest, python={"x_fixture/__init__.py": "", "x_fixture/api.py": "def broken(:\n"}
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert not result.ok
    assert any("does not parse" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_missing_core_endpoint_fails(make_source, manifest, core_openapi) -> None:
    source = make_source(
        manifest,
        python={
            "x_fixture/__init__.py": "",
            "x_fixture/api.py": "PATH = '/api/v1/removed-in-2-0'\n",
        },
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert not result.ok
    assert any("removed-in-2-0" in e for e in result.errors)


def test_path_parameter_names_need_not_match(make_source, manifest, core_openapi) -> None:
    """Core says {document_id}; the extension's literal says {id}. Failing on that
    would be noise, not a finding."""
    source = make_source(
        manifest,
        python={"x_fixture/__init__.py": "", "x_fixture/api.py": "PATH = '/api/v1/documents/{id}'\n"},
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert result.ok, result.errors


def test_own_scope_paths_are_not_checked(make_source, manifest, core_openapi) -> None:
    source = make_source(
        manifest,
        python={"x_fixture/__init__.py": "", "x_fixture/api.py": "PATH = '/x/x_fixture/threads'\n"},
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert result.ok, result.errors


def test_without_openapi_endpoints_are_not_checked_but_it_is_said_so(
    make_source, manifest
) -> None:
    """Silently skipping a check makes a green run mean less than it appears to."""
    source = make_source(
        manifest,
        python={"x_fixture/__init__.py": "", "x_fixture/api.py": "PATH = '/api/v1/anything'\n"},
    )
    result = run_contract_test(source, core_version=CORE, openapi=None)
    assert result.ok
    assert any("OpenAPI" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------


def test_subscribing_to_an_unknown_core_topic_fails(make_source, manifest, core_openapi) -> None:
    bad = {**manifest}
    bad["capabilities"] = {"events": {"emit": [], "subscribe": ["core.document.invented"]}}
    result = run_contract_test(make_source(bad), core_version=CORE, openapi=core_openapi)
    assert not result.ok
    assert any("core.document.invented" in e for e in result.errors)


def test_emitting_outside_own_namespace_fails(make_source, manifest, core_openapi) -> None:
    """Otherwise an extension could forge a platform event."""
    bad = {**manifest}
    bad["capabilities"] = {"events": {"emit": ["core.document.created"], "subscribe": []}}
    result = run_contract_test(make_source(bad), core_version=CORE, openapi=core_openapi)
    assert not result.ok
    assert any("outside this scope's namespace" in e for e in result.errors)


def test_emit_call_on_an_undeclared_topic_fails(make_source, manifest, core_openapi) -> None:
    source = make_source(
        manifest,
        python={
            "x_fixture/__init__.py": "",
            "x_fixture/api.py": "async def go(emit):\n    await emit('x_fixture.not.declared', {})\n",
        },
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert not result.ok
    assert any("x_fixture.not.declared" in e for e in result.errors)


def test_declared_emit_call_passes(make_source, manifest, core_openapi) -> None:
    source = make_source(
        manifest,
        python={
            "x_fixture/__init__.py": "",
            "x_fixture/api.py": "async def go(emit):\n    await emit('x_fixture.thing.happened', {})\n",
        },
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert result.ok, result.errors


# ---------------------------------------------------------------------------
# Field types
# ---------------------------------------------------------------------------


def test_unsupported_field_type_fails(make_source, manifest, core_openapi) -> None:
    source = make_source(
        {**manifest, "provides": [{"knowledge-hive/fields": {"dir": "metadata/fields"}}]},
        fields={"bad.field.yaml": {"entity": "documents", "field": {"name": "blobby", "type": "blob"}}},
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert not result.ok
    assert any("blob" in e for e in result.errors)


def test_supported_field_types_pass(make_source, manifest, core_openapi) -> None:
    source = make_source(
        {**manifest, "provides": [{"knowledge-hive/fields": {"dir": "metadata/fields"}}]},
        fields={
            "a.field.yaml": {"entity": "documents", "field": {"name": "sent_at", "type": "date"}},
            "b.field.yaml": {"entity": "documents", "field": {"name": "tags", "type": "string[]"}},
        },
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert result.ok, result.errors
    assert result.checked_field_types == 2


# ---------------------------------------------------------------------------
# Compat range
# ---------------------------------------------------------------------------


def test_out_of_range_core_is_reported_but_other_checks_still_run(
    make_source, manifest, core_openapi
) -> None:
    """Knowing what *else* broke tells the author whether widening the range is safe
    or whether the code genuinely has to change."""
    narrow = {**manifest, "core": {"api": "v1", "compat": ">=99.0"}}
    source = make_source(
        narrow,
        python={"x_fixture/__init__.py": "", "x_fixture/api.py": "from orion.kernel.manifest import Gone\n"},
    )
    result = run_contract_test(source, core_version=CORE, openapi=core_openapi)
    assert not result.ok
    assert any(">=99.0" in e for e in result.errors)
    assert any("Gone" in e for e in result.errors), "symbol check must still have run"
