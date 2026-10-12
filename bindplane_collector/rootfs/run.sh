#!/usr/bin/with-contenv bashio
set -e

export OIQ_OTEL_COLLECTOR_HOME=/data
cd /data
ln -sfn /opt/bdot/plugins /data/plugins
mkdir -p /data/storage
[ -f /data/config.yaml ] || cp /opt/bdot/config.yaml /data/config.yaml
[ -f /data/agent_id ] || cat /proc/sys/kernel/random/uuid > /data/agent_id
agent_id=$(cat /data/agent_id)

bashio::discovery "bindplane" "$(bashio::var.json \
    host "$(bashio::addon.hostname)" \
    port "^4318" \
    agent_id "${agent_id}")" > /dev/null \
    && bashio::log.info "Announced collector ${agent_id} to Home Assistant"

if ! bashio::config.has_value secret_key; then
    bashio::log.warning "No secret key yet. Set up the Bindplane integration in Home Assistant to connect this collector."
    exec sleep infinity
fi

labels=$(bashio::config 'labels' | paste -sd, -)
jq -n \
    --arg endpoint "$(bashio::config endpoint)" \
    --arg secret_key "$(bashio::config secret_key)" \
    --arg agent_id "${agent_id}" \
    --arg agent_name "$(bashio::config agent_name)" \
    --arg labels "${labels}" \
    --argjson insecure "$(bashio::config tls_insecure)" \
    '{endpoint: $endpoint, secret_key: $secret_key, agent_id: $agent_id, agent_name: $agent_name, labels: $labels}
     + if $insecure then {tls_config: {insecure_skip_verify: true}} else {} end' \
    > /data/manager.yaml

printf 'output: stdout\nlevel: info\n' > /data/logging.yaml

bashio::log.info "Starting Bindplane collector, connecting to $(bashio::config endpoint)"
exec /opt/bdot/observiq-otel-collector \
    --config /data/config.yaml \
    --manager /data/manager.yaml \
    --logging /data/logging.yaml
