"""`oxtend assemble` — build a client's release images from its BOM.

A release is one tenant's combination of core, its client bundle and the extensions it
licenses, frozen and tested together. This command turns a tenant BOM (the
`tenants/<tenant>/<env>.yaml` file in orion-gitops) into two images:

    <registry>/<out-namespace>/<tenant>-backend@sha256:…
    <registry>/<out-namespace>/<tenant>-pipeline@sha256:…

each the core image **by digest**, with every bundle's `/bundle` copied to
`/extensions/<scope>/` and a `/extensions/release.json` stating exactly what the image is.
Nothing is compiled. The core image is not modified and stays byte-identical for every
client; the release image is a thin layer on top of it.

Before anything is built, every piece is resolved to a digest and every bundle is checked
the way the kernel checks it at install, so a bundle the kernel would refuse never reaches
an image:

* its content hashes to the digest in its `bundle.json` (the kernel's own function);
* its signature verifies (skipped only with `--allow-unsigned`, which CI never passes);
* its manifest is valid, its scope/version/kind are the ones the BOM names;
* the core version satisfies its `core.compat`.

`release.json` is written with the kernel's own `ReleaseManifest` and `canonical_id`, so
the writer and the reader cannot disagree about what a release is. Both images get the
identical file, so they share one release id.

Output (`--output`, default `assembly.json`) records the pushed digests; the gitops
workflow writes them into the BOM, and promotion copies them unchanged to qa and prod —
the image is built once.

Called by: oxtend/cli.py (`assemble`), orion-gitops `.github/workflows/assemble.yml`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

from oxtend import __version__
from oxtend.package import NAMESPACE_BY_KIND

CORE_IMAGES = {"backend": "orion-backend", "pipeline": "orion-pipeline"}
TARGETS = ("backend", "pipeline")
#: Ownership and modes for everything assembled under /extensions: root-owned, readable
#: (and traversable) by everyone, writable only by root. BuildKit applies them on COPY,
#: so no RUN step and no shell in the core image are needed.
BUNDLE_COPY_FLAGS = "--chown=0:0 --chmod=u=rwX,go=rX"


class AssemblyError(RuntimeError):
    """The BOM, a bundle, or the tooling makes this release impossible to build."""


# ---------------------------------------------------------------------------
# The BOM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundlePin:
    scope: str
    version: str
    kind: str  # "client" | "extension", from the BOM field the pin sits in


@dataclass(frozen=True)
class Bom:
    tenant: str
    core_version: str
    bundles: tuple[BundlePin, ...]


def load_bom(path: Path) -> Bom:
    """Read the parts of a tenant BOM that decide what a release contains.

    The backend and pipeline core versions must be equal: both images load the same
    bundles and must run the same core, and a release with two core versions is not one
    release. Raises `AssemblyError` naming the field for anything missing or malformed.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise AssemblyError(f"{path}: cannot read the BOM: {exc}") from exc

    def need(obj: Any, key: str, where: str) -> Any:
        if not isinstance(obj, dict) or obj.get(key) in (None, ""):
            raise AssemblyError(f"{path}: {where}{key} is required")
        return obj[key]

    tenant = str(need(raw, "tenant", ""))
    core = need(raw, "core", "")
    backend = str(need(core, "backend", "core."))
    pipeline = str(need(core, "pipeline", "core."))
    if backend != pipeline:
        raise AssemblyError(
            f"{path}: core.backend is {backend} but core.pipeline is {pipeline}. Both release "
            "images load the same bundles and must be built on the same core version."
        )

    pins: list[BundlePin] = []
    bundle = (raw.get("client") or {}).get("bundle") or {}
    if bundle.get("scope"):
        pins.append(
            BundlePin(str(bundle["scope"]), str(need(bundle, "version", "client.bundle.")), "client")
        )
    for i, ext in enumerate(raw.get("extensions") or []):
        pins.append(
            BundlePin(
                str(need(ext, "scope", f"extensions[{i}].")),
                str(need(ext, "version", f"extensions[{i}].")),
                "extension",
            )
        )
    scopes = [p.scope for p in pins]
    dupes = sorted({s for s in scopes if scopes.count(s) > 1})
    if dupes:
        raise AssemblyError(f"{path}: {dupes} pinned more than once")
    return Bom(tenant=tenant, core_version=backend, bundles=tuple(pins))


# ---------------------------------------------------------------------------
# Container tooling, behind a seam the unit tests replace
# ---------------------------------------------------------------------------


class ImageTool(Protocol):
    def digest(self, ref: str) -> str: ...

    def platforms(self, ref: str) -> list[str]: ...

    def extract(self, ref: str, platform: str, path_in_image: str, dest: Path) -> None: ...

    def build(
        self, context: Path, dockerfile: Path, tag: str, platforms: list[str], *, push: bool
    ) -> str: ...

    def sign(self, ref: str, *, key: str | None, insecure_registry: bool) -> None: ...


