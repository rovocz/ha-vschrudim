"""Integrace VS Chrudim (vodoměr)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import VSChrudimCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

SERVICE_RESET_HISTORY = "reset_history"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VS Chrudim from a config entry."""
    coordinator = VSChrudimCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _async_register_services(hass)

    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the vschrudim.reset_history action (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_RESET_HISTORY):
        return

    async def _handle_reset_history(call: ServiceCall) -> None:
        raw_entity_id = call.data.get(ATTR_ENTITY_ID) or []

        # Selector s entitou (bez zaškrtnutí "vícenásobný výběr")
        # vrací jeden string, ne seznam - podporujeme obojí.
        entity_ids: list[str] = (
            [raw_entity_id]
            if isinstance(raw_entity_id, str)
            else list(raw_entity_id)
        )

        target_entries: list[ConfigEntry] = []

        if entity_ids:
            registry = er.async_get(hass)
            seen_entry_ids: set[str] = set()
            unresolved: list[str] = []

            for entity_id in entity_ids:
                entity_entry = registry.async_get(entity_id)

                if entity_entry is None or not entity_entry.config_entry_id:
                    unresolved.append(entity_id)
                    continue

                if entity_entry.config_entry_id in seen_entry_ids:
                    continue

                seen_entry_ids.add(entity_entry.config_entry_id)
                config_entry = hass.config_entries.async_get_entry(
                    entity_entry.config_entry_id
                )

                if config_entry is not None:
                    target_entries.append(config_entry)

            # Neplatné/neexistující entity NIKDY neprojdou potichu -
            # ať volání skončí zeleně jen tehdy, když se opravdu
            # něco stalo.
            if unresolved:
                raise HomeAssistantError(
                    "Tohle není platná entita VS Chrudim: "
                    + ", ".join(unresolved)
                )
        else:
            # Bez cíle resetujeme historii pro všechny
            # nakonfigurované účty VS Chrudim.
            target_entries = list(hass.config_entries.async_entries(DOMAIN))

        if not target_entries:
            raise HomeAssistantError(
                "Nenašel jsem žádnou nakonfigurovanou integraci "
                "VS Chrudim."
            )

        for config_entry in target_entries:
            coordinator: VSChrudimCoordinator | None = getattr(
                config_entry, "runtime_data", None
            )
            if coordinator is not None:
                await coordinator.async_reset_history()

    hass.services.async_register(
        DOMAIN, SERVICE_RESET_HISTORY, _handle_reset_history
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a VS Chrudim config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
