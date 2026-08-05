# /feature — spec → plan → implement, in this repo

The `orion-knowledge-hive` workflow (`.claude/commands/feature.md` there), reduced to
what a single-purpose tool repo needs. Same discipline, fewer moving parts: no VM, no
Airflow, no worktree fan-out for a repo this size.

## Phase 1 — `/feature <description>`

1. **Next spec number:** `ls docs/specs/ | sort | tail -1`
2. **Next unit number:** highest `OXT-NNN` in `docs/impl-plans/`. Unit IDs are `OXT-*`
   here — never `ORION-*`, which belongs to core's sequence.
3. **Plan mode.** Explore, then propose. Get approval before writing the spec.
4. **Write the spec** at `docs/specs/NN-<kebab-name>.md` using the 13-section format from
   `../orion-knowledge-hive/docs/templates/spec-template.md`. `docs/specs/01-oxtend-cli.md`
   is the worked example in this repo.
5. **Critical review pass** before any code: read the current
   `orion.kernel.*` source for the symbols the spec assumes, and list concrete gaps with
   file paths. The most common gap in this repo is a spec assuming a kernel API that the
   pinned core version does not have.
6. **⛔ Stop for approval.** Mark `status: REVIEWED` only after it.
7. **Write the plan** at `docs/impl-plans/spec-NN/plan.md`.
8. **Branch:** `git checkout -b feature/spec-NN-<name>`.

## Phase 2 — `/feature implement`

Work units in dependency order. After each unit:

```bash
pytest -q
ruff check src tests
git commit -m "OXT-NNN: <what changed>"
```

Then update the unit's Status, Completion Summary, the Summary Table row, and the
Progress Log **before** the commit — a plan that lags the code is a plan nobody trusts.

## Hard rules

| Rule | Why |
|---|---|
| Never add a validation rule to this repo | The kernel owns them; a duplicate means CI and install can disagree |
| Never mock cosign or the container CLI | A mocked signature proves nothing; mark the test and let it skip |
| Every new `contract-test` check needs a failing-case test | A check with only a passing test may not be running |
| Exit-code changes are breaking | `orion-extensions` CI branches on them |
| Widening the core pin to make a build pass is forbidden | If core rejects it, the bundle is wrong |

## Related

- [docs/specs/01-oxtend-cli.md](../../docs/specs/01-oxtend-cli.md)
- [docs/impl-plans/spec-01/plan.md](../../docs/impl-plans/spec-01/plan.md)
- [SPEC-67](../../../orion-knowledge-hive/docs/specs/67-orion-xtend-migration.md)
- `/release` — cut and verify a release
