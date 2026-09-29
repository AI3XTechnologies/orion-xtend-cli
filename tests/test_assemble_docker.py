"""`oxtend assemble` against a real registry and real docker (`requires_docker`).

Stands up nothing itself: the registry is `ORION_TEST_REGISTRY` (e.g. `localhost:5000`,
a `registry:2` service in CI). The two "core" images are minimal stand-ins — assembly
only stacks layers onto them, so what the base contains does not matter — while the
bundles are real: built by `oxtend build` and packaged by `oxtend package`.

Proves, end to end: digests are resolved from the registry, the pushed release images
contain every bundle at /extensions/<scope>/ and a release.json the kernel accepts, the
images carry BACKEND_VERSION and the release labels, and the digests reported are the
ones the registry serves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from oxtend.assemble import Verifier, assemble
from oxtend.build import build_bundle
from oxtend.package import package_bundle

pytestmark = pytest.mark.requires_docker

REGISTRY = os.environ.get("ORION_TEST_REGISTRY", "")
DOCKER = shutil.which("docker")


def _docker(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run([DOCKER, *args], capture_output=True, text=True, input=input_text, timeout=900)
    assert result.returncode == 0, f"docker {' '.join(args)}: {result.stderr[-1500:]}"
    return result.stdout


@pytest.fixture(scope="module", autouse=True)
def _require_tooling():
    if not DOCKER or not REGISTRY:
        pytest.skip("needs docker and ORION_TEST_REGISTRY (a registry:2 the host can push to)")


@pytest.fixture
def published(tmp_path, make_source, manifest):
    """A stand-in core at 1.0.0 and a real client + extension bundle, all pushed."""
    for image in ("orion-backend", "orion-pipeline"):
        ctx = tmp_path / image
        ctx.mkdir()
        (ctx / "Dockerfile").write_text(f"FROM busybox:stable\nLABEL test.core.image={image}\n")
        tag = f"{REGISTRY}/orion-core/{image}:1.0.0"
        _docker("build", "-t", tag, str(ctx))
        _docker("push", tag)

    client = make_source({**manifest, "kind": "client", "scope": "x_acme", "version": "0.3.0", "capabilities": {}})
    ext = make_source({**manifest, "scope": "x_addon", "version": "1.2.0", "capabilities": {}})
    for src, out in ((client, "x_acme"), (ext, "x_addon")):
        built = build_bundle(src, tmp_path / "built" / out, skip_ui=True)
        tag = package_bundle(built, REGISTRY)
        _docker("push", tag)

    bom = tmp_path / "dev.yaml"
    bom.write_text(
        yaml.safe_dump(
            {
                "tenant": "acme",
                "core": {"backend": "1.0.0", "pipeline": "1.0.0"},
                "client": {"bundle": {"scope": "x_acme", "version": "0.3.0"}},
                "extensions": [{"scope": "x_addon", "version": "1.2.0"}],
            }
        )
    )
    return bom


def test_assembles_and_pushes_release_images_the_kernel_accepts(published) -> None:
    from orion.kernel import release as release_mod

    result = assemble(
        published,
        registry=REGISTRY,
        verifier=Verifier(allow_unsigned=True),
        sign=False,
        insecure_registry=True,
        platforms=[_docker("version", "--format", "{{.Server.Os}}/{{.Server.Arch}}").strip()],
    )

    for target, ref in result.images.items():
        repo, digest = ref.split("@")
        assert repo == f"{REGISTRY}/orion-releases/acme-{target}"
        # The digest reported is the one the registry serves.
        served = json.loads(_docker("buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"))
        assert served["digest"] == digest

        _docker("pull", ref)
        config = json.loads(_docker("image", "inspect", ref))[0]["Config"]
        assert "BACKEND_VERSION=1.0.0" in config["Env"]
        labels = config["Labels"]
        assert labels["org.orion.release.id"] == result.release["id"]
        assert labels["org.orion.release.target"] == target
        assert labels["org.orion.release.bundles"] == "x_acme@0.3.0,x_addon@1.2.0"
        assert labels["test.core.image"] == f"orion-{target}", "built on the core image, by digest"

        manifest = json.loads(_docker("run", "--rm", ref, "cat", "/extensions/release.json"))
        parsed = release_mod.ReleaseManifest.model_validate(manifest)
        assert parsed.id == result.release["id"]
        listing = _docker("run", "--rm", ref, "ls", "/extensions/x_acme", "/extensions/x_addon")
        assert "oxtend.yaml" in listing and "bundle.json" in listing
