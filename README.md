# orion-xtend-cli

`oxtend` — the build tool for **Orion Xtend bundles**: the signed, self-contained OCI
artifacts that the Orion Knowledge Hive extension kernel installs.

Repo 5 of the six-repo topology in
[SPEC-67 §0](../orion-knowledge-hive/docs/specs/67-orion-xtend-migration.md).

## Install

```bash
pip install -e '.[dev]'          # editable install; required for the pin tests
oxtend --version
```

`orion-backend` is a hard dependency: `oxtend` imports the manifest contract and the
migration gate from it rather than reimplementing them. There is deliberately no
fallback — see [CLAUDE.md](CLAUDE.md#the-one-rule-that-matters).

## Pipeline

```bash
oxtend validate      ./x_orion_eval
oxtend lock          ./x_orion_eval
oxtend build         ./x_orion_eval --require-lock
oxtend contract-test ./x_orion_eval --core-version 0.7.0 --openapi core-openapi.json
oxtend sign          ./x_orion_eval                       # keyless in CI, --key locally
oxtend package       ./x_orion_eval --registry ghcr.io/ai3xtechnologies
oxtend push          ./x_orion_eval --registry ghcr.io/ai3xtechnologies

oxtend all           ./x_orion_eval --registry ghcr.io/ai3xtechnologies   # all of the above
```

| Command | What it does | Why it exists |
|---|---|---|
| `validate` | Manifest, metadata YAML, declared paths, and the **kernel's** migration gate | Catches at build time what would otherwise be an install-time failure on a customer cluster |
| `lock` | Writes `oxtend.lock`: resolved core version, manifest digest, UI dep tree | Rebuilding a tag six months later must produce the same artifact |
| `build` | Compiles `ui/` → `remotes/`, copies declarative assets, writes `bundle.json` | The digest is computed **once, here**; the kernel reads it rather than walking every file on every boot |
| `contract-test` | Symbols, endpoints, event topics, field types — against core at the compat version | A compat range that is never tested against its own bounds is a guess |
| `sign` | cosign-signs `bundle.json` (which carries the content digest) | One signature covers the bundle; tampering changes the digest |
| `package` | Builds `<registry>/orion-extensions/<scope>:<version>` (or `orion-clients/…`) | The delivery format the init-containers pull |
| `push` | Pushes it — **refusing unsigned artifacts** unless `--allow-unsigned` | A licensed artifact reaching a registry unsigned is not auditable |

## What `contract-test` actually checks

The reason this repo is not a copy of the reference CLI. A bundle can validate perfectly
and still be unrunnable on the core it claims to support:

1. **Symbols** — every `from orion… import X` resolves in the installed core.
2. **Endpoints** — every `/api/v1/…` or `/kernel/…` literal exists in core's OpenAPI
   schema (path parameter *names* are normalised; only the shape matters).
3. **Topics** — declared `core.*` subscriptions are topics core really emits; emitted
   topics stay inside the scope's namespace; every `emit()`/`guarded_subscribe()` call
   in the code is declared in the manifest.
4. **Field types** — every registered field uses a type core can store.

## Exit codes

`0` success · `1` the bundle is wrong · `2` tooling is missing (cosign, container CLI,
core wheel).

## Bundle layout produced by `build`

```
build/<scope>-<version>/
  oxtend.yaml           the manifest, verbatim
  oxtend.lock           provenance, if `oxtend lock` was run
  bundle.json           {scope, version, kind, digest, manifest, built_at, core_compat}
  bundle.json.sig       detached cosign signature (after `sign`)
  metadata/             fields, access rules, payload schema
  migrations/           scoped SQL
  dags/                 Airflow DAG modules + stage-definition YAML
  backend/python/       the extension's Python package
  remotes/              compiled UI remote
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Conventional Commits are enforced by a
`commit-msg` pre-commit hook and by `pr-title-lint` in CI.
