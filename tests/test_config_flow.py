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
    ):
        yield manager


async def discovered(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=DISCOVERY)
    assert result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "credentials"
    return result


async def test_discovery_with_api_key_creates_configuration(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, addon
):
    aioclient_mock.get(f"{API}/secret-keys", json={"defaultSecretKey": "s3cret"})
    aioclient_mock.get(f"{API}/configurations", json={"configurations": [{"metadata": {"name": "Mac"}}]})
    aioclient_mock.get(f"{API}/destinations", json={"destinations": [{"metadata": {"name": "grafana"}}]})
    aioclient_mock.post(f"{API}/apply", json={"updates": [{"status": "created"}]})
    aioclient_mock.post(f"{API}/rollouts/home-assistant/start", json={})

    result = await discovered(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "api_key"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_url": "https://app.bindplane.com", "api_key": "key"}
    )
    assert result["step_id"] == "configuration"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"configuration": "__create__", "destinations": ["grafana"]}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "otlp_url": "http://abcd1234-bindplane-collector:4318",
        "agent_id": "agent-1",
        "addon_slug": "abcd1234_bindplane_collector",
        "api_url": "https://app.bindplane.com",
        "api_key": "key",
    }
    assert result["result"].unique_id == "agent-1"
    applied = next(c[2] for c in aioclient_mock.mock_calls if str(c[1]).endswith("/apply"))
    spec = applied["resources"][0]["spec"]
    assert spec["sources"][0]["type"] == "otlp"
    assert spec["destinations"] == [{"name": "grafana"}]
    assert spec["selector"] == {"matchLabels": {"configuration": "home-assistant"}}
    addon.async_set_addon_options.assert_awaited_once_with(
        {
            "endpoint": "wss://app.bindplane.com/v1/opamp",
            "secret_key": "s3cret",
            "labels": ["room=den", "configuration=home-assistant"],
        }
    )
    addon.async_restart_addon.assert_awaited_once()


async def test_discovery_with_secret_key(hass: HomeAssistant, addon):
    result = await discovered(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "secret_key"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"endpoint": "wss://bp.local/v1/opamp", "secret_key": "abc"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "api_key" not in result["data"]
    options = addon.async_set_addon_options.await_args.args[0]
    assert options["secret_key"] == "abc" and options["labels"] == ["room=den", "configuration=old"]


async def test_bad_api_key(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, addon):
    aioclient_mock.get(f"{API}/secret-keys", status=401)
    result = await discovered(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "api_key"})
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
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"otlp_url": "http://x:4318"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], OPTIONS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == OPTIONS


async def test_user_installs_and_starts_addon(hass: HomeAssistant, addon):
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
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "addon"})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.MENU and result["step_id"] == "credentials"
    supervisor.store.add_repository.assert_awaited_once()
    addon.async_start_addon.assert_awaited_once()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "secret_key"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"endpoint": "wss://app.bindplane.com/v1/opamp", "secret_key": "abc"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["addon_slug"] == DISCOVERY.slug
