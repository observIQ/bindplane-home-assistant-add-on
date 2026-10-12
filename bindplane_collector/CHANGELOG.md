# Changelog

Versions are the Bindplane collector version plus an add-on revision: `1.109.0.1` is collector v1.109.0, add-on revision 1.

## 1.109.0.1

- Prebuilt images are published to `ghcr.io/observiq/{arch}-bindplane-home-assistant-collector`, so installs and updates no longer build on your device.
- Base image is Home Assistant's Alpine 3.23 base; build settings moved from the deprecated `build.yaml` into the Dockerfile.

## 1.109.0

- Initial release with Bindplane collector v1.109.0.
