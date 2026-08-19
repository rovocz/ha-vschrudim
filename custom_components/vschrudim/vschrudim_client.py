"""
Klient pro zakaznik.vschrudim.cz.

Tohle je přenesená a otestovaná logika ze samostatného ladicího
skriptu - stejný login flow, stejný krok 3 ("Naměřené stavy") a
stejná autodetekce CZ/EN formátu CSV. Rozdíl je jen v tom, že
místo print() používáme logging a místo ukončení skriptu vyhazujeme
výjimky, aby to šlo zavolat z Home Assistant.

Běží synchronně (přes `requests`) - Home Assistant to musí volat
přes `hass.async_add_executor_job(...)`, nikdy přímo v event loopu.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from .const import DEFAULT_BASE_URL

REQUEST_TIMEOUT = 30

_LOGGER = logging.getLogger(__name__)


class VSChrudimError(Exception):
    """Obecná chyba komunikace s zakaznik.vschrudim.cz."""


class VSChrudimAuthError(VSChrudimError):
    """Přihlašovací údaje byly serverem odmítnuty."""


class VSChrudimClient:
    """Synchronní klient - jedna instance = jedna přihlašovací session."""

    def __init__(
        self,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self.username = username
        self.password = password
        self.base_url = base_url

        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                    "Gecko/20100101 Firefox/128.0"
                ),
                "Accept-Language": "en-US,en;q=0.5",
            }
        )

    # ------------------------------------------------------------
    # POMOCNÉ
    # ------------------------------------------------------------

    @staticmethod
    def _hidden_fields(html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        result: dict[str, str] = {}
        for tag in soup.find_all("input", type="hidden"):
            name = tag.get("name")
            if name:
                result[name] = tag.get("value", "")
        return result

    # ------------------------------------------------------------
    # LOGIN
    # ------------------------------------------------------------

    def login(self) -> None:
        login_page_url = self.base_url + "/Default.aspx"
        login_post_url = self.base_url + "/"

        r0 = self.s.get(login_page_url, timeout=REQUEST_TIMEOUT)
        r0.raise_for_status()

        data = self._hidden_fields(r0.text)

        data.update(
            {
                "ctl00$ctl00$ToolkitScriptManager1": (
                    "ctl00$ctl00$lvLoginForm$LoginDialog1"
                    "$updatePanellAddress|"
                    "ctl00$ctl00$lvLoginForm$LoginDialog1"
                    "$btnLogin"
                ),
                "ctl00$ctl00$captchaToken": "",
                "ctl00$ctl00$crs": "",
                "ctl00$ctl00$lvLoginForm$LoginDialog1$edEmail":
                    self.username,
                "ctl00$ctl00$lvLoginForm$LoginDialog1$edPassword":
                    self.password,
                "ctl00$ctl00$ContentPlaceHolder1Common"
                "$ContentPlaceHolder1$PageName": "Default.aspx",
                "__ASYNCPOST": "true",
                "ctl00$ctl00$lvLoginForm$LoginDialog1$btnLogin":
                    "Login",
            }
        )

        r = self.s.post(
            login_post_url,
            data=data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-MicrosoftAjax": "Delta=true",
                "Cache-Control": "no-cache",
                "Content-Type":
                    "application/x-www-form-urlencoded; charset=utf-8",
                "Origin": self.base_url,
                "Referer": login_page_url,
            },
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )

        # Server nám u špatných údajů natvrdo řekne "Login is not
        # valid" - to je jistější signál než cokoliv jiného.
        if re.search(r"login is not valid", r.text, re.IGNORECASE):
            raise VSChrudimAuthError(
                "Server odmítl přihlašovací údaje "
                "('Login is not valid')."
            )

        test_url = self.base_url + "/ConsumptionPlaceList.aspx"
        test = self.s.get(test_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)

        if "gvConsumptionPlaces" not in test.text:
            raise VSChrudimAuthError(
                "Přihlášení se nepodařilo ověřit - server "
                "nezpřístupnil stránku s odběrnými místy."
            )

    # ------------------------------------------------------------
    # SEZNAM ODBĚRNÝCH MÍST
    #
    # Sloupec "Účetní verze smlouvy*" obsahuje buď:
    #   "od 01.10.2020"                -> AKTIVNÍ (bez konce)
    #   "od 09.03.2020 do 30.09.2020"  -> UKONČENÉ (má "do")
    # ------------------------------------------------------------

    def list_consumption_places(self) -> list[dict]:
        url = self.base_url + "/ConsumptionPlaceList.aspx"

        r = self.s.get(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        table = soup.find(
            "table",
            id=re.compile(r"gvConsumptionPlaces$"),
        )

        if table is None:
            raise VSChrudimError(
                "Nenašel jsem tabulku gvConsumptionPlaces - "
                "nejspíš nejsme přihlášení."
            )

        places = []
        idx = 0

        for row in table.find_all("tr"):
            if row.find("th"):
                continue

            cells = row.find_all("td")
            if len(cells) < 6:
                continue

            contract_text = cells[5].get_text(strip=True)
            is_active = (
                "od " in contract_text
                and " do " not in contract_text
            )

            places.append(
                {
                    "index": idx,
                    "evidencni_cislo": cells[1].get_text(strip=True),
                    "technicke_cislo": cells[2].get_text(strip=True),
                    "adresa": cells[3].get_text(strip=True),
                    "smlouva": cells[4].get_text(strip=True),
                    "contract_period": contract_text,
                    "active": is_active,
                }
            )
            idx += 1

        return places

    # ------------------------------------------------------------
    # VÝBĚR ODBĚRNÉHO MÍSTA (idx = "index" z list_consumption_places)
    # ------------------------------------------------------------

    def select_consumption_place(self, idx: int) -> None:
        url = self.base_url + "/ConsumptionPlaceList.aspx"

        r0 = self.s.get(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        r0.raise_for_status()
        html = r0.text

        if "gvConsumptionPlaces" not in html:
            raise VSChrudimError(
                "Na ConsumptionPlaceList.aspx chybí "
                "gvConsumptionPlaces - session zřejmě vypadla."
            )

        data = self._hidden_fields(html)

        event_target = (
            "ctl00$ctl00$ContentPlaceHolder1Common$"
            "ContentPlaceHolder1$gvConsumptionPlaces"
        )
        event_argument = f"Show${idx}"
        toolkit_target = "ctl00$ctl00$FormPanel|" + event_target

        data.update(
            {
                "__EVENTTARGET": event_target,
                "__EVENTARGUMENT": event_argument,
                "ctl00$ctl00$captchaToken": "",
                "ctl00$ctl00$crs": "",
                "ctl00$ctl00$ContentPlaceHolder1Common"
                "$ContentPlaceHolder1$PageName":
                    "ConsumptionPlaceList.aspx",
                "__ASYNCPOST": "true",
                "ctl00$ctl00$ToolkitScriptManager1": toolkit_target,
            }
        )

        r = self.s.post(
            url,
            data=data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-MicrosoftAjax": "Delta=true",
                "Cache-Control": "no-cache",
                "Content-Type":
                    "application/x-www-form-urlencoded; "
                    "charset=utf-8",
                "Origin": self.base_url,
                "Referer": url,
            },
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )

        redirect_url = None

        if "pageRedirect" in r.text:
            parts = r.text.split("|")
            try:
                pos = parts.index("pageRedirect")
                if pos + 2 < len(parts):
                    redirect_url = unquote(parts[pos + 2])
            except ValueError:
                redirect_url = None

        if redirect_url:
            full_url = (
                self.base_url + redirect_url
                if redirect_url.startswith("/")
                else self.base_url + "/" + redirect_url
            )
            main = self.s.get(full_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)

            if main.status_code == 200 and "MainInfo.aspx" in main.url:
                return

        # Fallback - zkusíme MainInfo přímo.
        main = self.s.get(
            self.base_url + "/UserData/MainInfo.aspx",
            allow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )

        if "MainInfo.aspx" not in main.url:
            raise VSChrudimError(
                f"Výběr odběrného místa (index {idx}) se nepodařil."
            )

    # ------------------------------------------------------------
    # KROK 3: "NAMĚŘENÉ STAVY" (MainInfo.aspx -> ProfileData.aspx)
    #
    # Odkaz "Naměřené stavy" je __doPostBack, ne obyčejný <a href>.
    # Pole mají na téhle stránce TROJITÝ prefix ctl00$ctl00$ctl00$,
    # proto se vždy čtou čerstvě z aktuální stránky.
    # ------------------------------------------------------------

    def open_profile_data(self) -> tuple[str, str]:
        main_url = self.base_url + "/UserData/MainInfo.aspx"

        r0 = self.s.get(main_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        r0.raise_for_status()
        html = r0.text

        soup = BeautifulSoup(html, "html.parser")
        link = soup.find(
            "a", id=re.compile(r"MainMenu1_btnProfileData$")
        )

        if link is None:
            raise VSChrudimError(
                "Na MainInfo.aspx se nepodařilo najít odkaz "
                "'Naměřené stavy' - web nejspíš změnil strukturu."
            )

        href = link.get("href", "")
        m = re.search(
            r"__doPostBack\('([^']*)'\s*,\s*'([^']*)'\)", href
        )

        if not m:
            raise VSChrudimError(
                f"Odkaz na Naměřené stavy má neočekávaný formát: "
                f"{href!r}"
            )

        event_target, event_argument = m.group(1), m.group(2)

        post_data = self._hidden_fields(html)
        post_data.update(
            {
                "__EVENTTARGET": event_target,
                "__EVENTARGUMENT": event_argument,
            }
        )

        r = self.s.post(
            main_url,
            data=post_data,
            headers={
                "Content-Type":
                    "application/x-www-form-urlencoded; "
                    "charset=utf-8",
                "Origin": self.base_url,
                "Referer": main_url,
            },
            allow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )

        if r.text.startswith("1|#|") and "pageRedirect" in r.text:
            parts = r.text.split("|")
            try:
                pos = parts.index("pageRedirect")
                redirect_url = unquote(parts[pos + 2])
            except (ValueError, IndexError):
                redirect_url = None

            if redirect_url:
                full_url = urljoin(r.url, redirect_url)
                r = self.s.get(full_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)

        if "DocumentShow.aspx" not in r.text:
            raise VSChrudimError(
                "Po kliknutí na 'Naměřené stavy' nevidím odkaz "
                "na export CSV."
            )

        return r.text, r.url

    # ------------------------------------------------------------
    # HLEDÁNÍ EXPORT ODKAZU (DATA_ID SE MĚNÍ PŘI KAŽDÉM NAČTENÍ!)
    # ------------------------------------------------------------

    @staticmethod
    def _find_csv_export_href(html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")

        link = soup.find(
            "a", id=re.compile(r"UserDataContentPlaceHolder_LinkA$")
        )
        if link is None:
            link = soup.find(
                "a", href=re.compile(r"DocumentShow\.aspx\?DATA_ID=")
            )

        return link.get("href") if link else None

    # ------------------------------------------------------------
    # STAŽENÍ CSV (jako text, ne do souboru)
    # ------------------------------------------------------------

    def _download_csv_text(self, href: str, profile_url: str) -> str:
        csv_url = urljoin(profile_url, href)

        r = self.s.get(csv_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()

        if r.url.rstrip("/") == (
            self.base_url + "/Default.aspx"
        ).rstrip("/"):
            raise VSChrudimError(
                "Server přesměroval stažení CSV na Default.aspx - "
                "session zřejmě vypadla."
            )

        for encoding in ("utf-8-sig", "cp1250"):
            try:
                return r.content.decode(encoding)
            except UnicodeDecodeError:
                continue

        raise VSChrudimError(
            "Nepodařilo se dekódovat stažené CSV "
            "(zkoušeno utf-8-sig, cp1250)."
        )

    # ------------------------------------------------------------
    # PARSOVÁNÍ CSV
    #
    # Web umí exportovat česky i anglicky a formát se mezi verzemi
    # liší - autodetekce podle oddělovače v datu (tečka/lomítko).
    # ------------------------------------------------------------

    @staticmethod
    def parse_csv_text(text: str) -> list[dict]:
        import csv as csv_module
        import io

        def parse_datetime(value: str) -> datetime:
            value = value.strip()
            if "." in value:
                formats = ["%d.%m.%Y %H:%M:%S"]
            elif "/" in value:
                formats = [
                    "%d/%m/%Y %H:%M:%S",
                    "%m/%d/%Y %H:%M:%S",
                ]
            else:
                formats = ["%Y-%m-%d %H:%M:%S"]

            for fmt in formats:
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue

            raise VSChrudimError(f"Neznámý formát data/času: {value!r}")

        def parse_value(value: str) -> float:
            value = value.strip()
            if "," in value:
                value = value.replace(",", ".")
            return float(value)

        rows = []
        reader = csv_module.DictReader(io.StringIO(text), delimiter=";")

        for row in reader:
            if not row.get("CAS"):
                continue

            rows.append(
                {
                    "meter": (row.get("MERIDLO") or "").strip(),
                    "time": parse_datetime(row["CAS"]),
                    "value": parse_value(row["STAV"]),
                }
            )

        return rows

    # ------------------------------------------------------------
    # VYSOKOÚROVŇOVÉ VOLÁNÍ PRO KOORDINÁTOR
    #
    # Přihlásí se, projde všechna AKTIVNÍ odběrná místa a pro
    # každé vrátí poslední (nejnovější) odečet.
    # ------------------------------------------------------------

    def get_latest_readings(self) -> dict[str, dict]:
        self.login()

        places = self.list_consumption_places()
        active = [p for p in places if p["active"]]

        if not active:
            raise VSChrudimError(
                "Nenašel jsem žádné aktivní odběrné místo "
                "(všechny mají v datech vyplněné 'do')."
            )

        result: dict[str, dict] = {}

        for place in active:
            self.select_consumption_place(place["index"])

            profile_html, profile_url = self.open_profile_data()

            href = self._find_csv_export_href(profile_html)
            if not href:
                _LOGGER.warning(
                    "Odběrné místo %s: export odkaz nenalezen, "
                    "přeskakuji.",
                    place["technicke_cislo"],
                )
                continue

            csv_text = self._download_csv_text(href, profile_url)
            rows = self.parse_csv_text(csv_text)

            if not rows:
                _LOGGER.warning(
                    "Odběrné místo %s: CSV neobsahuje žádné "
                    "řádky, přeskakuji.",
                    place["technicke_cislo"],
                )
                continue

            last = max(rows, key=lambda row: row["time"])

            key = (
                place["technicke_cislo"].replace(" ", "")
                or place["evidencni_cislo"]
            )

            result[key] = {
                "value": last["value"],
                "time": last["time"],
                "meridlo": last["meter"],
                "adresa": place["adresa"],
                "evidencni_cislo": place["evidencni_cislo"],
                "technicke_cislo": place["technicke_cislo"],
                # Celá stažená historie (typicky několik měsíců
                # zpět) - používá se pro doplnění dlouhodobých
                # statistik/grafů v Home Assistant, ne jen pro
                # aktuální hodnotu senzoru.
                "rows": rows,
            }

        return result
