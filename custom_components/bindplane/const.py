"""Constants for the Bindplane integration."""

DOMAIN = "bindplane"

REPOSITORY_URL = "https://github.com/observIQ/bindplane-home-assistant-add-on"
ADDON_NAME = "Bindplane Collector"
ADDON_SLUG = "bindplane_collector"

CONF_OTLP_URL = "otlp_url"
CONF_AGENT_ID = "agent_id"
CONF_ADDON_SLUG = "addon_slug"
CONF_API_URL = "api_url"
CONF_API_KEY = "api_key"
CONF_PROJECT_ID = "project_id"
CONF_ENDPOINT = "endpoint"
CONF_SECRET_KEY = "secret_key"
CONF_CONFIGURATION = "configuration"
CONF_DESTINATIONS = "destinations"

CONF_LEVEL = "level"
CONF_EXCLUDE = "exclude_loggers"
CONF_EVENTS = "events"
CONF_METRICS = "metrics"
CONF_METRICS_EXCLUDE = "metrics_exclude"
CONF_TRACES = "traces"

DEFAULT_API_URL = "https://app.bindplane.com"
DEFAULT_CONFIGURATION = "home-assistant"
DEV_NULL_DESTINATION = "home-assistant-dev-null"
DEFAULT_LEVEL = "INFO"
ALL_EVENTS = "all"
DEFAULT_EVENTS = [ALL_EVENTS]
CREATE_CONFIGURATION = "create-home-assistant"
CHANGE_API_KEY = "change-api-key"
