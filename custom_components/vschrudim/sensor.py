"""Senzory pro VS Chrudim - stav vodoměru a čas posledního odečtu."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PRAGUE_TZ
from .coordinator import VSChrudimCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: VSChrudimCoordinator = entry.runtime_data

    known_place_ids: set[str] = set()

    def _add_new_entities() -> None:
        new_entities: list[SensorEntity] = []

        for place_id in coordinator.data or {}:
            if place_id in known_place_ids:
                continue

            known_place_ids.add(place_id)
            new_entities.append(
                VSChrudimValueSensor(coordinator, place_id)
            )
            new_entities.append(
                VSChrudimTimeSensor(coordinator, place_id)
            )

        if new_entities:
            async_add_entities(new_entities)

    # Odběrná místa se nemění za běhu často, ale kdyby uživatel
    # získal nové (další nemovitost apod.), entity se dopočítají
    # samy při dalším pravidelném refreshi - bez restartu HA.
    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class VSChrudimBaseSensor(CoordinatorEntity[VSChrudimCoordinator], SensorEntity):
    """Společný základ - vyhledání dat pro konkrétní odběrné místo."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: VSChrudimCoordinator, place_id: str
    ) -> None:
        super().__init__(coordinator)
        self._place_id = place_id

    @property
    def _place(self) -> dict | None:
        return (self.coordinator.data or {}).get(self._place_id)

    @property
    def available(self) -> bool:
        return super().available and self._place is not None

    @property
    def device_info(self) -> DeviceInfo:
        place = self._place or {}
        label = place.get("adresa") or self._place_id

        return DeviceInfo(
            identifiers={(DOMAIN, self._place_id)},
            name=f"Vodoměr {label}",
            manufacturer="Vodárenská společnost Chrudim",
            model=place.get("meridlo"),
        )


class VSChrudimValueSensor(VSChrudimBaseSensor):
    """Aktuální (poslední známý) stav vodoměru v m3."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_name = "Stav vodoměru"

    def __init__(
        self, coordinator: VSChrudimCoordinator, place_id: str
    ) -> None:
        super().__init__(coordinator, place_id)
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{place_id}_state"
        )

    @property
    def native_value(self) -> float | None:
        place = self._place
        return place["value"] if place else None

    @property
    def extra_state_attributes(self) -> dict:
        place = self._place
        if not place:
            return {}

        return {
            "evidencni_cislo": place.get("evidencni_cislo"),
            "technicke_cislo": place.get("technicke_cislo"),
            "adresa": place.get("adresa"),
            "meridlo": place.get("meridlo"),
        }


class VSChrudimTimeSensor(VSChrudimBaseSensor):
    """Čas, ke kterému platí poslední stažená hodnota."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "Čas posledního odečtu"

    def __init__(
        self, coordinator: VSChrudimCoordinator, place_id: str
    ) -> None:
        super().__init__(coordinator, place_id)
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{place_id}_last_reading_time"
        )

    @property
    def native_value(self):
        place = self._place
        if not place:
            return None

        naive = place["time"]
        return naive.replace(tzinfo=PRAGUE_TZ)
