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
from .profile_navigation import (
    context_status,
    form_payload,
    login_page,
    navigation_candidate,
    profile_ready,
    safe_url,
)

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
        self._places_by_index: dict[int, dict] = {}
        self._selected_place: dict | None = None
        self._selected_response = None
        self._context_verified = False

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

    def _checked_response(self, response):
        """Ověřit HTTP, původ a rozpoznat vypršení session bez výpisu obsahu."""
        response.raise_for_status()
        try:
            safe_url(self.base_url, self.base_url, response.url)
        except ValueError as err:
            raise VSChrudimError("Portál přesměroval na nepovolený server.") from err
        if login_page(response.text, response.url):
            raise VSChrudimAuthError("Přihlášená relace portálu vypršela.")
        return response

    def _safe_request(self, method: str, url: str, **kwargs):
        """Před každým přesměrováním ověřit stejný portál a omezit jejich počet.

        POST po běžném 301/302/303 přechází na GET jako v requests. Při 307/308
        zůstane metoda i tělo; obě se odešlou jen původnímu serveru. Při chybě
        se nezaznamenává URL s tokeny ani tělo formuláře.
        """
        try:
            current = safe_url(self.base_url, self.base_url + "/", url)
            for _ in range(6):
                response = getattr(self.s, method.lower())(
                    current, allow_redirects=False, **kwargs
                )
                response.raise_for_status()
                safe_url(self.base_url, current, response.url)
                if response.status_code not in (301, 302, 303, 307, 308):
                    return self._checked_response(response)
                location = response.headers.get("Location")
                if not location:
                    raise VSChrudimError("Přesměrování portálu nemá cíl.")
                current = safe_url(self.base_url, response.url, location)
                if response.status_code == 303 or (
                    response.status_code in (301, 302) and method.upper() == "POST"
                ):
                    method = "GET"
                    kwargs.pop("data", None)
            raise VSChrudimError("Portál překročil limit přesměrování.")
        except ValueError as err:
            raise VSChrudimError("Portál přesměroval na nepovolený server.") from err

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
        r.raise_for_status()

        # Server nám u špatných údajů natvrdo řekne "Login is not
        # valid" - to je jistější signál než cokoliv jiného.
        if re.search(r"login is not valid", r.text, re.IGNORECASE):
            raise VSChrudimAuthError(
                "Server odmítl přihlašovací údaje "
                "('Login is not valid')."
            )

        test_url = self.base_url + "/ConsumptionPlaceList.aspx"
        test = self._safe_request("GET", test_url, timeout=REQUEST_TIMEOUT)

        if "gvConsumptionPlaces" not in test.text:
            raise VSChrudimError(
                "Server nezpřístupnil ověřitelný seznam odběrných míst."
            )

    # ------------------------------------------------------------
    # SEZNAM ODBĚRNÝCH MÍST
    #
    # Sloupec "Účetní verze smlouvy*" obsahuje buď:
    #   "od 01.10.2020"                -> AKTIVNÍ (bez konce)
    #   "od 09.03.2020 do 30.09.2020"  -> UKONČENÉ (má "do")
    # ------------------------------------------------------------

    def list_consumption_places(self) -> list[dict]:
        self._places_by_index = {}
        self._selected_place = None
        self._selected_response = None
        self._context_verified = False
        url = self.base_url + "/ConsumptionPlaceList.aspx"

        r = self._safe_request("GET", url, timeout=REQUEST_TIMEOUT)

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

        self._places_by_index = {place["index"]: place for place in places}
        return places

    # ------------------------------------------------------------
    # VÝBĚR ODBĚRNÉHO MÍSTA (idx = "index" z list_consumption_places)
    # ------------------------------------------------------------

    def select_consumption_place(self, idx: int) -> None:
        # Zvolený index musí stále označovat stejné místo ve čerstvém gridu.
        expected = self._places_by_index.get(idx)
        self._selected_place = None
        self._selected_response = None
        self._context_verified = False
        if expected is None:
            raise VSChrudimError("Odběrné místo není ověřeno v aktuálním seznamu.")
        url = self.base_url + "/ConsumptionPlaceList.aspx"

        r0 = self._safe_request("GET", url, timeout=REQUEST_TIMEOUT)
        html = r0.text

        if "gvConsumptionPlaces" not in html:
            raise VSChrudimError(
                "Na ConsumptionPlaceList.aspx chybí "
                "gvConsumptionPlaces - session zřejmě vypadla."
            )

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id=re.compile(r"gvConsumptionPlaces$"))
        rows = []
        if table:
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if not row.find("th") and len(cells) >= 6:
                    rows.append(cells)
        def normalize(value: str) -> str:
            return "".join(value.split()).casefold()

        if idx >= len(rows) or any(
            normalize(rows[idx][position].get_text(strip=True))
            != normalize(expected[key])
            for position, key in ((1, "evidencni_cislo"), (2, "technicke_cislo"))
        ):
            raise VSChrudimError("Pořadí nebo identita odběrného místa se změnila.")

        form = table.find_parent("form")
        if form is None:
            raise VSChrudimError("Seznam míst nemá platný formulář.")
        try:
            destination = safe_url(self.base_url, r0.url, form.get("action") or r0.url)
        except ValueError as err:
            raise VSChrudimError("Nepovolená adresa formuláře míst.") from err
        fields = form_payload(form)

        event_target = (
            "ctl00$ctl00$ContentPlaceHolder1Common$"
            "ContentPlaceHolder1$gvConsumptionPlaces"
        )
        event_argument = f"Show${idx}"
        toolkit_target = "ctl00$ctl00$FormPanel|" + event_target

        overrides = {
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": event_argument,
            "__ASYNCPOST": "true",
            "ctl00$ctl00$ToolkitScriptManager1": toolkit_target,
        }
        defaults = {
            "ctl00$ctl00$captchaToken": "",
            "ctl00$ctl00$crs": "",
            "ctl00$ctl00$ContentPlaceHolder1Common$ContentPlaceHolder1$PageName":
                "ConsumptionPlaceList.aspx",
        }
        present = {key for key, _ in fields}
        data = [(key, value) for key, value in fields if key not in overrides]
        data.extend(
            (key, value) for key, value in defaults.items() if key not in present
        )
        data.extend(overrides.items())

        r = self.s.post(
            destination,
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
        self._checked_response(r)

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
            try:
                full_url = safe_url(self.base_url, r.url, redirect_url)
            except ValueError as err:
                raise VSChrudimError("Nepovolené přesměrování výběru místa.") from err
            main = self._safe_request("GET", full_url, timeout=REQUEST_TIMEOUT)
        else:
            # Známá stránka použitá stávajícím klientem; kontext se ověřuje níže.
            main = self._safe_request(
                "GET",
                self.base_url + "/UserData/MainInfo.aspx",
                timeout=REQUEST_TIMEOUT,
            )
        self._checked_response(main)
        status = context_status(main.text, expected)
        if status is False:
            raise VSChrudimError("Portál potvrdil jiné odběrné místo.")
        self._selected_place = expected
        self._selected_response = main
        self._context_verified = status is True

    # ------------------------------------------------------------
    # KROK 3: "NAMĚŘENÉ STAVY" (MainInfo.aspx -> ProfileData.aspx)
    #
    # Odkaz "Naměřené stavy" je __doPostBack, ne obyčejný <a href>.
    # Pole mají na téhle stránce TROJITÝ prefix ctl00$ctl00$ctl00$,
    # proto se vždy čtou čerstvě z aktuální stránky.
    # ------------------------------------------------------------

    def open_profile_data(self) -> tuple[str, str]:
        """Otevřít profil z čerstvě vybraného a ověřeného kontextu místa."""
        if self._selected_place is None or self._selected_response is None:
            raise VSChrudimError("Před otevřením odečtů nebylo vybráno místo.")
        r0 = self._checked_response(self._selected_response)

        def finish(response, *, require_filter: bool):
            self._checked_response(response)
            status = context_status(response.text, self._selected_place)
            if status is False:
                raise VSChrudimError("Stránka odečtů patří jinému místu.")
            if status is True:
                self._context_verified = True
            if not self._context_verified:
                raise VSChrudimError("Portál nepotvrdil identitu vybraného místa.")
            if not profile_ready(response.text, require_filter=require_filter):
                raise VSChrudimError(
                    "Stránka odečtů nemá ověřitelný export nebo filtr."
                )
            return response.text, response.url

        # Server může po volbě místa už vrátit finální stránku.
        if profile_ready(r0.text, require_filter=True):
            return finish(r0, require_filter=True)

        candidate = navigation_candidate(r0.text)
        if candidate is None:
            # Přímá známá cesta je přípustná jen po potvrzení aktuálního místa.
            if not self._context_verified:
                raise VSChrudimError("Odkaz odečtů ani kontext místa nejsou dostupné.")
            target = safe_url(self.base_url, r0.url, "/UserData/ProfileData.aspx")
            r = self._safe_request("GET", target, timeout=REQUEST_TIMEOUT)
            return finish(r, require_filter=True)

        mode, node, target, argument = candidate
        if mode == "link":
            try:
                destination = safe_url(self.base_url, r0.url, target)
            except ValueError as err:
                raise VSChrudimError("Nepovolený odkaz odečtů.") from err
            r = self._safe_request("GET", destination, timeout=REQUEST_TIMEOUT)
        else:
            form = node.find_parent("form")
            if form is None:
                raise VSChrudimError("Ovládání odečtů nemá platný formulář.")
            try:
                destination = safe_url(
                    self.base_url, r0.url, form.get("action") or r0.url
                )
            except ValueError as err:
                raise VSChrudimError("Nepovolená adresa formuláře.") from err
            payload = [
                (key, value)
                for key, value in form_payload(form)
                if key not in ("__EVENTTARGET", "__EVENTARGUMENT")
            ]
            if mode == "postback":
                payload.extend(
                    (("__EVENTTARGET", target), ("__EVENTARGUMENT", argument))
                )
            else:
                payload.extend(
                    (("__EVENTTARGET", ""), ("__EVENTARGUMENT", ""), (target, argument))
                )
            r = self._safe_request(
                "POST", destination, data=payload,
                headers={"Origin": self.base_url, "Referer": r0.url},
                timeout=REQUEST_TIMEOUT,
            )
        self._checked_response(r)

        if r.text.startswith("1|#|") and "pageRedirect" in r.text:
            parts = r.text.split("|")
            try:
                pos = parts.index("pageRedirect")
                redirect_url = unquote(parts[pos + 2])
            except (ValueError, IndexError):
                redirect_url = None

            if redirect_url:
                try:
                    full_url = safe_url(self.base_url, r.url, redirect_url)
                except ValueError as err:
                    raise VSChrudimError("Nepovolené přesměrování odečtů.") from err
                r = self._safe_request("GET", full_url, timeout=REQUEST_TIMEOUT)

        return finish(r, require_filter=False)

    # ------------------------------------------------------------
    # FILTR "POSLEDNÍ TÝDEN / MĚSÍC / ROK" (edGraphLength)
    #
    # ProfileData.aspx má nahoře filtr, který určuje, jak dlouhé
    # období export CSV pokrývá - server default je "M" (poslední
    # měsíc). Přepnutím na "Y" (poslední rok) se stažené CSV
    # rozšíří na celou dostupnou historii (ověřeno: 1345 řádků
    # od data instalace vodoměru místo 746 za poslední měsíc).
    #
    # Ověřeno jako normální (ne-AJAX) postback - stejný princip
    # jako "Naměřené stavy" v open_profile_data().
    # ------------------------------------------------------------

    def _select_graph_length(
        self, profile_html: str, profile_url: str, length_code: str = "Y"
    ) -> tuple[str, str]:
        soup = BeautifulSoup(profile_html, "html.parser")

        select_length = soup.find(
            "select", id=re.compile(r"edGraphLength$")
        )

        if select_length is None:
            # Filtr nenalezen - nejde o fatální chybu, prostě
            # zůstaneme na serverovém defaultu (poslední měsíc).
            return profile_html, profile_url

        length_name = select_length.get("name")

        if not length_name:
            return profile_html, profile_url

        extra_fields = {length_name: length_code}

        # Druhý select (odběrné místo v grafu) musí jít v POSTu
        # taky, se svou AKTUÁLNÍ hodnotou - jinak by se "vynuloval".
        select_point = soup.find(
            "select", id=re.compile(r"edMeteringPoint$")
        )

        if select_point is not None:
            point_name = select_point.get("name")
            current_option = select_point.find("option", selected=True)

            if point_name and current_option is not None:
                extra_fields[point_name] = current_option.get("value", "")

        post_data = self._hidden_fields(profile_html)
        post_data.update(extra_fields)
        post_data["__EVENTTARGET"] = length_name
        post_data["__EVENTARGUMENT"] = ""

        r = self.s.post(
            profile_url,
            data=post_data,
            headers={
                "Content-Type":
                    "application/x-www-form-urlencoded; charset=utf-8",
                "Origin": self.base_url,
                "Referer": profile_url,
            },
            allow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )

        if "DocumentShow.aspx" not in r.text:
            # Nevyšlo - raději pokračujeme s původní stránkou
            # (měsíční export) než abychom celé stažení shodili.
            return profile_html, profile_url

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

    def get_latest_readings(
        self, graph_length: str = "M"
    ) -> dict[str, dict]:
        """Přihlásí se a stáhne aktuální data pro všechna aktivní místa.

        graph_length: "W"/"M"/"Y" - jaký rozsah CSV požádat od
        serveru (viz _select_graph_length). Výchozí "M" (poslední
        měsíc) je levné a pro běžný pravidelný poll bohatě stačí -
        "Y" (poslední rok) se má žádat jen při prvotním naplnění
        historie nebo po resetu, ne při každém běžném pollu.
        """
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

            # "M" (poslední měsíc) je serverový výchozí stav rovnou
            # po otevření ProfileData.aspx (ověřeno) - přepínat
            # filtr má smysl jen pro "W"/"Y", ať si nepřidáváme
            # zbytečný request navíc při každém běžném pollu.
            if graph_length != "M":
                profile_html, profile_url = self._select_graph_length(
                    profile_html, profile_url, graph_length
                )

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

        # Původní koordinátor by jinak prázdný výsledek označil za úspěšné
        # doplnění historie. Místa s platnými odečty zachovávají původní tok.
        if not result:
            raise VSChrudimError("Portál nevrátil použitelné odečty žádného místa.")
        return result
