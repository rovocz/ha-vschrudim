"""Konstanty pro integraci VS Chrudim."""

from zoneinfo import ZoneInfo

DOMAIN = "vschrudim"

DEFAULT_BASE_URL = "https://zakaznik.vschrudim.cz"

CONF_SCAN_INTERVAL_HOURS = "scan_interval_hours"

DEFAULT_SCAN_INTERVAL_HOURS = 1
MIN_SCAN_INTERVAL_HOURS = 1
MAX_SCAN_INTERVAL_HOURS = 24

# Web je český vodárenský podnik - časy v CSV bereme jako místní
# čas Evropa/Praha (server nikdy neposílá časovou zónu explicitně).
PRAGUE_TZ = ZoneInfo("Europe/Prague")
