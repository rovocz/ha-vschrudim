"""DataUpdateCoordinator pro VS Chrudim."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    clear_statistics,
    get_last_statistics,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_SCAN_INTERVAL_HOURS,
    DATA_HISTORY_SEEDED,
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


@dataclass(slots=True)
class _ClearStatisticsTask(RecorderTask):
    """Naplánuje clear_statistics na SKUTEČNÉM vlákně recorderu.

    `clear_statistics` interně kontroluje, že běží přesně na
    vyhrazeném vlákně recorderu (kvůli sdílené in-memory cache
    metadat statistik) - obyčejný `async_add_executor_job` běží
    v jiném vlákně a skončí "Detected unsafe call not in recorder
    thread". Zařazení přes `instance.queue_task(...)` je způsob,
    jaký pro tohle doporučují i vývojáři HA (viz diskuze k PR,
    které clear_statistics do jádra přidalo).
    """

    statistic_ids: list[str] = field(default_factory=list)

    def run(self, instance) -> None:  # noqa: ANN001
        clear_statistics(instance, self.statistic_ids)


class VSChrudimCoordinator(DataUpdateCoordinator[dict]):
    """Coordinator that logs in and fetches readings every scan interval.

    Historie z PŘED instalace integrace (to, co entita nemohla
    sama zachytit, protože ještě neexistovala) se importuje ZVLÁŠŤ
    jako "externí statistika" s vlastním ID (`vschrudim:<misto>`).
    Tohle vidí vestavěná karta "Graf statistik" (statistics-graph)
    a sekce Voda v Energy dashboardu.

    Aktuální hodnota senzoru "Stav vodoměru" má nastavený
    state_class, takže si pro ni Home Assistant statistiky
    generuje SÁM AUTOMATICKY z každé změny stavu - do toho
    záměrně nezasahujeme, aby to nekolidovalo s vlastním
    mechanismem recorderu.

    Reset historie (poškozená data, experimentování v Developer
    Tools apod.) se dělá přes akci `vschrudim.reset_history`
    (viz __init__.py) - volá `clear_statistics` a vynutí nový
    import, bez nutnosti cokoliv ručně mazat nebo měnit ID.
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

    @staticmethod
    def statistic_id_for_place(place_id: str) -> str:
        """Build the statistic_id for a place (shared by import + reset)."""
        safe_id = re.sub(r"[^a-z0-9_]", "_", place_id.lower())
        return f"{DOMAIN}:{safe_id}"

    async def async_reset_history(self) -> None:
        """Clear imported history and force a fresh import.

        Volá se z akce `vschrudim.reset_history`.
        """
        statistic_ids = [
            self.statistic_id_for_place(place_id)
            for place_id in (self.data or {})
        ]

        if not statistic_ids:
            raise HomeAssistantError(
                "Reset historie se nedá provést - integrace zatím "
                "nezná žádné odběrné místo (ještě neproběhl první "
                "úspěšný refresh)."
            )

        _LOGGER.info(
            "Mažu dovezenou historii pro: %s", ", ".join(statistic_ids)
        )

        try:
            get_instance(self.hass).queue_task(
                _ClearStatisticsTask(statistic_ids=statistic_ids)
            )
        except Exception as err:
            _LOGGER.exception(
                "Zařazení mazání statistik (%s) do fronty "
                "recorderu selhalo - viz traceback výše.",
                ", ".join(statistic_ids),
            )
            raise HomeAssistantError(
                f"Mazání historie selhalo: {err}"
            ) from err

        # Po resetu chceme, aby si příští poll znovu vyžádal celý
        # rok dat (ne jen poslední měsíc), ať se historie naplní
        # znovu celá - viz _async_update_data().
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, DATA_HISTORY_SEEDED: False},
        )

        # queue_task je "fire-and-forget" - mazání proběhne na
        # vlastním vlákně recorderu asynchronně, ne hned. Krátké
        # čekání dá recorderu šanci to stihnout, než spustíme
        # nový import (viz diskuze k PR, které clear_statistics
        # do HA přidalo - i oficiální WS API má stejné omezení).
        await asyncio.sleep(1)

        await self.async_request_refresh()

    async def _async_update_data(self) -> dict:
        # "Y" (poslední rok) jen dokud historie nebyla ještě
        # naplněná (první poll po instalaci, nebo po resetu) -
        # jinak stačí levné "M" (poslední měsíc), viz
        # get_latest_readings() a DATA_HISTORY_SEEDED výše.
        already_seeded = self.entry.data.get(DATA_HISTORY_SEEDED, False)
        graph_length = "M" if already_seeded else "Y"

        _LOGGER.info(
            "Stahuji data - already_seeded=%s -> graph_length=%s",
            already_seeded,
            graph_length,
        )

        try:
            readings = await self.hass.async_add_executor_job(
                self.client.get_latest_readings, graph_length
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

            if rows:
                times = [row["time"] for row in rows]
                _LOGGER.info(
                    "%s: staženo %s řádků, %s až %s",
                    place_id,
                    len(rows),
                    min(times),
                    max(times),
                )
            else:
                _LOGGER.info("%s: staženo 0 řádků", place_id)

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

        if not already_seeded:
            # Rok dat jsme si o něj řekli - příště stačí levnější
            # měsíc, ať se web nezatěžuje zbytečně velkým exportem
            # při každém běžném pollu.
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, DATA_HISTORY_SEEDED: True},
            )

        return readings

    async def _async_import_statistics(
        self, place_id: str, place: dict, rows: list[dict]
    ) -> None:
        if not rows:
            return

        # statistic_id smí obsahovat jen [a-z0-9_] za dvojtečkou -
        # "604202-7530" s pomlčkou HA odmítne ("Invalid
        # statistic_id"). Tohle je JEN pro statistiky - entity
        # (unique_id) necháváme beze změny, ať se nepřejmenují.
        statistic_id = self.statistic_id_for_place(place_id)

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

        # --------------------------------------------------------
        # --------------------------------------------------------
        # Nejstarší bod v historii nemá žádného předchůdce, se
        # kterým by šla spočítat "změna" - HA (na požádání karty,
        # ne my - "change" nikam sami nezapisujeme, počítá si ho
        # HA za běhu z "sum") ji spočítá jako celou hodnotu mínus
        # nic = falešný obří skok.
        #
        # Jeden kotevní bod těsně před daty tohle sice matematicky
        # opraví, ale JEN dokud se na graf díváš tak, že ten bod
        # není vidět. Jakmile si necháš zobrazit delší období
        # (třeba půl roku), kotevní bod se sám stane "prvním bodem
        # bez předchůdce" a skok se jen přesune na něj.
        #
        # Řešení: víc kotevních bodů v odstupňovaných vzdálenostech
        # před prvním opravdovým záznamem, všechny se STEJNOU
        # hodnotou (plochá "nulová" historie, ne skok) - ať uvidíš
        # klidnou přímku, ne špičku, i při pohledu na týden, měsíc,
        # čtvrtrok, půlrok i rok dozadu.
        # --------------------------------------------------------

        ANCHOR_OFFSETS_DAYS = [365, 180, 90, 30, 7]

        if last_stats_time is None and rows:
            rows_sorted = sorted(rows, key=lambda row: row["time"])
            earliest = rows_sorted[0]

            anchor_rows = [
                {
                    "meter": earliest["meter"],
                    "time": earliest["time"] - timedelta(days=offset),
                    "value": earliest["value"],
                }
                for offset in ANCHOR_OFFSETS_DAYS
            ]

            rows = [*anchor_rows, *rows_sorted]

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
            "mean_type": StatisticMeanType.NONE,
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
