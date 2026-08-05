# Contributing to orion-xtend-cli

Commit conventions and merge policy mirror
[`orion-knowledge-hive/CONTRIBUTING.md`](../orion-knowledge-hive/CONTRIBUTING.md); this
file records only what differs for a single-purpose tool repo.

## 1. Conventional Commits

Enforced twice: a `commit-msg` pre-commit hook locally, and `pr-title-lint` at PR time.

```
<type>(<scope>): <subject>
```

Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `build`, `ci`, `perf`,
`style`, `revert`. A breaking change adds `!` and a `BREAKING CHANGE:` footer.

## 2. Scopes

| Scope | Covers |
|---|---|
| `cli` | `src/oxtend/cli.py` — commands, options, exit codes |
| `validate` | `src/oxtend/validate.py` |
| `build` | `src/oxtend/build.py` |
| `contract` | `src/oxtend/contract_test.py` |
| `sign` | `src/oxtend/sign.py` |
| `lock` | `src/oxtend/lock.py` |
| `package` | `src/oxtend/package.py` |
| `vendored` | `src/oxtend/manifest_vendored.py` and the core pin |
| `ci` | `.github/` |
| `docs` | `docs/`, `README.md`, `CLAUDE.md` |

## 3. The core pin is a breaking-change surface

Bumping `orion-backend` in `pyproject.toml` changes the rules `oxtend validate` applies.
Treat it as such:

- A **patch/minor** core bump that only widens what is accepted → `chore(vendored):`.
- A core bump that changes `MANIFEST_SCHEMA_VERSION`, or that makes a previously valid
  manifest invalid → `feat(vendored)!:` with a `BREAKING CHANGE:` footer naming what
  stops building. Every extension in `orion-extensions` will need a manifest change, and
  the release notes are where their maintainers find that out.

Never widen the pin to make a failing build pass. If core rejects something the CLI
used to accept, that is core's decision and the bundle needs fixing.

## 4. Exit codes are public API

`0` / `1` / `2` are consumed by `orion-extensions`'s reusable workflow. Changing what a
code means is a breaking change even though no function signature moved.

## 5. Merge policy

Squash-merge to `main`; the PR title becomes the commit. `release-please` opens the
release PR, which cuts the tag, publishes the wheel, and builds `oxtend:<version>`.

## 6. Tests

- `pytest -q` must be green before review.
- A new check in `contract_test.py` needs **both** a passing and a failing test. A check
  with only a passing test is a check that might not be running.
- Do not mock `cosign` or the container CLI. Mark tests `requires_cosign` /
  `requires_docker` and let them skip where the binary is absent.
