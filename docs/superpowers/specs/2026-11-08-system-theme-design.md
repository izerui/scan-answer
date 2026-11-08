# System Appearance for macOS and Windows

The user authorized light/dark adaptation, including Windows. Keep the existing
Qt Widgets layout, controls, document data, and recognition workflow.

## Design

Follow `QApplication.styleHints().colorScheme()` at startup and listen to
`colorSchemeChanged`. Apply changes on the GUI event loop after Qt's notification.
Use one complete semantic palette and stylesheet per scheme. Do not change the
OS appearance or force a global preference. Unknown scheme falls back to the
native style's standard palette. Keep the existing native widget style.

This uses Qt's supported platform integration instead of separate registry and
macOS preference polling. A startup-only palette would leave existing windows
and open dialogs inconsistent, so refresh their presentation in place.

Light retains the existing neutral gray/white scheme. Dark uses neutral charcoal
surfaces and light text; error, warning, success and focus colors retain their
meaning with suitable contrast. Selected, disabled, read-only, hover and focus
states must remain legible in standard controls, menus, tabs, tables, trees,
plain-text JSON and dialogs.

Document images, template canvases' paper and region overlays are evidence.
Do not invert, tint, rewrite, reload or recolor their pixels/annotation palette.
Only surrounding canvas backgrounds adapt. Preserve document edits, undo stack,
current page/student, selection, scroll and zoom during a switch.

## Implementation Contracts

`ui/theme.py` owns:

- `apply_theme(app) -> ThemeManager`, idempotent and application-owned.
- `ThemeManager.apply_scheme(Qt.ColorScheme)` and `scheme`; system notifications
  use this same path. Unknown resolves from the native style, not our last palette.
- Existing color constants update for the current scheme; `REGION` stays fixed
  for paper annotations. Add `PAPER_ERR` for paper's fixed error overlay.
- Tokens include existing CHASSIS/RAIL/PANEL/WORK/LINE/LINE2/INK/INK2/INK3,
  HEADER/CANVAS/NOTICE/SELECTION/ERR_BG/WARN_BG/ERR/WARN/ACCENT/SUCCESS,
  DISABLED/DISABLED_BG/PRIMARY/PRIMARY_TEXT/RAIL_INK and
  `REGION_TEXT_<id|obj|sub|bar|loc|info>` for text on UI surfaces.
- `color(role)` resolves semantic role names or returns a literal hex value.
- `set_style(widget, template)` stores a stylesheet template on the widget,
  substitutes `$INK`, `$WORK`, etc. immediately, and refreshes it on change.
- `set_item_colors(item, *, foreground=None, background=None, column=None)`
  stores semantic roles and applies brushes. `column` supports QTreeWidgetItem.
  The manager updates tagged model items without resetting models or selection.
- On scheme change the manager updates palette, global stylesheet, local bound
  styles and tagged model colors, then calls `refresh_theme()` on widgets that
  implement it. These hooks update visual content only, never data or workflows.

Common widgets, rail icons and canvas surroundings use those interfaces.
Page/editor inline styles become token templates; rich text and cached custom
brushes receive small `refresh_theme()` hooks. Dynamic error/status colors bind
semantic role names when the state changes, so a later theme switch remains safe.

## Validation

Test startup light/dark/unknown, signal-driven switching, repeat initialization,
existing/new controls and dialogs, tagged tree/table data, state preservation and
unchanged paper image pixels/zoom. Measure representative text contrast. Capture
actual Qt screenshots of all five pages in both schemes, including loaded editor
and result evidence, at 1440 x 900 and 1280 x 800.

Run focused tests, independent code review, scoped lint, and full non-slow suite.
The implementation is shared across macOS/Windows; this macOS environment cannot
prove native Windows OS notification, title-bar or file-dialog behavior. Record
that boundary explicitly. No packaging, commits, dependency upgrades or global
system appearance changes.
