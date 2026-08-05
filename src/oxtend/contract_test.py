"""`oxtend contract-test` — the check that makes a compat range mean something.

This is the reason ORION-688 is not a copy job. A bundle can validate perfectly and
still be unrunnable on the core it claims compatibility with: it imports a core symbol
that was renamed, calls an endpoint that moved, emits a topic core has never heard of,
or registers a field type core cannot store. All four are silent until install.

Four checks, each against the core at the version in `core.compat`:

1. **Symbols** — every `from orion...import X` the extension's Python does must
   resolve in the installed core.
2. **Endpoints** — every core API path the extension calls must exist in core's
   OpenAPI schema.
3. **Topics** — every topic it emits/subscribes must be declared in its own manifest
   *and* known to core (for `core.*` topics).
4. **Field types** — every field it registers must use a type core supports.

Static analysis, deliberately: running the extension's code would need its runtime
dependencies, a database, and a live core. The AST catches the mistakes that actually
happen — a rename, a moved route, a typo'd topic.

Called by: oxtend/cli.py (`contract-test`, `all`), and the reusable extension CI
workflow, matrixed across the bounds of each scope's compat range.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from oxtend.manifest_vendored import kernel_manifest_module

#: A core API path referenced as a string literal in extension code. Matches the
#: prefixes core actually serves; `/x/<scope>/...` is the extension's own space and
#: is deliberately excluded.
_CORE_PATH_RE = re.compile(r"^/(?:api/v1|kernel)/[A-Za-z0-9_\-/{}]*$")


@dataclass
class ContractResult:
    core_version: str
    checked_symbols: int = 0
    checked_endpoints: int = 0
    checked_topics: int = 0
    checked_field_types: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _python_files(ext_dir: Path) -> list[Path]:
    backend = ext_dir / "backend" / "python"
    dags = ext_dir / "dags"
    files: list[Path] = []
    for root in (backend, dags):
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def _core_imports(tree: ast.AST) -> list[tuple[str, str]]:
    """`[(module, symbol)]` for every `orion.*` import in the tree."""
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "orion":
                for alias in node.names:
                    found.append((node.module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "orion":
                    found.append((alias.name, ""))
    return found


def _check_symbols(ext_dir: Path, result: ContractResult) -> None:
    import importlib

    for path in _python_files(ext_dir):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as exc:
            result.errors.append(f"{path.name}: does not parse: {exc}")
            continue
        for module_name, symbol in _core_imports(tree):
            result.checked_symbols += 1
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                result.errors.append(
                    f"{path.name}: imports {module_name!r}, which does not exist in core "
                    f"{result.core_version} ({exc})"
                )
                continue
            if symbol and not hasattr(module, symbol):
                result.errors.append(
                    f"{path.name}: imports {symbol!r} from {module_name}, which core "
                    f"{result.core_version} does not export"
                )


def _string_literals(tree: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _check_endpoints(ext_dir: Path, openapi: dict[str, Any] | None, result: ContractResult) -> None:
    if openapi is None:
        result.warnings.append(
            "no core OpenAPI schema supplied (--openapi) — endpoint existence was not checked"
        )
        return
    known: set[str] = set(openapi.get("paths", {}))
    # Normalise `{param}` so /documents/{id} matches /documents/{document_id}: the
    # extension's literal will rarely use core's parameter name, and failing on that
    # would be noise rather than a finding.
    normalised = {re.sub(r"\{[^}]+\}", "{}", p) for p in known}

    for path in _python_files(ext_dir):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue  # already reported by _check_symbols
        for literal in _string_literals(tree):
            if not _CORE_PATH_RE.match(literal):
                continue
            result.checked_endpoints += 1
            candidate = re.sub(r"\{[^}]+\}", "{}", literal)
            if candidate not in normalised:
                result.errors.append(
                    f"{path.name}: references core endpoint {literal!r}, which is absent from "
                    f"core {result.core_version}'s OpenAPI schema"
                )


def _check_topics(manifest: Any, ext_dir: Path, result: ContractResult) -> None:
    from orion.kernel.events import CORE_TOPICS  # type: ignore

    declared_emit = set(manifest.capabilities.events.emit)
    declared_subscribe = set(manifest.capabilities.events.subscribe)

    # Every declared core.* subscription must be a topic core actually emits, or the
    # extension is waiting for an event that will never arrive.
    for topic in sorted(declared_subscribe):
        result.checked_topics += 1
        if topic.startswith("core.") and topic not in CORE_TOPICS:
            result.errors.append(
                f"manifest subscribes to {topic!r}, which core {result.core_version} does not "
                f"emit — known core topics: {sorted(CORE_TOPICS)}"
            )
    # Emitted topics must be scope-namespaced; emitting a core.* topic would let an
    # extension forge a platform event.
    for topic in sorted(declared_emit):
        result.checked_topics += 1
        if not topic.startswith(f"{manifest.scope}."):
            result.errors.append(
                f"manifest emits {topic!r}, which is outside this scope's namespace "
                f"({manifest.scope}.*)"
            )

    # And every topic the code actually uses must be declared. An emit() call on an
    # undeclared topic raises CapabilityError at runtime — better to find it in CI.
    for path in _python_files(ext_dir):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name not in ("emit", "guarded_subscribe", "subscribe"):
                continue
            args = [a for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if not args:
                continue
            topic = args[-1].value if name != "emit" else args[0].value
            result.checked_topics += 1
            pool = declared_emit if name == "emit" else declared_subscribe
            if topic not in pool:
                result.errors.append(
                    f"{path.name}: {name}({topic!r}) but the manifest declares "
                    f"{sorted(pool)} — deny by default means this raises at runtime"
                )


def _check_field_types(manifest: Any, ext_dir: Path, result: ContractResult) -> None:
    from orion.kernel.fields import FIELD_TYPES  # type: ignore

    candidates: list[tuple[Path, dict[str, Any]]] = []
    if fields := manifest.fields_provides:
        directory = ext_dir / fields.dir
        if directory.is_dir():
            for path in sorted(directory.glob("*.field.yaml")):
                candidates.append((path, yaml.safe_load(path.read_text()) or {}))
    if payload := manifest.payload_schema:
        path = ext_dir / payload.file
        if path.is_file():
            schema = yaml.safe_load(path.read_text()) or {}
            for entry in schema.get("fields", []):
                candidates.append((path, {"field": entry}))

    for path, raw in candidates:
        declared = (raw.get("field") or raw).get("type", "string")
        result.checked_field_types += 1
        if declared not in FIELD_TYPES:
            result.errors.append(
                f"{path.name}: field type {declared!r} is not supported by core "
                f"{result.core_version} (supported: {sorted(FIELD_TYPES)})"
            )


def run_contract_test(
    ext_dir: Path,
    *,
    core_version: str,
    openapi: dict[str, Any] | None = None,
) -> ContractResult:
    """Run all four checks. Returns a result; the CLI decides the exit code."""
    manifest_mod = kernel_manifest_module()
    ext_dir = Path(ext_dir)
    manifest = manifest_mod.load_manifest(ext_dir)
    result = ContractResult(core_version=core_version)

    try:
        manifest_mod.assert_core_compatible(manifest, core_version)
    except Exception as exc:  # CoreCompatError
        # Reported and then *continued*: knowing which symbols also broke is what
        # tells an author whether widening the range is safe or whether the code
        # genuinely needs to change.
        result.errors.append(str(exc))

    _check_symbols(ext_dir, result)
    _check_endpoints(ext_dir, openapi, result)
    _check_topics(manifest, ext_dir, result)
    _check_field_types(manifest, ext_dir, result)
    return result
