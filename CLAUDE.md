# CLAUDE.md — orion-xtend-cli

Guidance for Claude Code when working in this repository.

## Project Overview

`oxtend` is the build tool for **Orion Xtend bundles** — the signed OCI artifacts the
Orion Knowledge Hive extension kernel installs. One command chain:

```
validate → lock → build → contract-test → sign → package → push
```

This repo is **repo 5** of the six-repo topology defined in
[SPEC-67 §0](../orion-knowledge-hive/docs/specs/67-orion-xtend-migration.md). It is
consumed by `orion-extensions` (repo 2) and the client repos (3, 4) through the
reusable CI workflow, and its output is consumed by `orion.kernel.registry` in core
(repo 1).

## The one rule that matters

**Never re-implement a kernel rule here.** The manifest contract and the
scoped-migration gate are imported from the pinned `orion-backend` wheel via
`src/oxtend/manifest_vendored.py`:

| Rule | Owner | Accessed through |
|---|---|---|
| `oxtend.yaml` shape, scope regex, `provides` set | `orion.kernel.manifest` | `kernel_manifest_module()` |
| Statement-level migration gate | `orion.kernel.migrations` | `kernel_migrations_module()` |
| Bundle digest algorithm | `orion.kernel.digest` | direct import in `build.py` |
| Supported field types | `orion.kernel.fields.FIELD_TYPES` | direct import in `contract_test.py` |
| Core event topics | `orion.kernel.events.CORE_TOPICS` | direct import in `contract_test.py` |

The reference CLI (`Orion Xtend/tools/oxtend-cli/oxtend.py`) duplicated the scope regex
and the migration gate. That is the worst possible split for a build tool: CI passes,
install fails, and the error surfaces on a customer's cluster.
`tests/test_validate_delegates.py` scans this repo's source for duplicated rules and
fails if one appears — do not "fix" a validation bug by adding a regex here. Fix it in
core and bump the pin.

## Development Workflow

Mirrors the process in `orion-knowledge-hive` (see its `CLAUDE.md` and
`.claude/commands/feature.md`), scaled to a single-purpose tool:

1. **Spec first** — `docs/specs/NN-<name>.md`, using the 13-section format from
   `orion-knowledge-hive/docs/templates/spec-template.md`.
2. **Implementation plan** — `docs/impl-plans/spec-NN/plan.md` with `OXT-NNN` units.
   Unit IDs are `OXT-*` here so they never collide with core's `ORION-*` sequence.
3. **Branch** — `feature/spec-NN-<name>` off `main`.
4. **Conventional Commits**, enforced by `.pre-commit-config.yaml` (`commit-msg` hook)
   and `pr-title-lint` in CI. Scopes: `cli`, `validate`, `build`, `contract`, `sign`,
   `lock`, `package`, `ci`, `docs`.
5. **release-please** cuts `vX.Y.Z` tags and publishes the wheel + `oxtend:<version>`
   image.

## Commands

```bash
# Setup (the editable install is required — the pin tests read dist metadata)
pip install -e '.[dev]'

# Tests
pytest -q                                   # full suite
pytest -m "not requires_cosign" -q          # skip tests needing a real cosign binary
pytest -m "not requires_docker" -q          # skip tests needing a container CLI

# Lint
ruff check src tests

# Use it
oxtend validate      ../orion-extensions/x_orion_eval
oxtend contract-test ../orion-extensions/x_orion_eval --core-version 0.7.0 \
                     --openapi /tmp/core-openapi.json
oxtend all           ../orion-extensions/x_orion_eval --registry ghcr.io/ai3xtechnologies
```

## Testing Rules

Adapted from core's mandatory integration-testing rules — the lesson there (150K lines,
1,994 mock tests, zero live verification) applies here too:

1. **No mocked cosign.** Signature tests either run against a real `cosign` binary
   (`-m requires_cosign`) or assert the *absent-binary* error path. A mocked cosign
   proves nothing about verification.
2. **No mocked container CLI** for `package`/`push`. The `push`-refuses-unsigned test
   is written so it fails before any registry call, so it is meaningful on a machine
   with no Docker.
3. **Round-trip against the real kernel.** `test_build_bundle_json.py` asserts that
   what `build` writes, `orion.kernel.digest.verify_bundle_digest` accepts. That
   cross-repo assertion is the contract; keep it.
4. **Contract-test the contract-tester.** Every check in `contract_test.py` has a
   test for both the passing and the failing case. A check that only has a passing
   test is a check that might not be running.

## Exit Codes

CI consumes these; they are part of the interface.

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Validation or contract failure — the bundle is wrong |
| 2 | Tooling failure — cosign absent, no container CLI, core wheel missing |

A build tool that returns 0 on a soft failure ships broken artifacts.

## Logging & Comment Standards

Same standard as core (`orion-knowledge-hive/CLAUDE.md` → "Logging & Code Visibility"):
every module has a top-level docstring saying what it is and where it fits; comments
explain **why**, not what. In a CLI the user-facing output *is* the log, so error
messages must name the file, the rule, and the remedy — `✗ 0002_bad.sql: statement type
'GRANT' is denied…` rather than `✗ validation failed`.

## Related

- [SPEC-67](../orion-knowledge-hive/docs/specs/67-orion-xtend-migration.md) — the
  migration this tool exists to serve
- [ORION-688](../orion-knowledge-hive/docs/impl-plans/spec-67/plan.md) — this repo's
  originating unit
- `../orion-extensions/.github/workflows/_extension.yml` — the CI that calls this tool
