# System Theme Implementation Plan

> Use the existing executing-plans / bounded parallel-worker workflow. The user
> has authorized implementation; preserve prior uncommitted work.

**Goal:** Follow system light/dark appearance across the existing desktop UI.

**Spec:** `docs/superpowers/specs/2026-11-08-system-theme-design.md`

## Work

- [ ] Core: failing tests, semantic palettes, Qt notification lifecycle, dynamic
  local styles, tagged item brushes, common widgets, rail and card background.
- [ ] Operational pages: migrate scan/results/exam/settings styles and dynamic
  status/rich text without rereading/resetting user state; focused tests.
- [ ] Template editor: migrate page, tree, properties, dialog and canvas
  surroundings; preserve annotation colors, dirty state, undo and zoom; tests.
- [ ] Integrate, inspect screenshots in light/dark at both desktop sizes,
  review changes, fix findings, then freeze implementation.
- [ ] Run complete non-slow suite, scoped Ruff F and diff check; document exact
  automatic verification and remaining native Windows verification.

## Review Focus

- Existing controls/models retain captured light colors after switching.
- Disabled/selected text and popup surfaces mix native and custom palettes.
- Theme callbacks reset edits, selections, zoom or current scan/recognition work.
- Deferred callbacks access destroyed widgets or duplicate after initialization.
- Evidence pixels and paper annotation colors change with the UI theme.

## Ownership

Parent: theme.py, widgets/common.py, widgets/card_view.py, main_window.py,
core tests, screenshots, docs and integration.

Page worker: pages/{scan_page,results_page,exam_page,settings_page}.py and its
own theme-page tests.

Editor worker: pages/template_page.py, editor/{tree,props,new_dialog,canvas}.py
and its own theme-editor tests.

Workers run only focused checks; parent runs the full suite after integration.
