ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-debian:trixie

ARG BUILD_ARCH
ARG BUILD_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends systemd \
    && rm -rf /var/lib/apt/lists/*

RUN case "${BUILD_ARCH}" in aarch64) a=arm64 ;; *) a=amd64 ;; esac \
    && mkdir -p /opt/bdot \
    && curl -fsSL "https://github.com/observIQ/bindplane-otel-collector/releases/download/v${BUILD_VERSION}/observiq-otel-collector-v${BUILD_VERSION}-linux-${a}.tar.gz" \
       | tar xz -C /opt/bdot \
    && rm -rf /opt/bdot/install /opt/bdot/updater /opt/bdot/gpg-keys.tar.gz

COPY rootfs /
CMD ["/run.sh"]
