# Changelog

What changed in each release. Versions before 2.0 are in their tag messages:
`git tag -n50`.

## Unreleased

### Added

- A header at the top of the panel: the Claude mark, in Anthropic's terracotta,
  and the word `claude` beside it, as the codex panel carries the OpenAI mark
  and `codex`. Below the header the two are the same dark table, so this is
  what tells them apart on one screen. The mark is SVG rendered by librsvg and
  scales with the rest of the panel. See [PANEL.md](PANEL.md).

## 2.0

### Added

- Patrik mode. A new turn throws money glyphs out of the panel and gives it a
  flinch. Right click the panel to switch it on; saved as `patrik_mode`.
  See [Patrik mode](PANEL.md#patrik-mode).
- Sound. A new turn plays one of four recordings, picked by what that turn
  cost. Right click to switch it on; saved as `sound`, independent of Patrik
  mode. See [Sound](PANEL.md#sound).
- A third limit row, for the account's weekly cap on one model. It names itself
  after whichever model the server reports, and shows a dash where there is no
  such cap.

### Fixed

- Glyphs near a screen edge started up to a full margin away from the panel,
  because the window manager had moved the overlay out of the position it asked
  for.
- Two panel tests carried a literal reset date and failed once it passed.
- `claude-fable-5-1` was missing from `pricing.json`, so every turn on it was
  excluded from the totals: the turn and session rows read zero and the day
  and week rows counted only older models. The entry is in, with Fable 5.1's
  own cache-read rate.
- `claude-opus-5-5` was missing from `pricing.json` in the same way, with the
  same result. Its entry is in at $4 / $20 per million, with its own
  `cache_read` rate of $0.20: a twentieth of input, not the usual tenth.

### Changed

- `claude-sonnet-5` stays at $2.00 / $10.00 in `pricing.json`. The introductory
  rate became the standard price and the increase due on 2026-09-01 was
  cancelled.
- A `pricing.json` entry may carry a `cache_read` rate of its own; without one
  the read stays a tenth of the input rate. See
  [Pricing](METERING.md#pricing).
