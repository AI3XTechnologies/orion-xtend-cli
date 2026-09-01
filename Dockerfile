# oxtend as a CI image: `oxtend:<version>`.
#
# Bundles cosign so the reusable extension workflow does not have to install it per
# job — signing is a required step, and a required step that depends on a network
# install is a required step that flakes.
FROM python:3.13-slim AS base

ARG COSIGN_VERSION=2.4.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && curl -fsSL -o /usr/local/bin/cosign \
      "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-amd64" \
 && chmod +x /usr/local/bin/cosign \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# orion-backend is private and published to no index, so `pip install .` alone can
# never resolve it — that is why this image had never been built. The workflow checks
# core out and builds its wheel into core-dist/ before calling docker build.
#
# --no-deps is deliberate. Core's full dependency set pulls the whole RAG stack
# (torch, docling, litellm) for an image that only ever imports orion.kernel.*, so
# the kernel's own requirements are installed explicitly instead. Keep this list in
# step with what orion/kernel imports; a missing one surfaces as
# VendoredKernelUnavailable at runtime, not at build time.
COPY core-dist/ ./core-dist/
RUN pip install --no-cache-dir --no-deps ./core-dist/*.whl \
 && pip install --no-cache-dir \
      "pydantic>=2" "sqlalchemy>=2" structlog sqlparse packaging pyyaml

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
 && oxtend --help >/dev/null \
 && python -c "from oxtend.manifest_vendored import kernel_manifest_module, kernel_migrations_module; kernel_manifest_module(); kernel_migrations_module(); print('vendored kernel modules import OK')"

# `oxtend package` needs a container CLI; in CI that is the runner's docker socket
# mounted in, which is why one is not installed here.
WORKDIR /work
ENTRYPOINT ["oxtend"]
CMD ["--help"]
