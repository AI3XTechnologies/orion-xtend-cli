# SPEC-01 `oxtend` Bundle CLI — Implementation Plan

## Metadata

| Field | Value |
|-------|-------|
| Plan ID | OXT-IMPL-001 |
| Version | 1.0 |
| Date | 2026-08-05 |
| Author | AI3X Technologies |
| Spec References | `docs/specs/01-oxtend-cli.md`; `orion-knowledge-hive/docs/impl-plans/spec-67/plan.md` ORION-688 |
| Status | IMPL COMPLETE — registry-dependent acceptance pending |
| Total Units | 8 |
| Completed | 7 |
| Blocked | 1 |

> Unit IDs are `OXT-*`, not `ORION-*`: this repo releases independently and its unit
> numbers must never collide with core's sequence.

---

## Summary Table

| Unit ID | Title | Phase | Status | Tests |
|---|---|---|---|---|
| OXT-001 | `manifest_vendored.py` — pinned-core access, no local rule copies | 1 - Foundation | COMPLETE | 6/6 |
| OXT-002 | `validate.py` — delegate every rule to the kernel; report all problems at once | 1 - Foundation | COMPLETE | 12/12 |
| OXT-003 | `build.py` — remote compile, verbatim copy, `bundle.json` with the kernel's digest | 2 - Build | COMPLETE | 9/9 |
| OXT-004 | `lock.py` — `oxtend.lock` + staleness detection | 2 - Build | COMPLETE | 6/6 |
| OXT-005 | `contract_test.py` — symbols, endpoints, topics, field types | 3 - Contract | COMPLETE | 16/16 |
| OXT-006 | `sign.py` — cosign sign/verify, identity pinning, no fabricated signatures | 4 - Release | COMPLETE | 4/4 (+1 cosign-gated) |
| OXT-007 | `package.py` — OCI image, kind-aware namespace, unsigned-push refusal | 4 - Release | COMPLETE | 5/5 |
| OXT-008 | End-to-end acceptance against a live registry | 5 - Acceptance | BLOCKED | 0/1 |

**Totals:** 8 units · 55 passing, 5 environment-gated skips.

---

## Parallelization Guide

| Wave | Units | Notes |
|---|---|---|
| 1 | OXT-001 | Everything else imports it. |
| 2 | OXT-002, OXT-004 | Both need only the vendored modules. |
| 3 | OXT-003 | Needs 001 (digest) and 002 (validate runs first). |
| 4 | OXT-005, OXT-006, OXT-007 | Independent of each other; 007 needs 006's `is_signed`. |
| 5 | OXT-008 | Needs all of the above plus a registry. |

**Critical path:** 001 → 002 → 003 → 007 → 008.

---

## Pre-Requisites

- [x] Repo created with remote `AI3XTechnologies/orion-xtend-cli`
- [x] `orion.kernel` exists in core (SPEC-67 Phase 0, ORION-664/666)
- [ ] `orion-backend` published to an index — see SPEC-01 OQ-01; CI currently checks out
      the sibling repo at `vars.CORE_REF`
- [ ] cosign keypair or Fulcio/OIDC available to CI (`id-token: write` is set)
- [ ] `ghcr.io/ai3xtechnologies/orion-extensions/*` and `orion-clients/*` namespaces
      provisioned

---

## Units of Work

### OXT-001: `manifest_vendored.py`

| Field | Value |
|---|---|
| Status | COMPLETE |
| Phase | 1 - Foundation |
| Dependencies | none |
| Spec Reference | `docs/specs/01-oxtend-cli.md` §7.1 |

**Objective:** One place where the pinned core wheel is reached, so "does the CLI own a
rule?" is answerable by reading one file.

**Tasks:** accessors for `orion.kernel.manifest` / `.migrations`; `installed_core_version`;
`declared_core_pin` read from dist metadata; a fatal `VendoredKernelUnavailable` with no
fallback path.

**Files:** `src/oxtend/manifest_vendored.py`, `tests/test_validate_pin_matches.py`.

**Completion Summary:** Done. `VendoredKernelUnavailable` is deliberately fatal — a
`validate` that silently used a local copy of the rules is worse than no `validate`.
`declared_core_pin` reads installed metadata rather than parsing `pyproject.toml`, so the
test checks what shipped.

**Notes:** The pin tests skip when neither distribution is installed (a `PYTHONPATH` dev
run), and CI fails if they skip — a skipped pin check looks green.

---

### OXT-002: `validate.py`

| Field | Value |
|---|---|
| Status | COMPLETE |
| Phase | 1 - Foundation |
| Dependencies | OXT-001 |
| Spec Reference | §7.1, §8 |

**Objective:** Catch at build time what would otherwise fail at install time, using the
kernel's own rules.

**Tasks:** manifest load; optional compat check; declared-path existence; metadata YAML
parsed through `spec_from_yaml`/`rule_from_yaml`; every migration through
`assert_migration_is_scoped`; deny-by-default events warning; accumulate rather than
raise.

**Files:** `src/oxtend/validate.py`, `tests/test_validate.py`,
`tests/test_validate_delegates.py`.

**Completion Summary:** Done. Declared-but-absent `provides` paths are errors, not
warnings — the failure they prevent is silent (a wrong `metadata/fields` path installs
cleanly and registers nothing).

---

### OXT-003: `build.py`

| Field | Value |
|---|---|
| Status | COMPLETE |
| Phase | 2 - Build |
| Dependencies | OXT-001, OXT-002 |
| Spec Reference | §4.1 |

**Objective:** Produce the directory the kernel installs, with the digest computed once.

