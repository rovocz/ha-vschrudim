# VS Chrudim – Home Assistant

Home Assistant custom integration for the customer portal of **Vodárenská společnost Chrudim** (`zakaznik.vschrudim.cz`).

The integration logs into the customer portal, finds all active consumption places, downloads the meter-reading CSV and exposes the latest water-meter reading and its effective reading time in Home Assistant.

## Features

- setup through the Home Assistant UI (Config Flow)
- e-mail + password authentication
- automatic discovery of all active consumption places belonging to the account
- one Home Assistant device per active consumption place
- current water-meter reading in m³
- timestamp of the latest reading available from VS Chrudim
- historical hourly readings imported into Home Assistant long-term statistics
- catches up multiple readings when the portal uploads a batch of historical data
- configurable polling interval (1–24 hours, default 1 hour)
- reauthentication if the portal rejects the stored password
- reconfiguration without removing the integration
- Czech and English UI text

## Important: how historical data works

The VS Chrudim portal can sometimes make many hourly readings available at once. The integration therefore does **not** assume that every poll produces one new reading.

On every successful poll it downloads the available CSV and compares its timestamps with the historical statistics already imported for that consumption place. If the portal suddenly provides 24 new hourly readings, all 24 are imported with their original timestamps.

This means a graph can still show the real hourly consumption even when the portal itself was only contacted once during that period.

The current sensor state is the latest reading available from the portal. A separate timestamp sensor shows the time to which that reading applies.

## Installation with HACS – custom repository

Until this repository is accepted into the HACS default list, install it as a custom repository:

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu in the upper-right corner.
3. Select **Custom repositories**.
4. Enter this GitHub repository URL.
5. Select category **Integration**.
6. Add the repository and install **VS Chrudim (vodoměr)**.
7. Restart Home Assistant.
8. Go to **Settings → Devices & services → Add integration**.
9. Search for **VS Chrudim**.
10. Enter the same e-mail and password you use on the VS Chrudim customer portal.

## Manual installation

Copy the contents of `custom_components/vschrudim/` into:

```text
/config/custom_components/vschrudim/
```

Then restart Home Assistant and add the integration through **Settings → Devices & services → Add integration**.

## Entities

For every active consumption place the integration creates a Home Assistant device with:

- **Stav vodoměru** – current cumulative water-meter reading in m³
- **Čas posledního odečtu** – timestamp of the reading

The meter sensor uses `total_increasing`, so Home Assistant can calculate consumption statistics from it.

The device also exposes the consumption-place identifiers supplied by the portal, such as the registration number, technical number, address and meter identifier.

## Polling

The default polling interval is **1 hour**. It can be changed through the integration options to a value between 1 and 24 hours.

The polling interval only controls how often Home Assistant checks the portal. It does not assume that VS Chrudim publishes new data at the same frequency.

## Historical statistics

The integration imports older readings that existed before the integration was installed into Home Assistant long-term statistics. New readings are subsequently handled by the normal Home Assistant recorder/statistics mechanism through the meter entity.

The historical import uses a separate statistic ID in the form:

```text
vschrudim:<consumption_place_id>
```

The built-in Home Assistant statistics graph can use this history directly. Cards that require a normal entity history may only show the period for which the entity itself existed.

## Troubleshooting

Enable debug logging from **Settings → Devices & services → VS Chrudim → three dots → Enable debug logging**. Reproduce the problem and then download the captured log.

Alternatively:

```yaml
logger:
  logs:
    custom_components.vschrudim: debug
```

Never post your password or session cookies in an issue or log excerpt.

## Removing the integration

Go to **Settings → Devices & services**, open **VS Chrudim**, choose the three-dot menu and select **Delete**.

Removing the integration stops polling and removes its config entry. Home Assistant's recorder database and existing historical statistics are managed by Home Assistant itself.

## Development

The project contains automated Config Flow tests and GitHub Actions for HACS validation. The live portal is not accessed by the automated tests; network responses are mocked.

## Disclaimer

This is an independent community integration. It is not an official product of Vodárenská společnost Chrudim. The integration relies on the current behaviour of the customer portal and may stop working if the portal changes.

Do not publish your VS Chrudim credentials, session cookies, exported personal data or real consumption-place details in GitHub issues or pull requests.
