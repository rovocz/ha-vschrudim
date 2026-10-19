# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/), versioning follows [Semantic Versioning](https://semver.org/).

Entries below start at 0.5.0. Changes made before this point are available in the git history.

## [0.5.0]

### Added

- New action `vschrudim.reset_history` ("VS Chrudim: Reset water meter history"). Clears the imported long-term statistics for a VS Chrudim account (or, without a target, for every configured account) and forces a fresh import on the next update. Use this instead of manually editing statistics in **Developer Tools → Statistics**.
- The `ProfileData.aspx` page has a "last week / last month / last year" filter that controls how much history the CSV export covers (server default: last month). The integration now requests "last year" for the very first import (or right after `vschrudim.reset_history`) so the full available history is picked up, then switches to the cheaper "last month" for every regular poll afterwards - which is already more than enough to catch up on new hourly readings. Verified: 1345 rows back to the meter's installation date on the first fetch, instead of ~746 for the default month.

### Fixed

- The very first data point of a freshly imported history no longer produces a misleading spike at the start of `change`-type graphs. An earlier attempt inserted a single anchor point one hour before the real data - that only moved the spike to the anchor point itself once the graph's time range grew large enough to include it. Five anchor points (same value as the first real reading) are now inserted at 1 week, 1 month, 3 months, 6 months and 1 year before the real data starts, so any typical viewing range - a week up to a year - sees a flat baseline instead of a spike.
- `vschrudim.reset_history` no longer requires a target to be selected. It previously used a `target:` schema, which Home Assistant enforces as mandatory even when the action itself treats "no target" as a valid choice (reset everything). It now uses an optional `entity_id` field instead.
- `vschrudim.reset_history` failed with `RuntimeError: Detected unsafe call not in recorder thread`. `clear_statistics` must run on the recorder's own dedicated thread, not a generic executor thread - it is now scheduled through the recorder's task queue (`instance.queue_task(...)`), matching the pattern used internally by Home Assistant core for this exact function.
- Added `mean_type` to the statistics metadata (`StatisticMeanType.NONE`), fixing a deprecation warning (`doesn't specify mean_type when calling async_import_statistics`) that would otherwise stop the import from working in Home Assistant 2026.11.
