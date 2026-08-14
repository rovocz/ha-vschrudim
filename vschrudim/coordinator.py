"""DataUpdateCoordinator pro VS Chrudim."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    PRAGUE_TZ,
)
from .vschrudim_client import (
    VSChrudimAuthError,
    VSChrudimClient,
    VSChrudimError,
)

_LOGGER = logging.getLogger(__name__)


class VSChrudimCoordinator(DataUpdateCoordinator[dict]):
    """Jednou za `scan_interval_hours` proběhne celý login+stažení.

    ARCHITEKTURA (po druhé iteraci):

    Senzor "Stav vodoměru" má nastavený state_class, takže si pro
    něj Home Assistant statistiky (i dlouhodobé) generuje SÁM
    AUTOMATICKY z každé změny stavu - o tohle se nestaráme a nic
    do toho neručíme, aby to nekolidovalo s vlastním mechanismem
    recorderu.

    Historii Z PŘED instalace integrace (to, co entita nemohla
    sama zachytit, protože ještě neexistovala) importujeme ZVLÁŠŤ
    jako "externí statistiku" s vlastním ID (`vschrudim:<misto>`).
    Tohle vidí vestavěná karta "Graf statistik" (statistics-graph)
    a sekce Voda v Energy dashboardu - ale NE karty jako
    apexcharts-card, které čekají skutečnou entitu (to je
    kompromis, na který jsme přišli experimentálně).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        hours = entry.options.get(
            CONF_SCAN_INTERVAL_HOURS,
            entry.data.get(
                CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
            ),
        )

        super().__init__(
            hass,
            _LOGGER,
            name="VS Chrudim",
            update_interval=timedelta(hours=hours),
        )

        self.entry = entry
        self.client = VSChrudimClient(
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            base_url=entry.data.get("base_url", DEFAULT_BASE_URL),
        )

    async def _async_update_data(self) -> dict:
        try:
            readings = await self.hass.async_add_executor_job(
                self.client.get_latest_readings
            )
        except VSChrudimAuthError as err:
            # Tohle spustí v HA "reauth" flow (uživatel bude
            # vyzván zadat heslo znovu) - typicky špatné heslo
            # po jeho změně na webu vodárny.
            raise ConfigEntryAuthFailed(str(err)) from err
        except VSChrudimError as err:
            raise UpdateFailed(str(err)) from err

        for place_id, place in readings.items():
            rows = place.pop("rows", [])

            # Import do statistik NIKDY nesmí shodit celou
            # aktualizaci - kdyby se recorder API mezi verzemi HA
            # změnilo, senzory (aktuální stav/čas) mají dál
            # fungovat i bez dlouhodobé historie.
            try:
                await self._async_import_statistics(place_id, place, rows)
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Import dlouhodobé historie pro %s selhal "
                    "(aktuální hodnota senzoru tím není dotčena).",
                    place_id,
                )

        return readings

    async def _async_import_statistics(
        self, place_id: str, place: dict, rows: list[dict]
    ) -> None:
        if not rows:
            return

        # DŮLEŽITÉ: statistic_id smí obsahovat jen [a-z0-9_] za
        # dvojtečkou - "604202-7530" s pomlčkou HA odmítne
        # ("Invalid statistic_id"). Tohle je JEN pro statistiky -
        # entity (unique_id) necháváme beze změny, ať se
        # nepřejmenují.
        safe_id = re.sub(r"[^a-z0-9_]", "_", place_id.lower())
        statistic_id = f"{DOMAIN}:{safe_id}"

        last_stats = await get_instance(
            self.hass
        ).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,
            {"sum"},
        )

        last_stats_time = None

        if last_stats and last_stats.get(statistic_id):
            last_stats_time = last_stats[statistic_id][0]["start"]

        new_rows = [
            row
            for row in rows
            if last_stats_time is None
            or row["time"].replace(tzinfo=PRAGUE_TZ).timestamp()
            > last_stats_time
        ]

        if not new_rows:
            return

        new_rows.sort(key=lambda row: row["time"])

        metadata = {
            "has_mean": False,
            "has_sum": True,
            "name": f"Vodoměr {place.get('adresa', place_id)}",
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
            # Vyžadováno novějšími verzemi HA (recorder od podzimu
            # 2025 bez tohohle pole import statistik odmítá).
            "unit_class": "volume",
        }

        statistics = [
            {
                "start": row["time"].replace(
                    tzinfo=PRAGUE_TZ,
                    minute=0,
                    second=0,
                    microsecond=0,
                ),
                "sum": row["value"],
                "state": row["value"],
            }
            for row in new_rows
        ]

        _LOGGER.debug(
            "Importuji %s nových bodů historie pro %s (statistic_id=%s)",
            len(statistics),
            place_id,
            statistic_id,
        )

        async_add_external_statistics(self.hass, metadata, statistics)