def _run(cmd: list[str], *, what: str, timeout: int = 1800, env: dict | None = None) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise AssemblyError(f"{what}: {cmd[0]} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AssemblyError(f"{what}: timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise AssemblyError(f"{what} failed:\n{(result.stderr or result.stdout).strip()[-2000:]}")
    return result.stdout


class DockerImageTool:
    """`docker` + `docker buildx` + `cosign`, the tools CI and the local harness both have."""

    def __init__(self, docker: str | None = None, cosign: str | None = None) -> None:
        self.docker = docker or shutil.which("docker") or "docker"
        self.cosign = cosign or shutil.which("cosign") or "cosign"

    def _inspect(self, ref: str, fmt: str) -> Any:
        out = _run(
            [self.docker, "buildx", "imagetools", "inspect", ref, "--format", fmt],
            what=f"inspect {ref}",
            timeout=300,
        )
        return json.loads(out)

    def digest(self, ref: str) -> str:
        manifest = self._inspect(ref, "{{json .Manifest}}")
        digest = manifest.get("digest") if isinstance(manifest, dict) else None
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise AssemblyError(f"inspect {ref}: no digest in the registry's answer")
        return digest

    def platforms(self, ref: str) -> list[str]:
        manifest = self._inspect(ref, "{{json .Manifest}}")
        found = []
        for entry in manifest.get("manifests") or []:
            p = entry.get("platform") or {}
            if p.get("os") in (None, "unknown") or p.get("architecture") in (None, "unknown"):
                continue  # attestation manifests, not images
            found.append(f"{p['os']}/{p['architecture']}" + (f"/{p['variant']}" if p.get("variant") else ""))
        if found:
            return found
        config = self._inspect(ref, "{{json .Image}}")
        if isinstance(config, dict) and config.get("os") and config.get("architecture"):
            return [f"{config['os']}/{config['architecture']}"]
        raise AssemblyError(f"inspect {ref}: cannot tell which platforms it was built for")

    def extract(self, ref: str, platform: str, path_in_image: str, dest: Path) -> None:
        _run([self.docker, "pull", "--quiet", "--platform", platform, ref], what=f"pull {ref}")
        cid = _run(
            [self.docker, "create", "--platform", platform, ref, "true"], what=f"create {ref}"
        ).strip()
        try:
            _run([self.docker, "cp", f"{cid}:{path_in_image}", str(dest)], what=f"copy {path_in_image} out of {ref}")
        finally:
            subprocess.run([self.docker, "rm", "-f", cid], capture_output=True, text=True)

    def build(
        self, context: Path, dockerfile: Path, tag: str, platforms: list[str], *, push: bool
    ) -> str:
        meta = context / f"{dockerfile.name}.metadata.json"
        cmd = [
            self.docker, "buildx", "build",
            "--file", str(dockerfile),
            "--tag", tag,
            "--platform", ",".join(platforms),
            "--metadata-file", str(meta),
            "--provenance=false",
            "--push" if push else "--load",
            str(context),
        ]
        _run(cmd, what=f"build {tag}")
        digest = json.loads(meta.read_text()).get("containerimage.digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise AssemblyError(f"build {tag}: buildx reported no image digest")
        return digest

    def sign(self, ref: str, *, key: str | None, insecure_registry: bool) -> None:
        cmd = [self.cosign, "sign", "--yes"]
        if key:
            cmd += ["--key", key]
        if insecure_registry:
            cmd += ["--allow-insecure-registry", "--allow-http-registry"]
        cmd.append(ref)
        _run(cmd, what=f"sign {ref}", timeout=600, env=dict(os.environ))


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class ResolvedBundle:
    pin: BundlePin
    ref: str  # <repo>@sha256:…
    platform: str
    content_dir: Path | None = None
    digest: str = ""  # from its bundle.json, verified


@dataclass
class Plan:
    bom: Bom
    core_refs: dict[str, str]  # target -> <repo>@sha256:…
    core_platforms: list[str]
    bundles: list[ResolvedBundle] = field(default_factory=list)


def _repo(ref: str) -> str:
    """`host/ns/name:tag` → `host/ns/name`. A registry port (`localhost:5001`) is kept."""
    last = ref.rsplit("/", 1)
    if ":" in last[-1]:
        last[-1] = last[-1].split(":", 1)[0]
    return "/".join(last)


def resolve(bom: Bom, *, registry: str, core_namespace: str, tool: ImageTool) -> Plan:
    """Resolve every piece to a digest. Nothing is pulled or built yet."""
    registry = registry.rstrip("/")
    core_refs: dict[str, str] = {}
    for target, image in CORE_IMAGES.items():
        tagged = f"{registry}/{core_namespace}/{image}:{bom.core_version}"
        core_refs[target] = f"{_repo(tagged)}@{tool.digest(tagged)}"
    backend_platforms = tool.platforms(core_refs["backend"])
    pipeline_platforms = set(tool.platforms(core_refs["pipeline"]))
    platforms = [p for p in backend_platforms if p in pipeline_platforms]
    if not platforms:
        raise AssemblyError(
            f"core backend ({backend_platforms}) and pipeline ({sorted(pipeline_platforms)}) "
            "images share no platform"
        )

    plan = Plan(bom=bom, core_refs=core_refs, core_platforms=platforms)
    for pin in bom.bundles:
        tagged = f"{registry}/{NAMESPACE_BY_KIND[pin.kind]}/{pin.scope}:{pin.version}"
        ref = f"{_repo(tagged)}@{tool.digest(tagged)}"
        # Bundle content is data and never executes, so any one platform's copy is the
        # bundle; the digest check below proves it.
        plan.bundles.append(ResolvedBundle(pin=pin, ref=ref, platform=tool.platforms(ref)[0]))
    return plan


# ---------------------------------------------------------------------------
# Verification — the kernel's own checks, before anything is built
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verifier:
    """How bundle signatures are checked. Exactly one mode, or an explicit opt-out."""

    key: str | None = None
    identity_regexp: str | None = None
    issuer: str | None = None
    allow_unsigned: bool = False

    def check(self) -> None:
        if self.allow_unsigned:
            return
        if self.key:
            return
        if self.identity_regexp and self.issuer:
            return
        raise AssemblyError(
            "bundle signatures must be verified: pass --verify-key, or --identity-regexp with "
            "--issuer. --allow-unsigned exists for a local harness only."
        )


def verify_bundle(bundle: ResolvedBundle, *, core_version: str, verifier: Verifier) -> None:
    """Refuse a bundle the kernel would refuse at install. Sets `bundle.digest`."""
    from orion.kernel.digest import verify_bundle_digest  # type: ignore
    from orion.kernel.manifest import assert_core_compatible, load_manifest  # type: ignore

    from oxtend.sign import verify_bundle as verify_signature

    pin, root = bundle.pin, bundle.content_dir
    assert root is not None
    try:
        manifest = load_manifest(root)
        bundle.digest = verify_bundle_digest(root)
        assert_core_compatible(manifest, running_version=core_version)
    except Exception as exc:  # the kernel's ManifestError / CoreCompatError
        raise AssemblyError(f"{pin.scope}@{pin.version} ({bundle.ref}): {exc}") from exc
    if (manifest.scope, manifest.version, manifest.kind) != (pin.scope, pin.version, pin.kind):
        raise AssemblyError(
            f"{bundle.ref} holds {manifest.kind} {manifest.scope}@{manifest.version}, but the "
            f"BOM pins {pin.kind} {pin.scope}@{pin.version}"
        )
    if verifier.allow_unsigned:
        return
    ok = verify_signature(
        root,
        key=verifier.key,
        identity_regexp=verifier.identity_regexp,
        issuer=verifier.issuer,
    )
    if not ok:
        raise AssemblyError(f"{pin.scope}@{pin.version} ({bundle.ref}): signature does not verify")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def release_manifest(plan: Plan, *, assembled_at: str | None = None) -> dict[str, Any]:
    """`release.json`, built and validated with the kernel's own model."""
    try:
        from orion.kernel import release as release_mod  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the installed core
        raise AssemblyError(
            "the installed orion-backend has no orion.kernel.release; `oxtend assemble` needs "
            "a core that knows assembled releases"
        ) from exc

    core = release_mod.ReleaseCore(
        version=plan.bom.core_version,
        backend_image=plan.core_refs["backend"],
        pipeline_image=plan.core_refs["pipeline"],
    )
    bundles = [
        release_mod.ReleaseBundle(
            scope=b.pin.scope, kind=b.pin.kind, version=b.pin.version, digest=b.digest, image=b.ref
        )
        for b in plan.bundles
    ]
    manifest = release_mod.ReleaseManifest(
        schema_version=release_mod.SCHEMA_VERSION,
        id=release_mod.canonical_id(core, bundles),
        tenant=plan.bom.tenant,
        core=core,
        bundles=bundles,
        assembled_at=assembled_at or datetime.now(timezone.utc).isoformat(),
        assembler=f"oxtend {__version__}",
    )
    return manifest.model_dump(mode="json")


def _stage(scope: str) -> str:
    return f"bundle_{scope}"


def dockerfile(target: str, plan: Plan, release: dict[str, Any]) -> str:
    """The Dockerfile for one release image. Deterministic for a given plan."""
    lines = ["# syntax=docker/dockerfile:1", "# Generated by oxtend assemble. Do not edit."]
    for b in plan.bundles:
        lines.append(f"FROM --platform={b.platform} {b.ref} AS {_stage(b.pin.scope)}")
    base = plan.core_refs[target]
    lines.append(f"FROM {base}")
    for b in plan.bundles:
        # Root-owned, readable by the app user and writable by nobody else: the release is
        # immutable. Set here rather than trusted from the bundle image, whose modes are
        # whatever the machine that built it had — 0600 from cosign on the signature, 0777
        # from a Windows-mounted checkout — and the kernel runs as uid 1000.
        lines.append(
            f"COPY --from={_stage(b.pin.scope)} {BUNDLE_COPY_FLAGS}"
            f" /bundle /extensions/{b.pin.scope}"
        )
    lines.append(f"COPY {BUNDLE_COPY_FLAGS} release.json /extensions/release.json")
    # The version is a fact of the image, so the chart does not have to repeat it.
    lines.append(f"ENV BACKEND_VERSION={plan.bom.core_version}")
    labels = {
        "org.opencontainers.image.base.name": _repo(base),
        "org.opencontainers.image.base.digest": base.rsplit("@", 1)[1],
        "org.orion.release.id": release["id"],
        "org.orion.release.tenant": plan.bom.tenant,
        "org.orion.release.core": plan.bom.core_version,
        "org.orion.release.target": target,
        "org.orion.release.bundles": ",".join(f"{b.pin.scope}@{b.pin.version}" for b in plan.bundles),
    }
    lines.append("LABEL " + " \\\n      ".join(f'{k}="{v}"' for k, v in labels.items()))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@dataclass
class AssemblyResult:
    release: dict[str, Any]
    images: dict[str, str]  # target -> <repo>@sha256:…
    pushed: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "release_id": self.release["id"],
            "tenant": self.release["tenant"],
            "core": self.release["core"]["version"],
            "bundles": [
                {"scope": b["scope"], "version": b["version"], "digest": b["digest"]}
                for b in self.release["bundles"]
            ],
            "images": self.images,
            "pushed": self.pushed,
        }


def assemble(
    bom_path: Path,
    *,
    registry: str,
    core_namespace: str = "orion-core",
    out_namespace: str = "orion-releases",
    verifier: Verifier,
    push: bool = True,
    sign_key: str | None = None,
    sign: bool = True,
    insecure_registry: bool = False,
    platforms: list[str] | None = None,
    work_dir: Path | None = None,
    tool: ImageTool | None = None,
) -> AssemblyResult:
    """Resolve, verify, render, build, push and sign one tenant's release."""
    verifier.check()
    tool = tool or DockerImageTool()
    bom = load_bom(bom_path)
    plan = resolve(bom, registry=registry, core_namespace=core_namespace, tool=tool)
    if platforms:
        missing = [p for p in platforms if p not in plan.core_platforms]
        if missing:
            raise AssemblyError(f"core images are not built for {missing} (have {plan.core_platforms})")
        plan.core_platforms = list(platforms)
    if not push and len(plan.core_platforms) > 1:
        raise AssemblyError(
            "a local (--no-push) build can load only one platform; pass --platform"
        )

    own_dir = work_dir is None
    root = Path(tempfile.mkdtemp(prefix="oxtend-assemble-")) if own_dir else Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    try:
        for b in plan.bundles:
            dest = root / "bundles" / b.pin.scope
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tool.extract(b.ref, b.platform, "/bundle", dest)
            b.content_dir = dest
            verify_bundle(b, core_version=bom.core_version, verifier=verifier)

        release = release_manifest(plan)
        context = root / "context"
        context.mkdir(exist_ok=True)
        (context / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")

        images: dict[str, str] = {}
        registry = registry.rstrip("/")
        for target in TARGETS:
            df = context / f"Dockerfile.{target}"
            df.write_text(dockerfile(target, plan, release), encoding="utf-8")
            repo = f"{registry}/{out_namespace}/{bom.tenant}-{target}"
            tag = f"{repo}:{bom.core_version}-{release['id'][7:19]}"
            digest = tool.build(context, df, tag, plan.core_platforms, push=push)
            images[target] = f"{repo}@{digest}"
            if push and sign:
                tool.sign(images[target], key=sign_key, insecure_registry=insecure_registry)
        return AssemblyResult(release=release, images=images, pushed=push)
    finally:
        if own_dir:
            shutil.rmtree(root, ignore_errors=True)
