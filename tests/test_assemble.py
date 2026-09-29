"""`oxtend assemble` — a tenant BOM to release images.

The container tooling is replaced by an in-memory registry that serves real built
bundles, so everything except docker itself runs for real: BOM parsing, digest
resolution, the kernel's own bundle verification, `release.json`, and the Dockerfiles.
The end-to-end run against a real registry is `test_assemble_docker.py`
(`requires_docker`).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from oxtend.assemble import (
    AssemblyError,
    Verifier,
    assemble,
    load_bom,
)
from oxtend.build import build_bundle

REGISTRY = "registry.test"


class FakeRegistry:
    """Stands in for `DockerImageTool`: refs resolve to digests of what they serve."""

    def __init__(self) -> None:
        self.bundles: dict[str, Path] = {}  # tagged ref -> built bundle dir
        self.core_tags: set[str] = set()
        self.built: dict[str, str] = {}  # tag -> Dockerfile text
        self.signed: list[str] = []
        self.platform_map: dict[str, list[str]] = {}

    @staticmethod
    def _digest_of(text: str) -> str:
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()

    def add_core(self, version: str) -> None:
        for image in ("orion-backend", "orion-pipeline"):
            self.core_tags.add(f"{REGISTRY}/orion-core/{image}:{version}")

    def add_bundle(self, built_dir: Path, namespace: str) -> str:
        meta = json.loads((built_dir / "bundle.json").read_text())
        tag = f"{REGISTRY}/{namespace}/{meta['scope']}:{meta['version']}"
        self.bundles[tag] = built_dir
        return tag

    def _tag_for(self, ref: str) -> str:
        if "@" not in ref:
            return ref
        repo, digest = ref.split("@", 1)
        for tag in [*self.bundles, *self.core_tags]:
            if tag.rsplit(":", 1)[0] == repo and self._digest_of(tag) == digest:
                return tag
        raise AssemblyError(f"unknown ref {ref}")

    # -- ImageTool -----------------------------------------------------------

    def digest(self, ref: str) -> str:
        if ref not in self.bundles and ref not in self.core_tags:
            raise AssemblyError(f"inspect {ref} failed: not found")
        return self._digest_of(ref)

    def platforms(self, ref: str) -> list[str]:
        return self.platform_map.get(self._tag_for(ref), ["linux/amd64", "linux/arm64"])

    def extract(self, ref: str, platform: str, path_in_image: str, dest: Path) -> None:
        assert path_in_image == "/bundle"
        shutil.copytree(self.bundles[self._tag_for(ref)], dest)

    def build(self, context: Path, dockerfile: Path, tag: str, platforms: list[str], *, push: bool) -> str:
        text = dockerfile.read_text()
        self.built[tag] = text
        release = (context / "release.json").read_text()
        return self._digest_of(text + release)

    def sign(self, ref: str, *, key: str | None, insecure_registry: bool) -> None:
        self.signed.append(ref)


@pytest.fixture
def registry(tmp_path, make_source, manifest):
    reg = FakeRegistry()
    reg.add_core("1.0.0")
    client = make_source({**manifest, "kind": "client", "scope": "x_acme", "version": "0.3.0", "capabilities": {}})
    ext = make_source({**manifest, "scope": "x_addon", "version": "1.2.0", "capabilities": {}})
    reg.add_bundle(build_bundle(client, tmp_path / "built" / "x_acme", skip_ui=True), "orion-clients")
    reg.add_bundle(build_bundle(ext, tmp_path / "built" / "x_addon", skip_ui=True), "orion-extensions")
    return reg


@pytest.fixture
def bom(tmp_path) -> Path:
    path = tmp_path / "dev.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tenant": "acme",
                "env": "dev",
                "core": {"backend": "1.0.0", "pipeline": "1.0.0", "chatUi": "1.0.0"},
                "client": {"bundle": {"scope": "x_acme", "version": "0.3.0"}},
                "extensions": [{"scope": "x_addon", "version": "1.2.0"}],
            }
        )
    )
    return path


UNSIGNED = Verifier(allow_unsigned=True)


def _run(bom: Path, registry: FakeRegistry, **kwargs: Any):
    return assemble(bom, registry=REGISTRY, verifier=kwargs.pop("verifier", UNSIGNED), tool=registry, **kwargs)


# ---------------------------------------------------------------------------
# The BOM
# ---------------------------------------------------------------------------


def test_load_bom_reads_the_release_pieces(bom) -> None:
    parsed = load_bom(bom)
    assert parsed.tenant == "acme" and parsed.core_version == "1.0.0"
    assert [(p.scope, p.kind) for p in parsed.bundles] == [("x_acme", "client"), ("x_addon", "extension")]


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda b: b.pop("tenant"), "tenant is required"),
        (lambda b: b["core"].pop("pipeline"), "core.pipeline is required"),
        (lambda b: b["core"].update(pipeline="1.0.1"), "same core version"),
        (lambda b: b["extensions"].append({"scope": "x_addon", "version": "9"}), "more than once"),
        (lambda b: b["extensions"].append({"scope": "x_other"}), r"extensions\[1\].version"),
    ],
)
def test_load_bom_refuses_what_is_not_one_release(bom, mutate, match) -> None:
    body = yaml.safe_load(bom.read_text())
    mutate(body)
    bom.write_text(yaml.safe_dump(body))
    with pytest.raises(AssemblyError, match=match):
        load_bom(bom)


# ---------------------------------------------------------------------------
# A good release
# ---------------------------------------------------------------------------


def test_assembles_both_images_from_digests(bom, registry) -> None:
    result = _run(bom, registry)
    assert set(result.images) == {"backend", "pipeline"}
    for target, ref in result.images.items():
        assert ref.startswith(f"{REGISTRY}/orion-releases/acme-{target}@sha256:")
    assert sorted(registry.signed) == sorted(result.images.values()), "both images are signed"

    dockerfiles = list(registry.built.values())
    assert len(dockerfiles) == 2
    backend = next(d for d in dockerfiles if "org.orion.release.target=\"backend\"" in d)
    assert f"FROM {REGISTRY}/orion-core/orion-backend@sha256:" in backend
    assert ":1.0.0" not in backend.split("FROM", 3)[-1].split("\n", 1)[0], "the base is pinned by digest"
    assert "COPY --from=bundle_x_acme /bundle /extensions/x_acme" in backend
    assert "COPY --from=bundle_x_addon /bundle /extensions/x_addon" in backend
    assert "COPY release.json /extensions/release.json" in backend
    assert "ENV BACKEND_VERSION=1.0.0" in backend


def test_release_json_is_the_kernels_model_and_one_id_for_both_images(bom, registry) -> None:
    from orion.kernel import release as release_mod

    result = _run(bom, registry)
    manifest = release_mod.ReleaseManifest.model_validate(result.release)
    assert manifest.tenant == "acme"
    assert manifest.scopes == ["x_acme", "x_addon"]
    assert manifest.id == release_mod.canonical_id(manifest.core, manifest.bundles)
    for bundle in manifest.bundles:
        built = json.loads((registry.bundles[bundle.image.split("@")[0] + ":" + bundle.version]
                            / "bundle.json").read_text())
        assert bundle.digest == built["digest"]
    ids = {line for d in registry.built.values() for line in d.splitlines() if "org.orion.release.id" in line}
    assert len(ids) == 1, "backend and pipeline carry the same release id"


def test_the_same_pieces_give_the_same_release_id(bom, registry) -> None:
    first = _run(bom, registry).release["id"]
    assert _run(bom, registry).release["id"] == first


def test_result_json_records_what_promotion_needs(bom, registry) -> None:
    out = _run(bom, registry).as_json()
    assert out["tenant"] == "acme" and out["core"] == "1.0.0" and out["pushed"] is True
    assert set(out["images"]) == {"backend", "pipeline"}
    assert [b["scope"] for b in out["bundles"]] == ["x_acme", "x_addon"]


def test_a_bundle_built_for_one_platform_still_goes_into_a_multi_platform_release(bom, registry) -> None:
    """Bundle content is data, so its stage is pinned to the platform it was built for."""
    tag = next(t for t in registry.bundles if "x_addon" in t)
    registry.platform_map[tag] = ["linux/amd64"]
    _run(bom, registry)
    backend = next(iter(registry.built.values()))
    assert "FROM --platform=linux/amd64 " in backend


# ---------------------------------------------------------------------------
# What must be refused before anything is built
# ---------------------------------------------------------------------------


def test_a_tampered_bundle_is_refused(bom, registry) -> None:
    tag = next(t for t in registry.bundles if "x_addon" in t)
    (registry.bundles[tag] / "oxtend.yaml").write_text(
        (registry.bundles[tag] / "oxtend.yaml").read_text() + "\n# edited after build\n"
    )
    with pytest.raises(AssemblyError, match="digest mismatch"):
        _run(bom, registry)
    assert registry.built == {}


def test_a_bundle_whose_kind_differs_from_its_pin_is_refused(bom, registry) -> None:
    """A client bundle pinned as an extension would be published under the wrong namespace
    and licence-gated as an add-on."""
    tag = next(t for t in registry.bundles if "x_acme" in t)
    registry.bundles[tag.replace("orion-clients", "orion-extensions")] = registry.bundles[tag]
    body = yaml.safe_load(bom.read_text())
    body["client"]["bundle"] = {"scope": "", "version": ""}
    body["extensions"].append({"scope": "x_acme", "version": "0.3.0"})
    bom.write_text(yaml.safe_dump(body))
    with pytest.raises(AssemblyError, match="BOM pins extension x_acme"):
        _run(bom, registry)


def test_a_bundle_incompatible_with_the_core_is_refused(bom, registry, tmp_path, make_source, manifest) -> None:
    old = make_source(
        {**manifest, "scope": "x_addon", "version": "1.2.0", "capabilities": {}, "core": {"api": "v1", "compat": "<0.9"}},
        name="old",
    )
    tag = next(t for t in registry.bundles if "x_addon" in t)
    registry.bundles[tag] = build_bundle(old, tmp_path / "built" / "old", skip_ui=True)
    with pytest.raises(AssemblyError, match="requires core"):
        _run(bom, registry)


def test_a_missing_piece_is_refused(bom, registry) -> None:
    body = yaml.safe_load(bom.read_text())
    body["extensions"][0]["version"] = "7.7.7"
    bom.write_text(yaml.safe_dump(body))
    with pytest.raises(AssemblyError, match="not found"):
        _run(bom, registry)


def test_signatures_are_required_unless_explicitly_skipped(bom, registry) -> None:
    with pytest.raises(AssemblyError, match="signatures must be verified"):
        _run(bom, registry, verifier=Verifier())


def test_an_unsigned_bundle_fails_verification(bom, registry, monkeypatch) -> None:
    import oxtend.sign as sign_mod

    monkeypatch.setattr(sign_mod, "verify_bundle", lambda *a, **k: False)
    with pytest.raises(AssemblyError, match="signature does not verify"):
        _run(bom, registry, verifier=Verifier(key="cosign.pub"))


def test_a_local_build_is_one_platform(bom, registry) -> None:
    with pytest.raises(AssemblyError, match="one platform"):
        _run(bom, registry, push=False)
    result = _run(bom, registry, push=False, platforms=["linux/amd64"])
    assert result.pushed is False and registry.signed == [], "local builds are not signed"
