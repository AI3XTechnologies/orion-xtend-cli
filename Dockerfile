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
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# `oxtend package` needs a container CLI; in CI that is the runner's docker socket
# mounted in, which is why one is not installed here.
WORKDIR /work
ENTRYPOINT ["oxtend"]
CMD ["--help"]
