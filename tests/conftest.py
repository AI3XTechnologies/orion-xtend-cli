"""Fixtures for the oxtend test suite.

Every test builds the exact extension source tree it asserts about, in `tmp_path` —
no committed fixture directories, so a test cannot pass because a stale file on disk
happens to satisfy it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "extension",
        "scope": "x_fixture",
        "name": "Fixture Extension",
        "version": "1.0.0",
        "core": {"api": "v1", "compat": ">=0.1,<2.0"},
        "capabilities": {
            # Must be a topic core actually emits, or every contract test built on this
            # fixture fails the topics check. `core.document.created` sat here until
            # ORION-704's follow-up retired it from CORE_TOPICS — it had no producer and
            # no handler, the dead-mechanism shape this migration exists to remove. The
            # real bundles were all updated then; this fixture was missed, and CI could
            # not catch it because the job that runs these tests has never checked out
            # core (no CORE_READ_TOKEN), so it has never run.
            "events": {"emit": ["x_fixture.thing.happened"], "subscribe": ["core.collection.swapped"]}
        },
    }


@pytest.fixture
def make_source(tmp_path: Path):
    """Write an extension *source* directory (pre-build) and return its path."""

    def _make(
        manifest: dict[str, Any],
        *,
        migrations: dict[str, str] | None = None,
        fields: dict[str, dict[str, Any]] | None = None,
        access: dict[str, dict[str, Any]] | None = None,
        python: dict[str, str] | None = None,
        dags: dict[str, str] | None = None,
        payload_schema: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> Path:
        root = tmp_path / (name or manifest["scope"])
        root.mkdir(parents=True, exist_ok=True)
        (root / "oxtend.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

        def write(rel: str, content: str) -> None:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        for filename, sql in (migrations or {}).items():
            write(f"migrations/{filename}", sql)
        for filename, body in (fields or {}).items():
            write(f"metadata/fields/{filename}", yaml.safe_dump(body))
        for filename, body in (access or {}).items():
            write(f"metadata/access/{filename}", yaml.safe_dump(body))
        for rel, code in (python or {}).items():
            write(f"backend/python/{rel}", code)
        for rel, code in (dags or {}).items():
            write(f"dags/{rel}", code)
        if payload_schema is not None:
            write("metadata/payload/schema.yaml", yaml.safe_dump(payload_schema))
        return root

    return _make


@pytest.fixture
def scoped_sql():
    def _sql(scope: str, body: str) -> str:
        return f"SET LOCAL search_path = {scope};\n{body}"

    return _sql


@pytest.fixture
def core_openapi() -> dict[str, Any]:
    """A minimal stand-in for core's OpenAPI schema."""
    return {
        "openapi": "3.1.0",
        "paths": {
            "/api/v1/collections": {"get": {}},
            "/api/v1/documents/{document_id}": {"get": {}},
            "/kernel/extensions": {"get": {}},
        },
    }
