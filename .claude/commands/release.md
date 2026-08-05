# /release — cut an oxtend release

Mirrors `orion-knowledge-hive`'s `/feature deploy` phase, scaled to a tool repo.

## Pre-flight

```bash
git branch --show-current          # must be main
git status --short                 # must be clean
pytest -q                          # must be green
ruff check src tests
```

## 1. Confirm the core pin is deliberate

A release publishes the rules `oxtend validate` applies. Check what moved:

```bash
git log --oneline -- pyproject.toml | head -5
python -c "from oxtend.manifest_vendored import declared_core_pin, installed_core_version; \
           print(declared_core_pin(), installed_core_version())"
```

If `MANIFEST_SCHEMA_VERSION` changed in core, or a previously valid manifest now fails,
the release must be `feat(vendored)!:` with a `BREAKING CHANGE:` footer — every extension
maintainer finds out from those release notes.

## 2. Let release-please open the PR

Pushing to `main` triggers `.github/workflows/release-please.yml`. Review the generated
changelog: every entry should be traceable to a Conventional Commit. An empty changelog
means a PR title did not parse.

## 3. Merge the release PR

That cuts the tag, builds the wheel, publishes
`ghcr.io/ai3xtechnologies/oxtend:<version>`, and cosign-signs the image. The tool that
refuses to push unsigned bundles must not ship unsigned itself.

## 4. Verify the published image

```bash
VERSION=<new version>
docker run --rm ghcr.io/ai3xtechnologies/oxtend:$VERSION --version
cosign verify ghcr.io/ai3xtechnologies/oxtend:$VERSION \
  --certificate-identity-regexp '.*orion-xtend-cli.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## 5. Bump the consumers

`orion-extensions` and the client repos pin the image tag in their reusable workflow.
Open a PR in each; do not let them float on `:latest` — a floating build tool means a
bundle's provenance cannot be reconstructed.

```bash
grep -rn "oxtend:" ../orion-extensions/.github/workflows/
grep -rn "oxtend:" ../orion-client-dilmah/.github/workflows/
```
