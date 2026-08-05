"""Access to the vendored kernel manifest model (SPEC-67 §5.1, ORION-688 task 2).

The CLI does **not** re-implement the manifest rules. The reference CLI duplicates
the scope regex and the migration gate, which means the build tool and the runtime
can disagree about what is valid — CI passes, install fails, and the error surfaces
on a customer's cluster. Instead the CLI imports both from the pinned core wheel:

    orion.kernel.manifest   — the oxtend.yaml contract
    orion.kernel.migrations — the statement-level scope gate

`orion-backend` is a hard dependency in `pyproject.toml`, pinned to a compatible
range. `tests/test_validate_pin_matches.py` asserts the installed version is inside
the range this CLI declares, and its sibling test in the core repo asserts the
reverse — a pin that only one side checks is a pin that drifts.

Called by: oxtend/validate.py, oxtend/build.py, oxtend/contract_test.py.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

#: The core distribution this CLI validates against.
CORE_DISTRIBUTION = "orion-backend"


class VendoredKernelUnavailable(RuntimeError):
    """Raised when the pinned core wheel is not importable.

    Deliberately fatal rather than degrading to a local re-implementation: a
    validate that silently used different rules than the kernel is worse than no
    validate at all.
    """


def kernel_manifest_module() -> Any:
    try:
        from orion.kernel import manifest  # type: ignore import-not-found
    except ImportError as exc:  # pragma: no cover - exercised via the unit test
        raise VendoredKernelUnavailable(
            f"cannot import orion.kernel.manifest from the pinned {CORE_DISTRIBUTION} wheel: "
            f"{exc}. Install it (`pip install -e '.[dev]'`) — oxtend deliberately has no "
            "fallback copy of the manifest rules."
        ) from exc
    return manifest


def kernel_migrations_module() -> Any:
    try:
        from orion.kernel import migrations  # type: ignore import-not-found
    except ImportError as exc:  # pragma: no cover
        raise VendoredKernelUnavailable(
            f"cannot import orion.kernel.migrations from the pinned {CORE_DISTRIBUTION} wheel: "
            f"{exc}"
        ) from exc
    return migrations


def installed_core_version() -> str:
    """The version of the core wheel this CLI is validating against."""
    try:
        return importlib.metadata.version(CORE_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError as exc:  # pragma: no cover
        raise VendoredKernelUnavailable(
            f"{CORE_DISTRIBUTION} is not installed; oxtend cannot validate without it"
        ) from exc


def declared_core_pin() -> str:
    """The `orion-backend` specifier this CLI's own metadata declares.

    Read from installed metadata rather than parsing `pyproject.toml`, so the test
    checks what was actually shipped rather than what the source says.
    """
    requires = importlib.metadata.requires("oxtend") or []
    for req in requires:
        name, _, rest = req.partition(" ")
        if name.strip().lower() == CORE_DISTRIBUTION:
            return rest.strip() or ""
        if req.lower().startswith(CORE_DISTRIBUTION):
            return req[len(CORE_DISTRIBUTION) :].strip()
    return ""