**Tasks:** `npm ci` when a lockfile exists; copy the verbatim directory set; ship a
pre-built `remotes/` when there is no `ui/`; write `bundle.json` using
`orion.kernel.digest.compute_bundle_digest`; wipe the output directory first.

**Files:** `src/oxtend/build.py`, `tests/test_build_bundle_json.py`.

**Completion Summary:** Done. The output directory is deleted before every build because a
stale file would be hashed into the digest and shipped. `test_kernel_verifies_the_digest_we_wrote`
asserts the cross-repo contract directly.

**Notes:** `npm ci` over `npm install` when a lockfile exists — a build that resolves
fresh versions cannot be reproducible, which is what OXT-004 exists for.

---

### OXT-004: `lock.py`

| Field | Value |
|---|---|
| Status | COMPLETE |
| Phase | 2 - Build |
| Dependencies | OXT-001 |
| Spec Reference | §4.2 |

**Objective:** Make a rebuild of a tag produce the same artifact, and record what the
bundle was built against.

**Tasks:** write `oxtend.lock`; hash `oxtend.yaml`'s raw bytes; resolve direct npm deps
from `package-lock.json`; `lock_is_current()` for `build --require-lock`.

**Files:** `src/oxtend/lock.py`, `tests/test_sign_lock_push.py`.

**Completion Summary:** Done. Raw-bytes hashing is deliberate: a comment change is a
change to build inputs. A stale lock is treated as worse than a missing one, because it
is trusted.

---

### OXT-005: `contract_test.py`

| Field | Value |
|---|---|
| Status | COMPLETE |
| Phase | 3 - Contract |
| Dependencies | OXT-001 |
| Spec Reference | §7.2 |

**Objective:** Make `core.compat` mean something.

**Tasks:** AST-walk `backend/python/` and `dags/`; resolve every `orion.*` import;
match `/api/v1|/kernel` literals against core's OpenAPI with normalised path params;
check declared and *called* topics both ways; check field types against `FIELD_TYPES`;
report a compat violation but keep running the other checks.

**Files:** `src/oxtend/contract_test.py`, `tests/test_contract.py`.

**Completion Summary:** Done. Static analysis only — executing extension code inside the
build tool would be a sandbox problem, not a validation one. Omitting `--openapi` warns
rather than silently skipping, because a green run that checked less than it appears to
is the failure this command exists to prevent.

---

### OXT-006: `sign.py`

| Field | Value |
|---|---|
| Status | COMPLETE |
| Phase | 4 - Release |
| Dependencies | OXT-003 |
| Spec Reference | §7.3, §9 |

**Objective:** Sign the artifact the way the kernel verifies it.

**Tasks:** `cosign sign-blob` over `bundle.json`; keyless in CI, `--key` locally;
`verify_bundle` refusing to run without a pinned identity; missing cosign → exit 2.

**Files:** `src/oxtend/sign.py`, `tests/test_sign_lock_push.py`.

**Completion Summary:** Done. Only `bundle.json` is signed because it carries the content
digest — one signature covers the bundle. The round-trip test runs only where a real
cosign exists; a mocked cosign proves nothing.

---

### OXT-007: `package.py`

| Field | Value |
|---|---|
| Status | COMPLETE |
| Phase | 4 - Release |
| Dependencies | OXT-006 |
| Spec Reference | §5.1 |

**Objective:** Publish, and refuse to publish an unsigned bundle.

**Tasks:** `busybox` Dockerfile with OCI labels; `orion-extensions/` vs `orion-clients/`
namespace by `kind`; `push` refusal before any container-CLI lookup.

**Files:** `src/oxtend/package.py`, `tests/test_sign_lock_push.py`.

**Completion Summary:** Done. The refusal is ordered before the CLI lookup so the gate
does not degrade into "fails for a different reason" on a machine without Docker. The
`busybox`-not-`scratch` rationale is kept inline, with the ORION-671 follow-up noted
rather than silently applied.

---

### OXT-008: End-to-end acceptance

| Field | Value |
|---|---|
| Status | BLOCKED |
| Phase | 5 - Acceptance |
| Dependencies | OXT-001..007 |
| Spec Reference | §12.5 |

**Objective:** ORION-688's acceptance criterion, run for real.

**Acceptance Criteria:**
```bash
oxtend all ./x_orion_eval --registry ghcr.io/ai3xtechnologies
cosign verify ghcr.io/ai3xtechnologies/orion-extensions/x_orion_eval:1.0.0
```

**Blocked on:** a reachable registry, provisioned namespaces, and a cosign identity. All
three are environment, not code.

---

## Progress Log

| Date | Units | Status | Tests | Notes |
|---|---|---|---|---|
| 2026-08-05 | OXT-001..007 | COMPLETE | 55 pass, 5 env-gated skips | Full CLI implemented against the SPEC-67 kernel. |
| 2026-08-05 | OXT-008 | BLOCKED | 0/1 | Needs a registry + cosign identity. |

## Blockers & Assumptions Log

- **Assumption:** `orion-backend` is installable from the sibling repo until it is
  published to an index (SPEC-01 OQ-01). CI checks out `vars.CORE_REF`, defaulting to
  `main`, and fails loudly when the ref is missing rather than falling back.
- **Assumption:** the CI runner provides the container socket for `oxtend package`; the
  `oxtend` image therefore ships cosign but no container CLI.
- **Blocker:** `requires_docker` tests and OXT-008 cannot run in an environment without a
  container runtime. The suite is written so their absence is a visible skip, never a
  silent pass.
- **Note:** the local verification run used Python 3.9 + `eval_type_backport` because no
  3.13 interpreter was available on the dev machine; `requires-python` is 3.13 and CI
  uses it.
