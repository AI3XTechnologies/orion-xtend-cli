"""The pinned core wheel must match what this CLI declares (ORION-688 task 2).

Half of a two-sided assertion. The other half lives in the core repo
(`backend/tests/kernel/test_oxtend_pin.py`): core asserts the CLI's declared range
contains core's own version. A pin only one side checks is a pin that drifts, and the
symptom of drift is a validate that passes against rules the runtime does not use.
"""

from __future__ import annotations

import importlib.metadata

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from oxtend.manifest_vendored import (
    CORE_DISTRIBUTION,
    declared_core_pin,
    installed_core_version,
)


def _metadata_available() -> bool:
    """Whether both distributions are *installed* (not merely importable).

    A `PYTHONPATH=src` developer run has the modules but no dist-info, so there is no
    pin to read. CI installs the package (`pip install -e '.[dev]'`), so these tests
    run there — which is where the pin actually matters. Skipping is correct; passing
    vacuously would not be.
    """
    for dist in ("oxtend", CORE_DISTRIBUTION):
        try:
            importlib.metadata.distribution(dist)
        except importlib.metadata.PackageNotFoundError:
            return False
    return True


pytestmark = pytest.mark.skipif(
    not _metadata_available(),
    reason=(
        "oxtend and/or orion-backend are not installed as distributions — run "
        "`pip install -e '.[dev]'` (CI does) so the declared pin can be read"
    ),
)


def test_core_wheel_is_installed() -> None:
    version = installed_core_version()
    assert Version(version) >= Version("0.7.0"), (
        f"{CORE_DISTRIBUTION} {version} is older than the manifest contract this CLI vendors"
    )


def test_declared_pin_is_a_range_not_a_wildcard() -> None:
    """An unpinned dependency on core means `oxtend validate` silently changes rules
    when core releases."""
    pin = declared_core_pin()
    assert pin, f"pyproject.toml must pin {CORE_DISTRIBUTION} explicitly"
    spec = SpecifierSet(pin)
    assert any(s.operator in (">=", "==", "~=") for s in spec), f"{pin} has no lower bound"
    assert any(s.operator in ("<", "<=", "==", "~=") for s in spec), (
        f"{pin} has no upper bound — a major core release would be accepted silently"
    )


def test_installed_core_satisfies_the_declared_pin() -> None:
    pin = declared_core_pin()
    installed = installed_core_version()
    assert Version(installed) in SpecifierSet(pin, prereleases=True), (
        f"installed {CORE_DISTRIBUTION} {installed} is outside the declared pin {pin} — "
        "the CLI is validating against rules it did not intend to"
    )


def test_manifest_schema_version_is_the_one_this_cli_understands() -> None:
    """If core bumps MANIFEST_SCHEMA_VERSION, the CLI needs a deliberate update rather
    than to keep emitting bundles at the old version."""
    from oxtend.manifest_vendored import kernel_manifest_module

    assert kernel_manifest_module().MANIFEST_SCHEMA_VERSION == 1
