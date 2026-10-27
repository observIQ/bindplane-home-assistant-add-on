"""Config flow for Bindplane."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from aiohasupervisor import SupervisorError
from aiohasupervisor.models import Job, StoreAddRepository
import voluptuous as vol

from homeassistant.components.hassio import (
    AddonError,
    AddonManager,
    AddonState,
    get_supervisor_client,
)
from homeassistant.config_entries import (
    SOURCE_HASSIO,
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import (
    BindplaneAuthError,
    BindplaneClient,
    BindplaneError,
    BindplanePermissionError,
    bindplane_link,
    opamp_endpoint,
)
from .const import (
    ADDON_NAME,
    ADDON_SLUG,
    ALL_EVENTS,
    CHANGE_API_KEY,
    CONF_ADDON_SLUG,
    CONF_AGENT_ID,
    CONF_API_KEY,
    CONF_API_URL,
    CONF_CONFIGURATION,
    CONF_DESTINATIONS,
    CONF_ENDPOINT,
    CONF_EVENTS,
    CONF_EXCLUDE,
    CONF_LEVEL,
    CONF_METRICS,
    CONF_METRICS_EXCLUDE,
    CONF_OTLP_URL,
    CONF_PROJECT_ID,
    CONF_SECRET_KEY,
    CONF_SUPERVISOR_LOGS,
    CONF_TRACES,
    CREATE_CONFIGURATION,
    DEFAULT_API_URL,
    DEFAULT_CONFIGURATION,
    DEFAULT_EVENTS,
    DEFAULT_LEVEL,
    DOMAIN,
    REPOSITORY_URL,
)

_LOGGER = logging.getLogger(__name__)

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SUGGESTED_EVENTS = [
    "automation_triggered",
    "script_started",
    "call_service",
    "state_changed",
    "homeassistant_started",
    "homeassistant_stop",
    "component_loaded",
    "core_config_updated",
    "user_added",
    "user_removed",
]
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


def job_progress(job: Job) -> float:
    """Highest progress of a running Supervisor job or its children."""
    return max([job.progress, *(job_progress(c) for c in job.child_jobs if not c.done)])


class BindplaneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Bindplane."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._addon_slug: str | None = None
        self._addon_task: asyncio.Task | None = None
        self._client: BindplaneClient | None = None
        self._secret_key = ""
        self._configurations: list[str] = []
        self._destinations: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BindplaneOptionsFlow:
        """Return the options flow."""
        return BindplaneOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Install the add-on, unless an external collector is given under Advanced."""
        if not is_hassio(self.hass):
            return await self.async_step_external()
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Optional("advanced"): section(
                            vol.Schema({vol.Optional(CONF_OTLP_URL): URL}), {"collapsed": True}
                        )
                    }
                ),
            )
        if url := user_input.get("advanced", {}).get(CONF_OTLP_URL):
            return await self.async_step_external({CONF_OTLP_URL: url})
        return await self.async_step_addon()

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Handle the add-on announcing itself."""
        self._addon_slug = discovery_info.slug
        await self._async_use_discovery(discovery_info.config)
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm setting up the discovered collector."""
        if user_input is None:
            self._set_confirm_only()
            return self.async_show_form(step_id="hassio_confirm")
        return await self.async_step_api_key()

    async def _async_use_discovery(self, config: dict[str, Any]) -> None:
        url = f"http://{config['host']}:{config['port']}"
        await self.async_set_unique_id(config[CONF_AGENT_ID], raise_on_progress=self.source == SOURCE_HASSIO)
        self._abort_if_unique_id_configured(updates={CONF_OTLP_URL: url})
        self._data = {CONF_OTLP_URL: url, CONF_AGENT_ID: config[CONF_AGENT_ID], CONF_ADDON_SLUG: self._addon_slug}

    def _addon_manager(self) -> AddonManager:
        assert self._addon_slug
        return AddonManager(self.hass, _LOGGER, ADDON_NAME, self._addon_slug)

    async def _async_find_addon_slug(self) -> str:
        client = get_supervisor_client(self.hass)

        async def find() -> str | None:
            return next((a.slug for a in await client.store.addons_list() if a.slug.endswith(f"_{ADDON_SLUG}")), None)

        if slug := await find():
            return slug
        await client.store.add_repository(StoreAddRepository(repository=REPOSITORY_URL))
        if slug := await find():
            return slug
        raise AddonError(f"{ADDON_NAME} not found in the add-on store")

    async def async_step_addon(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Use the Bindplane Collector add-on, installing and starting it if needed."""
        try:
            self._addon_slug = await self._async_find_addon_slug()
            info = await self._addon_manager().async_get_addon_info()
        except (AddonError, SupervisorError) as err:
            _LOGGER.error("Could not find the %s add-on: %s", ADDON_NAME, err)
            return self.async_abort(reason="addon_not_found")
        if info.state is AddonState.NOT_INSTALLED:
            return await self.async_step_install_addon()
        return await self.async_step_start_addon()

    async def async_step_install_addon(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Install the add-on."""
        return await self._async_addon_progress("install_addon", self._async_install_addon, "start_addon")

    async def _async_install_addon(self) -> None:
        install = self.hass.async_create_task(self._addon_manager().async_install_addon())
        jobs = get_supervisor_client(self.hass).jobs
        while not install.done():
            try:
                running = [j for j in (await jobs.info()).jobs if j.reference == self._addon_slug and not j.done]
            except SupervisorError:
                running = []
            if running:
                self.async_update_progress(max(map(job_progress, running)) / 100)
            await asyncio.wait({install}, timeout=1)
        await install

    async def async_step_start_addon(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Start the add-on and wait for it to announce itself."""
        return await self._async_addon_progress("start_addon", self._async_start_addon, "addon_ready")

    async def _async_start_addon(self) -> None:
        manager = self._addon_manager()
        if (await manager.async_get_addon_info()).state is not AddonState.RUNNING:
            await manager.async_start_addon()
        for _ in range(30):
            try:
                await self._async_use_discovery(await manager.async_get_addon_discovery_info())
            except AddonError:
                await asyncio.sleep(1)
            else:
                return
        raise AddonError(f"{ADDON_NAME} did not announce itself")

    async def _async_addon_progress(self, step_id: str, job: Any, next_step_id: str) -> ConfigFlowResult:
        if not self._addon_task:
            self._addon_task = self.hass.async_create_task(job())
        if not self._addon_task.done():
            return self.async_show_progress(step_id=step_id, progress_action=step_id, progress_task=self._addon_task)
        try:
            await self._addon_task
        except (AddonError, SupervisorError) as err:
            _LOGGER.error("%s add-on step %s failed: %s", ADDON_NAME, step_id, err)
            return self.async_show_progress_done(next_step_id="addon_failed")
        finally:
            self._addon_task = None
        return self.async_show_progress_done(next_step_id=next_step_id)

    async def async_step_addon_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add-on install or start failed."""
        return self.async_abort(reason="addon_failed")

    async def async_step_addon_ready(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add-on is running."""
        return await self.async_step_api_key()

    async def async_step_api_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Connect with a Bindplane API key."""
        if user_input is None:
            return self._show_api_key_form()
        self._client = BindplaneClient(
            async_get_clientsession(self.hass),
            user_input[CONF_API_URL],
            user_input[CONF_API_KEY],
            user_input.get(CONF_PROJECT_ID),
        )
        self._data.pop(CONF_PROJECT_ID, None)
        self._data.update(user_input)
        try:
            self._secret_key = await self._client.secret_key()
            self._configurations = await self._client.configurations()
            self._destinations = await self._client.destinations()
            if agent_id := self._data.get(CONF_AGENT_ID):
                await self._client.collector(agent_id)
        except BindplaneAuthError as err:
            return self._show_api_key_form("invalid_auth", str(err))
        except BindplanePermissionError as err:
            return self._show_api_key_form("missing_permission", str(err))
        except BindplaneError as err:
            return self._show_api_key_form("cannot_connect", str(err))
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=self._data)
        return await self.async_step_configuration()

    def _show_api_key_form(self, error: str | None = None, detail: str = "") -> ConfigFlowResult:
        schema = vol.Schema(
            {
                vol.Required(CONF_API_URL, default=DEFAULT_API_URL): URL,
                vol.Required(CONF_API_KEY): PASSWORD,
                vol.Optional(CONF_PROJECT_ID): str,
            }
        )
        suggested = {k: self._data[k] for k in (CONF_API_URL, CONF_PROJECT_ID) if self._data.get(k)}
        return self.async_show_form(
            step_id="api_key",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors={"base": error} if error else None,
            description_placeholders={"error": detail, "cloud_url": DEFAULT_API_URL},
        )

    async def async_step_configuration(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick or create the Bindplane configuration for this collector."""
        assert self._client
        errors: dict[str, str] = {}
        placeholders = {"configuration": DEFAULT_CONFIGURATION, "error": ""}
        if user_input is not None:
            name = user_input[CONF_CONFIGURATION]
            if name == CHANGE_API_KEY:
                return self._show_api_key_form()
            try:
                if name == CREATE_CONFIGURATION:
                    name = DEFAULT_CONFIGURATION
                    await self._client.apply_configuration(user_input.get(CONF_DESTINATIONS, []), name)
            except BindplaneAuthError as err:
                return self._show_api_key_form("invalid_auth", str(err))
            except BindplanePermissionError as err:
                return self._show_api_key_form("missing_permission", str(err))
            except BindplaneError as err:
                errors["base"] = "apply_failed"
                placeholders["error"] = str(err)
            else:
                self._data[CONF_CONFIGURATION] = name
                return await self._async_finish(
                    opamp_endpoint(self._data[CONF_API_URL]), self._secret_key, [f"configuration={name}"]
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_CONFIGURATION, default=CREATE_CONFIGURATION): SelectSelector(
                    SelectSelectorConfig(
                        options=[CREATE_CONFIGURATION, *self._configurations, CHANGE_API_KEY],
                        translation_key="configuration",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_DESTINATIONS, default=[]): SelectSelector(
                    SelectSelectorConfig(options=self._destinations, multiple=True)
                ),
            }
        )
        return self.async_show_form(
            step_id="configuration", data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    async def _async_finish(self, endpoint: str, secret_key: str, labels: list[str]) -> ConfigFlowResult:
        manager = self._addon_manager()
        try:
            options = dict((await manager.async_get_addon_info()).options)
            options.update({CONF_ENDPOINT: endpoint, CONF_SECRET_KEY: secret_key})
            kept = [x for x in options.get("labels", []) if not x.startswith("configuration=")]
            options["labels"] = [*kept, *labels]
            await manager.async_set_addon_options(options)
            await manager.async_restart_addon()
        except (AddonError, SupervisorError) as err:
            _LOGGER.error("Could not configure the %s add-on: %s", ADDON_NAME, err)
            return self.async_abort(reason="addon_set_config_failed")

        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data=self._data)
        return self.async_create_entry(title=ADDON_NAME, data=self._data)

    async def async_step_external(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Send to a collector that runs outside Home Assistant."""
        if user_input is not None:
            url = user_input[CONF_OTLP_URL].rstrip("/")
            if self.source == SOURCE_RECONFIGURE:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(), data_updates={CONF_OTLP_URL: url}
                )
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=url, data={CONF_OTLP_URL: url})
        return self.async_show_form(
            step_id="external",
            data_schema=vol.Schema(
                {vol.Required(CONF_OTLP_URL, default=self._data.get(CONF_OTLP_URL, "http://")): URL}
            ),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Ask for a new API key after Bindplane rejected the old one."""
        self._data = dict(entry_data)
        return self._show_api_key_form()

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the Bindplane connection."""
        self._data = dict(self._get_reconfigure_entry().data)
        self._addon_slug = self._data.get(CONF_ADDON_SLUG)
        if self._addon_slug:
            return await self.async_step_api_key()
        return await self.async_step_external()


class BindplaneOptionsFlow(OptionsFlow):
    """What to export."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_LEVEL, default=DEFAULT_LEVEL): SelectSelector(
                    SelectSelectorConfig(options=LEVELS, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional(CONF_EXCLUDE, default=[]): TextSelector(TextSelectorConfig(multiple=True)),
                vol.Optional(CONF_EVENTS, default=DEFAULT_EVENTS): SelectSelector(
                    SelectSelectorConfig(
                        options=[ALL_EVENTS, *SUGGESTED_EVENTS],
                        multiple=True,
                        custom_value=True,
                        translation_key="events",
                    )
                ),
                vol.Required(CONF_METRICS, default=True): BooleanSelector(),
                vol.Required(CONF_TRACES, default=True): BooleanSelector(),
                vol.Required(CONF_SUPERVISOR_LOGS, default=True): BooleanSelector(),
                vol.Optional(CONF_METRICS_EXCLUDE, default=[]): EntitySelector(EntitySelectorConfig(multiple=True)),
            }
        )
        link = bindplane_link(self.config_entry.data)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
            description_placeholders={"link": f"[Open in Bindplane ↗]({link})" if link else ""},
        )
