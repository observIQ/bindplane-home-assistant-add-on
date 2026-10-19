# Bindplane Collector

> [!WARNING]
> Bindplane does not provide support for this add-on or integration specifically. Please report problems through [GitHub issues](https://github.com/observIQ/bindplane-home-assistant-add-on/issues).

Runs the Bindplane Distro for OpenTelemetry Collector, managed remotely by Bindplane.

## Setup

1. Start this add-on.
2. Go to **Settings → Devices & services**. Home Assistant shows **Bindplane Collector discovered**.
3. Click **Configure** and follow the steps. The integration writes the endpoint, secret key and labels into this add-on's options for you.

You can also fill in the options yourself and attach a configuration in Bindplane.

## Options

| Option | Description |
|---|---|
| `endpoint` | Bindplane OpAMP URL. Bindplane Cloud: `wss://app.bindplane.com/v1/opamp`. |
| `secret_key` | Bindplane secret key. |
| `agent_name` | Name shown in Bindplane. |
| `labels` | `key=value` labels. Bindplane assigns configurations by label, e.g. `configuration=home-assistant`. |
| `tls_insecure` | Skip TLS verification for self-hosted Bindplane. |

## Network

Home Assistant reaches the collector over the internal add-on network. You don't need to map any ports. Map 4317/4318 only if devices outside Home Assistant should send OTLP to this collector.

The collector's configuration and identity are stored in the add-on's `/data`, so the collector keeps its identity in Bindplane across restarts and updates.
