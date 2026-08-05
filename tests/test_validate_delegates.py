"""The CLI must not carry its own copy of any kernel rule (ORION-688 task 2).

These are source-scanning tests, and that is deliberate. The failure they prevent is
not a wrong answer today — it is someone "fixing" a validation bug by editing a regex
into this repo, at which point `oxtend validate` and install can disagree and CI stops
meaning anything.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from oxtend import build, contract_test, validate
from oxtend.manifest_vendored import kernel_manifest_module, kernel_migrations_module

_SRC = Path(__file__).parents[1] / "src" / "oxtend"

# The two patterns the reference CLI duplicated: the scope regex
# (oxtend.py:95) and the table-DDL migration gate (oxtend.py:31).
_SCOPE_REGEX_SHAPES = [
    re.compile(r"\^x_\[a-z0-9_\]"),
    re.compile(r"CREATE\\s\+TABLE"),
    re.compile(r"ALTER\\s\+TABLE"),
]


def _cli_sources() -> list[tuple[str, str]]:
    return [(p.name, p.read_text()) for p in sorted(_SRC.glob("*.py"))]


@pytest.mark.parametrize("pattern", _SCOPE_REGEX_SHAPES, ids=lambda p: p.pattern[:24])
def test_no_duplicated_kernel_rule_in_cli_source(pattern) -> None:
    offenders = [name for name, src in _cli_sources() if pattern.search(src)]
    assert offenders == [], (
        f"{offenders} appear to re-implement a kernel rule matching {pattern.pattern!r}. "
        "Delegate to orion.kernel instead — a build tool and a runtime that disagree about "
        "validity is the failure this repo is designed to prevent."
    )


def test_validate_calls_the_kernel_migration_gate() -> None:
    src = inspect.getsource(validate)
    assert "assert_migration_is_scoped" in src
    assert "kernel_migrations_module" in src


def test_validate_loads_the_manifest_through_the_kernel() -> None:
    src = inspect.getsource(validate)
    assert "load_manifest" in src
    assert "kernel_manifest_module" in src


def test_build_uses_the_kernel_digest_function() -> None:
    """ORION-665 reads the digest instead of recomputing it, which only works if both
    sides hash identically. Importing core's function is how that is guaranteed."""
    src = inspect.getsource(build)
    assert "from orion.kernel.digest import compute_bundle_digest" in src
    assert "hashlib" not in src, "build must not hash bundles itself"


def test_contract_test_reads_core_topic_and_field_type_sets_from_core() -> None:
    src = inspect.getsource(contract_test)
    assert "from orion.kernel.events import CORE_TOPICS" in src
    assert "from orion.kernel.fields import FIELD_TYPES" in src


def test_vendored_modules_are_importable() -> None:
    """If this fails, the pinned core wheel is not installed — and oxtend has no
    fallback by design."""
    assert kernel_manifest_module().MANIFEST_SCHEMA_VERSION >= 1
    assert callable(kernel_migrations_module().assert_migration_is_scoped)
