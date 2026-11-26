# Agent image: python + git + helm + kustomize (pinned, checksum-verified). Runs as UID 1000.
# Base pinned to bookworm: trixie apt (sqv) fails signature checks under qemu when cross-building linux/amd64.
# Build: docker build -t <registry>/rightsizing-agent:<tag> -f Dockerfile .
FROM python:3.12-slim-bookworm AS tools
ARG TARGETARCH=amd64
ARG HELM_VERSION=v3.16.3
ARG HELM_SHA256_amd64=f5355c79190951eed23c5432a3b920e071f4c00a64f75e077de0dd4cb7b294ea
ARG HELM_SHA256_arm64=5bd34ed774df6914b323ff84a0a156ea6ff2ba1eaf0113962fa773f3f9def798
ARG KUSTOMIZE_VERSION=v5.5.0
ARG KUSTOMIZE_SHA256_amd64=6703a3a70a0c47cf0b37694030b54f1175a9dfeb17b3818b623ed58b9dbc2a77
ARG KUSTOMIZE_SHA256_arm64=b4170d1acb8cfacace9f72884bef957ff56efdcd4813b66e7604aabc8b57e93d
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "$TARGETARCH" in amd64) hs="$HELM_SHA256_amd64"; ks="$KUSTOMIZE_SHA256_amd64";; \
                          arm64) hs="$HELM_SHA256_arm64"; ks="$KUSTOMIZE_SHA256_arm64";; \
                          *) echo "unsupported arch $TARGETARCH"; exit 1;; esac; \
    curl -fsSLo /tmp/helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    echo "${hs}  /tmp/helm.tgz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tgz -C /tmp && install -m 0755 "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm; \
    curl -fsSLo /tmp/kustomize.tgz "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    echo "${ks}  /tmp/kustomize.tgz" | sha256sum -c -; \
    tar -xzf /tmp/kustomize.tgz -C /usr/local/bin kustomize && chmod 0755 /usr/local/bin/kustomize

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --home-dir /home/rightsizing rightsizing
COPY --from=tools /usr/local/bin/helm /usr/local/bin/kustomize /usr/local/bin/
WORKDIR /app
COPY pyproject.toml ./
COPY rightsizing ./rightsizing
RUN pip install --no-cache-dir . && rm -rf /root/.cache
USER 1000:1000
# HOME, /tmp and /work are emptyDir mounts so the root filesystem can stay read-only.
ENV HOME=/home/rightsizing HELM_CACHE_HOME=/tmp/helm/cache HELM_CONFIG_HOME=/tmp/helm/config \
    HELM_DATA_HOME=/tmp/helm/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "rightsizing"]
CMD ["--config", "/etc/rightsizing/config.yaml", "--workdir", "/work"]
