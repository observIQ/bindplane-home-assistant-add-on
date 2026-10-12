"""Bindplane sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BindplaneConfigEntry
from .api import AGENT_STATUS, bindplane_link
from .const import CONF_AGENT_ID, DOMAIN
from .exporter import Exporter


@dataclass(frozen=True, kw_only=True)
class ExporterSensorDescription(SensorEntityDescription):
    """Describes an exporter sensor."""

    value_fn: Callable[[Exporter], Any]


@dataclass(frozen=True, kw_only=True)
class CollectorSensorDescription(SensorEntityDescription):
    """Describes a collector sensor."""

    value_fn: Callable[[dict[str, Any]], Any]


EXPORTER_SENSORS = (
    ExporterSensorDescription(
        key="records_exported",
        translation_key="records_exported",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda e: e.exported,
    ),
    ExporterSensorDescription(
        key="records_dropped",
        translation_key="records_dropped",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda e: e.dropped,
    ),
    ExporterSensorDescription(
        key="metrics_exported",
        translation_key="metrics_exported",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda e: e.metrics_exported,
    ),
    ExporterSensorDescription(
        key="spans_exported",
        translation_key="spans_exported",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda e: e.spans_exported,
    ),
    ExporterSensorDescription(
        key="export_status",
        translation_key="export_status",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "error", "pending"],
        value_fn=lambda e: e.status,
    ),
)

COLLECTOR_SENSORS = (
    CollectorSensorDescription(
        key="collector_status",
        translation_key="collector_status",
        device_class=SensorDeviceClass.ENUM,
        options=[*AGENT_STATUS.values(), "unknown"],
        value_fn=lambda a: AGENT_STATUS.get(a.get("status"), "unknown"),
    ),
    CollectorSensorDescription(
        key="collector_configuration",
        translation_key="collector_configuration",
        value_fn=lambda a: (
            (a.get("configurationStatus") or {}).get("assigned") or (a.get("labels") or {}).get("configuration")
        ),
    ),
    CollectorSensorDescription(
        key="collector_version",
        translation_key="collector_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda a: a.get("version"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: BindplaneConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up sensors."""
    device = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Bindplane",
        model="Bindplane Distro for OpenTelemetry Collector",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=bindplane_link(entry.data),
    )
    data = entry.runtime_data
    entities: list[SensorEntity] = [ExporterSensor(entry, data.exporter, d, device) for d in EXPORTER_SENSORS]
    if data.coordinator:
        entities += [CollectorSensor(entry, data.coordinator, d, device) for d in COLLECTOR_SENSORS]
    async_add_entities(entities)


class ExporterSensor(SensorEntity):
    """Exporter statistics."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: ExporterSensorDescription

    def __init__(
        self,
        entry: BindplaneConfigEntry,
        exporter: Exporter,
        description: ExporterSensorDescription,
        device: DeviceInfo,
    ) -> None:
        self.entity_description = description
        self._exporter = exporter
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device

    async def async_added_to_hass(self) -> None:
        """Update when the exporter flushes."""
        self.async_on_remove(self._exporter.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._exporter)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "export_status":
            return {"last_error": self._exporter.last_error, "url": self._exporter.url}
        return None


class CollectorSensor(CoordinatorEntity, SensorEntity):
    """Collector state as reported by Bindplane."""

    _attr_has_entity_name = True
    entity_description: CollectorSensorDescription

    def __init__(
        self, entry: BindplaneConfigEntry, coordinator: Any, description: CollectorSensorDescription, device: DeviceInfo
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device
        self._agent_id = entry.data[CONF_AGENT_ID]

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return "unknown" if self.entity_description.key == "collector_status" else None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "collector_status":
            data = self.coordinator.data or {}
            return {"agent_id": self._agent_id, "error": data.get("errorMessage") or data.get("healthErrorMessage")}
        return None
