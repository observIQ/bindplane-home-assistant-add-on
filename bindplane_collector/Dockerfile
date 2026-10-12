ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.23

ARG BUILD_ARCH
ARG BUILD_VERSION
RUN case "${BUILD_ARCH}" in aarch64) a=arm64 ;; *) a=amd64 ;; esac \
    && v=$(echo "${BUILD_VERSION}" | cut -d. -f1-3) \
    && mkdir -p /opt/bdot \
    && curl -fsSL "https://github.com/observIQ/bindplane-otel-collector/releases/download/v${v}/observiq-otel-collector-v${v}-linux-${a}.tar.gz" \
       | tar xz -C /opt/bdot \
    && rm -rf /opt/bdot/install /opt/bdot/updater /opt/bdot/gpg-keys.tar.gz

COPY rootfs /
CMD ["/run.sh"]
