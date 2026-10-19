"""Konstanty pro integraci VS Chrudim."""

from zoneinfo import ZoneInfo

DOMAIN = "vschrudim"

DEFAULT_BASE_URL = "https://zakaznik.vschrudim.cz"

CONF_SCAN_INTERVAL_HOURS = "scan_interval_hours"

# Klíč v entry.data - jestli už proběhl prvotní import historie
# s plným ("Y" = poslední rok) rozsahem CSV. Po prvním úspěšném
# naplnění se běžné polly ptají jen na "M" (poslední měsíc), což
# stačí na dohnání nových hodinových záznamů a je to podstatně
# levnější než stahovat celý rok při každém pollu.
DATA_HISTORY_SEEDED = "history_seeded"

DEFAULT_SCAN_INTERVAL_HOURS = 1
MIN_SCAN_INTERVAL_HOURS = 1
MAX_SCAN_INTERVAL_HOURS = 24

# Web je český vodárenský podnik - časy v CSV bereme jako místní
# čas Evropa/Praha (server nikdy neposílá časovou zónu explicitně).
PRAGUE_TZ = ZoneInfo("Europe/Prague")
