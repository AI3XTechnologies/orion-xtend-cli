"""`oxtend package` / `oxtend push` — the OCI artifact (ORION-688 tasks 5, 7).

Packages a built bundle as `<registry>/orion-extensions/<scope>:<version>` (or
`orion-clients/<scope>` for a `kind: client` bundle) and pushes it.

The base image stays `busybox`, with the reference implementation's rationale intact:
the installer `docker run`s the image to copy `/bundle` out of it, and a `scratch`
image cannot run its own extraction command. ORION-671's init-container approach means
extraction may eventually become unnecessary — noted as a follow-up rather than
changed here, because the init-container path is not the only consumer yet.

`push` refuses an unsigned bundle unless `--allow-unsigned`. CI never passes it.

Called by: oxtend/cli.py (`package`, `push`, `all`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from oxtend.sign import is_signed

BUNDLE_JSON = "bundle.json"

#: Registry namespaces, per SPEC-67 §0 (`orion-extensions/*`, `orion-clients/*`).
NAMESPACE_BY_KIND = {"extension": "orion-extensions", "client": "orion-clients"}


class PackagingError(RuntimeError):
    pass


def _docker() -> str:
    for candidate in ("docker", "podman", "nerdctl"):
        if path := shutil.which(candidate):
            return path
    raise PackagingError("no container CLI found on PATH (tried docker, podman, nerdctl)")


def _bundle_meta(bundle_dir: Path) -> dict:
    path = Path(bundle_dir) / BUNDLE_JSON
    if not path.is_file():
        raise PackagingError(f"{path} not found — run `oxtend build` first")
    return json.loads(path.read_text())


def image_tag(bundle_dir: Path, registry: str) -> str:
    meta = _bundle_meta(bundle_dir)
    namespace = NAMESPACE_BY_KIND.get(meta.get("kind", "extension"), "orion-extensions")
    return f"{registry.rstrip('/')}/{namespace}/{meta['scope']}:{meta['version']}"


def package_bundle(bundle_dir: Path, registry: str) -> str:
    """Build the OCI image and return its tag."""
    bundle_dir = Path(bundle_dir)
    meta = _bundle_meta(bundle_dir)
    tag = image_tag(bundle_dir, registry)

    # busybox, not scratch: the installer runs this image to copy /bundle out of
    # itself (see scripts/install_extension.sh). Revisit once ORION-671's
    # init-container extraction is the only consumer — follow-up, not this unit.
    dockerfile = (
        "FROM busybox:stable\n"
        f'LABEL org.orion.scope="{meta["scope"]}" \\\n'
        f'      org.orion.version="{meta["version"]}" \\\n'
        f'      org.orion.kind="{meta.get("kind", "extension")}" \\\n'
        f'      org.orion.digest="{meta["digest"]}" \\\n'
        f'      org.opencontainers.image.created="{meta.get("built_at", "")}"\n'
        "COPY . /bundle\n"
    )
    dockerfile_path = bundle_dir / "Dockerfile"
    dockerfile_path.write_text(dockerfile)

    result = subprocess.run(
        [_docker(), "build", "-t", tag, "-f", str(dockerfile_path), "."],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PackagingError(f"image build failed:\n{(result.stderr or result.stdout)[-2000:]}")
    return tag


def push_bundle(bundle_dir: Path, registry: str, *, allow_unsigned: bool = False) -> str:
    """Push the packaged image. Refuses an unsigned bundle by default."""
    bundle_dir = Path(bundle_dir)
    if not is_signed(bundle_dir) and not allow_unsigned:
        raise PackagingError(
            f"{bundle_dir} has no {BUNDLE_JSON}.sig — refusing to push an unsigned bundle. "
            "Run `oxtend sign` first, or pass --allow-unsigned for a local-only push "
            "(CI never passes it)."
        )
    tag = image_tag(bundle_dir, registry)
    result = subprocess.run([_docker(), "push", tag], capture_output=True, text=True)
    if result.returncode != 0:
        raise PackagingError(f"push failed:\n{(result.stderr or result.stdout)[-2000:]}")
    return tag
