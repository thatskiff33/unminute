# Design

The approved design for the Unminute window (#89). The boards in `boards/` are the specification: the app should look and behave as they show.

## Boards

`boards/` holds the source of every board from the design canvas, signed off on 15 October 2026 and frozen at canvas version `1792040066-d1fe`. `Locked` was added afterwards in the same format, for the locked screen (#111 C4b); it follows the signed-off tokens and the `Help` page's type and spacing. `canvas.json` lists them with their size and title. Each `.dc.html` file is one board; several take a property (for example `Meeting.dc.html` takes `direction` and `phase`) and the small files import a larger board with fixed properties.

All names and text in the boards are fictional sample content.

| Row | Boards |
|---|---|
| Brand system | `Main` (colour roles, type, buttons, speaker chips, contrast) |
| Meetings page | `Home`, `Recording`, `Home-Empty`, `Home-Drop`, `Home-States`, `Recording-States`; draft text while recording (#104, added 23 October 2026): `Recording-Drafts`, `Recording-Drafts-Dark`, `Recording-Drafts-States` |
| Reviewing a meeting | `Meeting`, `Meeting-Transcribing`, `Meeting-Edit`, `Meeting-Reviewed`, `Meeting-Earlier`, `Meeting-Panels` |
| Dialogs and hand off | `Names`, `Confirm`, `Handoff`, `Handoff-Template` |
| First-run setup | `Setup-1`, `Setup` (step 2), `Setup-3`, `Setup-4`, `Setup-5` |
| Settings, help and the locked screen | `Settings`, `Models` (Downloads), `Help`; `Models-Queued`, `Models-Queued-Dark` and `Models-Resumed` (queued downloads, #366: `Models` with its `state` and `theme` properties); `Locked` (added for #111 C4b, 22 October 2026, after the sign-off below) |
| Narrow windows | `Meeting-Narrow` (900 px), `Meeting-640`, `Meeting-320`, `Home-640`, `Home-320` |
| Dark theme | `Home-Dark`, `Meeting-Dark` |
| Explored, not chosen | `Home-B`, `Meeting-B` |

The `Today-*` boards show the app before the redesign, for comparison.

`assets/` holds canvas assets the boards use but the app does not ship (the white Unlimit logo); `tools/design_check/render.js` maps them.

## Tokens

`workspace/ui/tokens.css` turns the boards' values into CSS custom properties (colours, type, radii, shadows, control heights) for light and dark themes. Archivo is bundled in `workspace/ui/fonts/` under the SIL Open Font Licence (`OFL.txt`).

## Checking the app against the boards

`tools/design_check.py` renders each board and the matching app screen in headless Microsoft Edge at the same size and writes board, app and difference images with a score (the share of pixels that differ visibly):

```
python tools/design_check.py <out-dir> [case ...]
```

- Boards are rendered by `tools/design_check/render.html`, a small renderer for the board format, with the app's bundled Archivo.
- The app runs from `workspace/ui` with a fake bridge (`tools/design_check/fixtures.js`) that serves the boards' sample meeting, so no workspace, model or network is used. Each case names a board and an app scenario.
- It needs Microsoft Edge and Pillow (both in the development environment). It is a development tool and is not shipped.

The check compares pixels, so it catches layout, spacing, colour and wording differences. It does not test behaviour; `tools/workspace_ui_check.py` and `tools/accessibility_check.py` still do that on the real window.
