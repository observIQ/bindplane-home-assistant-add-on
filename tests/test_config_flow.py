"""Tests for the Bindplane config flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bindplane.config_flow import AddonState
from custom_components.bindplane.const import DOMAIN

API = "https://app.bindplane.com/v1"
DISCOVERY = HassioServiceInfo(
    config={"host": "abcd1234-bindplane-collector", "port": 4318, "agent_id": "agent-1"},
    name="Bindplane Collector",
    slug="abcd1234_bindplane_collector",
    uuid="1234",
)


@pytest.fixture
def addon():
    manager = MagicMock()
    manager.async_get_addon_info = AsyncMock(
        return_value=MagicMock(options={"endpoint": "", "secret_key": "", "labels": ["room=den", "configuration=old"]})
    )
    manager.async_set_addon_options = AsyncMock()
    manager.async_restart_addon = AsyncMock()
    with (
        patch("custom_components.bindplane.config_flow.AddonManager", return_value=manager),
        patch("custom_components.bindplane.async_setup_entry", return_value=True),
        patch("custom_components.bindplane.async_unload_entry", return_value=True),
    ):
        yield manager


async def discovered(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=DISCOVERY)
    assert result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "api_key"
    return result


async def test_discovery_with_api_key_creates_configuration(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, addon
):
    aioclient_mock.get(f"{API}/secret-keys", json={"defaultSecretKey": "s3cret"})
    aioclient_mock.get(f"{API}/configurations", json={"configurations": [{"metadata": {"name": "Mac"}}]})
    aioclient_mock.get(f"{API}/destinations", json={"destinations": [{"metadata": {"name": "grafana"}}]})
    aioclient_mock.get(f"{API}/collectors/agent-1", status=404)
    aioclient_mock.post(f"{API}/apply", json={"updates": [{"status": "created"}]})
    aioclient_mock.post(f"{API}/rollouts/home-assistant/start", json={})

    result = await discovered(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_url": "https://app.bindplane.com", "api_key": "key"}
    )
    assert result["step_id"] == "configuration"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"configuration": "create-home-assistant", "destinations": ["grafana"]}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "otlp_url": "http://abcd1234-bindplane-collector:4318",
        "agent_id": "agent-1",
        "addon_slug": "abcd1234_bindplane_collector",
        "api_url": "https://app.bindplane.com",
        "api_key": "key",
        "configuration": "home-assistant",
    }
    assert result["result"].unique_id == "agent-1"
    applied = next(c[2] for c in aioclient_mock.mock_calls if str(c[1]).endswith("/apply"))
    resource = applied["resources"][0]
    assert resource["apiVersion"] == "bindplane.observiq.com/v2"
    spec = resource["spec"]
    source = spec["sources"][0]
    assert source["type"] == "otlp"
    route = [{"id": "0", "components": ["destinations/d-0"]}]
    assert source["routes"] == {"logs": route, "metrics": route, "traces": route}
    assert spec["destinations"] == [{"id": "d-0", "name": "grafana"}]
    assert len(spec["sources"]) == 1
    assert spec["selector"] == {"matchLabels": {"configuration": "home-assistant"}}
    addon.async_set_addon_options.assert_awaited_once_with(
        {
            "endpoint": "wss://app.bindplane.com/v1/opamp",
            "secret_key": "s3cret",
            "labels": ["room=den", "configuration=home-assistant"],
        }
    )
    addon.async_restart_addon.assert_awaited_once()


async def test_bad_api_key(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, addon):
    aioclient_mock.get(f"{API}/secret-keys", status=401)
    result = await discovered(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_url": "https://app.bindplane.com", "api_key": "nope"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_external_collector(hass: HomeAssistant, addon):
    with patch("custom_components.bindplane.config_flow.is_hassio", return_value=False):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "external"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"otlp_url": "http://10.0.0.2:4318/"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"otlp_url": "http://10.0.0.2:4318"}


async def test_options(hass: HomeAssistant, addon):
    OPTIONS = {
        "level": "WARNING",
        "exclude_loggers": ["aiohttp"],
        "events": ["call_service"],
        "metrics": True,
        "metrics_exclude": ["sensor.noisy"],
        "traces": True,
        "supervisor_logs": True,
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"otlp_url": "http://x:4318"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], OPTIONS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == OPTIONS


async def test_user_installs_and_starts_addon(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, addon):
    aioclient_mock.get(f"{API}/secret-keys", json={"defaultSecretKey": "s3cret"})
    aioclient_mock.get(f"{API}/configurations", json={"configurations": []})
    aioclient_mock.get(f"{API}/destinations", json={"destinations": []})
    aioclient_mock.get(f"{API}/collectors/agent-1", status=404)
    aioclient_mock.post(f"{API}/apply", json={"updates": [{"status": "created"}]})
    aioclient_mock.post(f"{API}/rollouts/home-assistant/start", json={})
    addon.async_get_addon_info.return_value.state = AddonState.NOT_INSTALLED
    addon.async_install_addon = AsyncMock(
        side_effect=lambda: setattr(addon.async_get_addon_info.return_value, "state", AddonState.NOT_RUNNING)
    )
    addon.async_start_addon = AsyncMock()
    addon.async_get_addon_discovery_info = AsyncMock(return_value=DISCOVERY.config)
    supervisor = MagicMock()
    supervisor.store.addons_list = AsyncMock(side_effect=[[], [MagicMock(slug=DISCOVERY.slug)]])
    supervisor.store.add_repository = AsyncMock()

    with (
        patch("custom_components.bindplane.config_flow.is_hassio", return_value=True),
        patch("custom_components.bindplane.config_flow.get_supervisor_client", return_value=supervisor),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM and result["step_id"] == "api_key"
    supervisor.store.add_repository.assert_awaited_once()
    addon.async_start_addon.assert_awaited_once()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_url": "https://app.bindplane.com", "api_key": "k"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"configuration": "create-home-assistant"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["addon_slug"] == DISCOVERY.slug


async def test_missing_permission_on_apply_returns_to_api_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, addon
):
    aioclient_mock.get(f"{API}/secret-keys", json={"defaultSecretKey": "s3cret"})
    aioclient_mock.get(f"{API}/configurations", json={"configurations": []})
    aioclient_mock.get(f"{API}/destinations", json={"destinations": []})
    aioclient_mock.get(f"{API}/collectors/agent-1", status=404)
    aioclient_mock.post(f"{API}/apply", status=403, json={"errors": ["missing permission WriteResources"]})

    result = await discovered(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_url": "https://app.bindplane.com", "api_key": "bps_x", "project_id": "p1"}
    )
    assert result["step_id"] == "configuration"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"configuration": "create-home-assistant"}
    )
    assert result["step_id"] == "api_key"
    assert result["errors"] == {"base": "missing_permission"}
    assert result["description_placeholders"]["error"] == "missing permission WriteResources"

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/secret-keys", json={"defaultSecretKey": "s3cret"})
    aioclient_mock.get(f"{API}/configurations", json={"configurations": []})
    aioclient_mock.get(f"{API}/destinations", json={"destinations": []})
    aioclient_mock.get(f"{API}/collectors/agent-1", status=404)
    aioclient_mock.post(f"{API}/apply", json={"updates": [{"status": "created"}]})
    aioclient_mock.post(f"{API}/rollouts/home-assistant/start", json={})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_url": "https://app.bindplane.com", "api_key": "bps_better", "project_id": "p1"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"configuration": "create-home-assistant"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["api_key"] == "bps_better"

    applied = next(c[2] for c in aioclient_mock.mock_calls if str(c[1]).endswith("/apply"))["resources"]
    assert applied[0]["kind"] == "Destination" and applied[0]["spec"]["type"] == "dev_null"
    assert applied[1]["spec"]["destinations"] == [{"id": "d-0", "name": "home-assistant-dev-null"}]
    assert applied[1]["spec"]["sources"][0]["routes"]["logs"] == [{"id": "0", "components": ["destinations/d-0"]}]


async def test_missing_read_permission_shown_on_api_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, addon
):
    aioclient_mock.get(f"{API}/secret-keys", status=403, json={"errors": ["missing permission ReadSecrets"]})
    result = await discovered(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_url": "https://app.bindplane.com", "api_key": "bps_x", "project_id": "p1"}
    )
    assert result["errors"] == {"base": "missing_permission"}
    assert result["description_placeholders"]["error"] == "missing permission ReadSecrets"


async def test_change_api_key_from_configuration(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, addon):
    aioclient_mock.get(f"{API}/secret-keys", json={"defaultSecretKey": "s3cret"})
    aioclient_mock.get(f"{API}/configurations", json={"configurations": []})
    aioclient_mock.get(f"{API}/destinations", json={"destinations": []})
    aioclient_mock.get(f"{API}/collectors/agent-1", status=404)
    result = await discovered(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_url": "https://app.bindplane.com", "api_key": "k"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"configuration": "change-api-key"})
    assert result["step_id"] == "api_key" and not result["errors"]


async def test_external_collector_under_advanced(hass: HomeAssistant, addon):
    with patch("custom_components.bindplane.config_flow.is_hassio", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"advanced": {"otlp_url": "http://10.0.0.5:4318"}}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"otlp_url": "http://10.0.0.5:4318"}
    addon.async_set_addon_options.assert_not_awaited()
