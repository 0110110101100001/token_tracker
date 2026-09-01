# Changelog

What changed in each release. Versions before 2.0 are in their tag messages:
`git tag -n50`.

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

### Changed

- `claude-sonnet-5` stays at $2.00 / $10.00 in `pricing.json`. The introductory
  rate became the standard price and the increase due on 2026-09-01 was
  cancelled.
