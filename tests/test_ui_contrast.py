# tests/test_ui_contrast.py
# Non-text contrast of the page's own colours (WCAG 2.2 1.4.11, #298), worked
# out from workspace/ui/tokens.css: the focus ring (--accent) reaches 3:1
# against each surface a control sits on, in the light and the dark theme, and
# so does the ring on pink and the darker "Optional" on the current setup step.
#
# Three things fall short of WCAG 2.2 AA for the brand look, by the owner's
# decision of 25 October 2026 (NZ time), recorded on #298 at
# issuecomment-6391966468. Each is a named owner exception in
# tools/accessibility_check.py (OWNER_EXCEPTIONS), and these tests keep the
# page, the boards and that record agreeing, so nothing else slips under it:
#   - control borders (--control-line, 1.4.11): the brand values, below 3:1;
#   - the meeting title at rest (1.4.11): no border until hovered; the hover
#     border reaches 3:1 and keyboard focus draws the 2 px ring over it;
#   - the tick on a done step (--done): a graphic drawn by the style, at 3:1
#     (1.4.11), not text at 4.5:1 (1.4.3).
# tools/accessibility_check.py measures the live page (tests/test_ui_page_audit.py
# runs its audit headless); these tests keep the tokens and rules from drifting.

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import accessibility_check  # noqa: E402

TOKENS = (ROOT / "workspace" / "ui" / "tokens.css").read_text(encoding="utf-8")
APP_CSS = (ROOT / "workspace" / "ui" / "app.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "workspace" / "ui" / "index.html").read_text(encoding="utf-8")
BOARDS = ROOT / "docs" / "design" / "boards"
NON_TEXT = 3.0      # WCAG 2.2 1.4.11; no rounding up
EXCEPTIONS = accessibility_check.OWNER_EXCEPTIONS
DECISION = "https://github.com/thatskiff33/unminute/issues/298#issuecomment-6391966468"


def declarations(block):
    """{--name: value} for the custom properties in a block of CSS."""
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


def themes():
    """The light tokens and the dark tokens (light values the dark theme does not override)."""
    light_block = re.search(r"^:root\s*\{(.*?)^\}", TOKENS, re.DOTALL | re.MULTILINE).group(1)
    dark_media = TOKENS[TOKENS.index("@media (prefers-color-scheme: dark)"):]
    dark_block = re.search(r":root\s*\{(.*?)\}", dark_media, re.DOTALL).group(1)
    light = declarations(light_block)
    return {"light": light, "dark": {**light, **declarations(dark_block)}}


def rgba(value):
    """(r, g, b, a) from #RRGGBB or rgba(r, g, b, a)."""
    value = value.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)) + (1.0,)
    m = re.fullmatch(r"rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\s*\)", value)
    if m:
        return (int(m[1]), int(m[2]), int(m[3]), float(m[4]))
    raise ValueError(f"not a colour this test reads: {value}")


def over(colour, background):
    """colour drawn over an opaque background (r, g, b)."""
    *rgb, a = colour
    return tuple(c * a + b * (1 - a) for c, b in zip(rgb, background, strict=True))


def luminance(rgb):
    # WCAG 2.2 relative luminance (sRGB): linear at or below 0.04045.
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def surfaces(tokens):
    """The opaque colours a control can sit on: the page, a card, the sidebar,
    and a quiet panel (--soft) or a field's fill (--input-bg) on a card."""
    card = over(rgba(tokens["--card"]), over(rgba(tokens["--page"]), (255, 255, 255)))
    page = over(rgba(tokens["--page"]), (255, 255, 255))
    return {"page": page, "card": card,
            "sidebar": over(rgba(tokens["--side-bg"]), page),
            "soft panel on a card": over(rgba(tokens["--soft"]), card),
            "field fill on a card": over(rgba(tokens["--input-bg"]), card)}


def rules(css, selector_part):
    """The declaration blocks of rules whose selector contains selector_part."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
            if selector_part in sel]


def board_text():
    return "".join(b.read_text(encoding="utf-8") for b in sorted(BOARDS.glob("*.dc.html")))


class LuminanceTests(unittest.TestCase):
    def test_the_srgb_threshold_is_wcag_2_2s(self):
        # A blended channel can fall between the WCAG 2.0 threshold (0.03928)
        # and the WCAG 2.2 one (0.04045); there it is linear.
        v = 10.2      # 0.0400 of full scale
        self.assertAlmostEqual(v / 255 / 12.92, luminance((v, v, v)), places=12)


class OwnerExceptionRecordTests(unittest.TestCase):
    """The owner's exceptions of 25 October 2026 are named, with their rule,
    what they cover and the decision, and match the page exactly."""

    def test_the_three_exceptions_and_no_others(self):
        self.assertEqual({"control borders": "1.4.11", "meeting title at rest": "1.4.11",
                          "done tick": "1.4.3 (as text); 1.4.11 (as a graphic)"},
                         {name: ex["criterion"] for name, ex in EXCEPTIONS.items()})
        for name, ex in EXCEPTIONS.items():
            with self.subTest(exception=name):
                self.assertEqual(DECISION, ex["decision"])

    def test_the_control_border_exception_is_the_token_s_value(self):
        recorded = EXCEPTIONS["control borders"]
        self.assertIn("--control-line", recorded["covers"])
        for theme, tokens in themes().items():
            with self.subTest(theme=theme):
                self.assertEqual(rgba(recorded["tokens"][theme]), rgba(tokens["--control-line"]))
                # The pattern for this mode matches its colour as the browser
                # reports it; the other mode's colour is not excused here.
                drawn = recorded["tokens"][theme]
                other = recorded["tokens"]["dark" if theme == "light" else "light"]
                self.assertEqual("control borders", accessibility_check.owner_exception(
                    "low_contrast_boundary", f"input#x 1.80 {drawn}", theme))
                self.assertIsNone(accessibility_check.owner_exception(
                    "low_contrast_boundary", f"input#x 1.80 {other}", theme))

    def test_the_title_exception_names_the_title_rule(self):
        self.assertIn(".title-edit textarea", EXCEPTIONS["meeting title at rest"]["covers"])
        self.assertIn("--done", EXCEPTIONS["done tick"]["covers"])
        self.assertIn(".done-tick", EXCEPTIONS["done tick"]["covers"])


class ControlBorderContrastTests(unittest.TestCase):
    def test_the_control_border_is_3_to_1_or_the_owner_s_exception(self):
        # Below 3:1 only at the brand values the owner accepted; any other
        # value must reach 3:1 on every surface. An exception no longer needed
        # (the value reaches 3:1 everywhere) is taken out of the record.
        recorded = EXCEPTIONS["control borders"]["tokens"]
        for theme, tokens in themes().items():
            line = rgba(tokens["--control-line"])
            excepted = line == rgba(recorded[theme])
            lowest = min(ratio(over(line, bg), bg) for bg in surfaces(tokens).values())
            with self.subTest(theme=theme):
                if excepted:
                    self.assertLess(lowest, NON_TEXT, "the owner exception is no longer needed")
                else:
                    self.assertGreaterEqual(lowest, NON_TEXT)

    def test_the_focus_ring_reaches_3_to_1_on_the_page_and_a_card(self):
        for theme, tokens in themes().items():
            ring = rgba(tokens["--accent"])
            for name in ("page", "card"):
                bg = surfaces(tokens)[name]
                with self.subTest(theme=theme, surface=name):
                    self.assertGreaterEqual(ratio(over(ring, bg), bg), NON_TEXT)

    def test_the_boards_draw_controls_with_the_token_colour(self):
        # The boards write colours inline, without spaces: the control border
        # (and the switch's off track, which the page draws with the same
        # token) as the token's value, never #319's values or an older one.
        others = ("rgba(20,41,102,0.51)", "rgba(255,255,255,0.38)", "rgba(20,41,102,0.24)")
        text = board_text()
        for old in others:
            self.assertNotIn(old, text, "a board draws a control with another border colour")
        for theme, tokens in themes().items():
            with self.subTest(theme=theme):
                self.assertIn(tokens["--control-line"].replace(" ", ""), text)


class TextAndRingOnColourTests(unittest.TestCase):
    """The tick on a done step, "Optional" on the current setup step, and the
    focus ring on pink (#298, from the real-window run of the extended check)."""

    def test_the_done_tick_is_a_graphic_at_3_to_1(self):
        # WCAG 1.4.11 for a graphic (the owner's decision: not text at 4.5:1).
        for theme, tokens in themes().items():
            with self.subTest(theme=theme):
                done = rgba(tokens["--done"])[:3]
                self.assertGreaterEqual(ratio(rgba(tokens["--done-fg"])[:3], done), NON_TEXT)

    def test_the_done_tick_is_drawn_by_the_style_not_written_as_text(self):
        # The style draws the tick with an empty text alternative; the page
        # writes no tick as text, and each done step's marker is aria-hidden.
        body = "".join(b for sel, b in rules(APP_CSS, ".done-tick::before"))
        self.assertRegex(body, r'\bcontent\s*:\s*"\\2713"\s*/\s*""')
        for name, source in (("app.js", APP_JS), ("index.html", INDEX)):
            with self.subTest(file=name):
                self.assertNotIn("\u2713", source)
                self.assertNotIn("&#10003;", source)
        for marker in ('class: done[i] ? "step-dot done-tick" : "step-dot", "aria-hidden": "true"',
                       'class: kind === "done" ? "su-dot done-tick" : "su-dot", '
                       '"aria-hidden": "true"'):
            self.assertIn(marker, APP_JS)
        self.assertIn('<span class="ho-n done-tick" aria-hidden="true"></span>', INDEX)
        # Each done marker is drawn in --done-fg on --done.
        for sel in (".stepper li.done .step-dot", ".su-steps li.done .su-dot",
                    ".ho-step.done .ho-n"):
            with self.subTest(selector=sel):
                drawn = "".join(b for s, b in rules(APP_CSS, sel))
                self.assertIn("background: var(--done)", drawn)
                self.assertIn("color: var(--done-fg)", drawn)

    def test_optional_on_the_current_setup_step_reaches_4_5_to_1(self):
        body = "".join(b for sel, b in rules(APP_CSS, ".su-steps li.now .su-opt"))
        token = re.search(r"color\s*:\s*var\((--[\w-]+)\)", body)
        if token is None:
            self.fail("no colour for Optional on the current step")
        for theme, tokens in themes().items():
            with self.subTest(theme=theme):
                bg = over(rgba(tokens["--side-sel"]), surfaces(tokens)["page"])
                fg = over(rgba(tokens[token[1]]), bg)
                self.assertGreaterEqual(ratio(fg, bg), 4.5)

    def test_the_focus_ring_on_pink_reaches_3_to_1(self):
        # Each pink surface with controls on it, the rule that recolours the
        # ring there, and the colour that rule must reach 3:1 against.
        cases = {".ho-next :focus-visible": "--hold", ".holds li :focus-visible": "--hold",
                 ".notice.hold :focus-visible": "--hold", ".m-banner.hold :focus-visible": "--hold",
                 ".banner :focus-visible": "--hold", ".su-test.quiet :focus-visible": "--hold",
                 "#transcript li.hit :focus-visible": "--hit",
                 ".ho-passages label:has(input:checked) :focus-visible": "--hit"}
        for selector, surface in cases.items():
            body = "".join(b for sel, b in rules(APP_CSS, selector))
            token = re.search(r"outline-color\s*:\s*var\((--[\w-]+)\)", body)
            with self.subTest(selector=selector):
                self.assertIsNotNone(token, "the ring keeps its orange on pink")
            if not token:
                continue
            for theme, tokens in themes().items():
                with self.subTest(selector=selector, theme=theme):
                    bg = over(rgba(tokens[surface]), surfaces(tokens)["card"])
                    self.assertLess(ratio(over(rgba(tokens["--accent"]), bg), bg), NON_TEXT)
                    self.assertGreaterEqual(ratio(over(rgba(tokens[token[1]]), bg), bg), NON_TEXT)

    def test_the_boards_draw_done_steps_with_the_token_colour(self):
        text = board_text().upper()
        self.assertNotIn("#008661", text, "a board draws a done step in #319's green")
        self.assertIn(themes()["light"]["--done"].strip().upper(), text)


def exact_rule(selector):
    """The declarations of the rule with exactly this selector (empty if none)."""
    found = [body for sel, body in rules(APP_CSS, selector) if sel == selector]
    return found[0] if found else ""


def board_title(board):
    """The inline style of the sample meeting's title on a design board."""
    text = (BOARDS / board).read_text(encoding="utf-8")
    found = re.search(r"<h1 style=\"([^\"]*)\">Community garden planning", text)
    return found[1] if found else None


class MeetingTitleBoundaryTests(unittest.TestCase):
    """The meeting title is a field drawn as a heading. At rest it shows no
    border (the owner exception "meeting title at rest"); hovered, its border
    takes a colour at 3:1, and keyboard focus covers it with the 2 px ring."""

    def test_the_title_has_no_visible_border_at_rest(self):
        body = exact_rule(".title-edit textarea")
        self.assertRegex(body, r"\bborder\s*:\s*1px solid transparent\s*(;|$)",
                         "the meeting title shows a border at rest")
        self.assertRegex(body, r"\bbackground\s*:\s*transparent\b")

    def test_hovering_shows_a_border_at_3_to_1(self):
        body = exact_rule(".title-edit textarea:hover")
        token = re.search(r"\bborder-color\s*:\s*var\((--[\w-]+)\)", body)
        if token is None:
            self.fail("hovering the title shows no border")
        for theme, tokens in themes().items():
            soft = over(rgba(tokens["--soft"]), surfaces(tokens)["page"])
            with self.subTest(theme=theme):
                self.assertGreaterEqual(ratio(over(rgba(tokens[token[1]]), soft), soft), NON_TEXT)

    def test_keyboard_focus_stays_distinct_and_thicker(self):
        # The page's ring (2 px, orange) is drawn over the 1 px border: the
        # title's focus rule may move it, never thin, hide or recolour it.
        ring = exact_rule(":focus-visible")
        width = re.search(r"\boutline\s*:\s*(\d+)px solid var\(--accent\)", ring)
        border = re.search(r"\bborder\s*:\s*(\d+)px solid", exact_rule(".title-edit textarea"))
        self.assertIsNotNone(width)
        self.assertIsNotNone(border)
        self.assertGreater(int(width[1]), int(border[1]))
        focus = exact_rule(".title-edit textarea:focus-visible")
        self.assertNotRegex(focus, r"\boutline(-width|-color|-style)?\s*:")
        offset = re.search(r"\boutline-offset\s*:\s*(-?\d+)px", focus)
        if offset:      # the ring covers the border instead of sitting beside it
            self.assertGreaterEqual(int(offset[1]), -int(border[1]))
        for theme, tokens in themes().items():
            bg = surfaces(tokens)["page"]
            with self.subTest(theme=theme):
                self.assertGreaterEqual(ratio(over(rgba(tokens["--accent"]), bg), bg), NON_TEXT)

    def test_no_title_rule_draws_an_outline_of_its_own(self):
        # The title's boundary is its border; an outline on it at rest or on
        # hover would outrank or crowd the focus ring.
        for sel, body in rules(APP_CSS, ".title-edit"):
            with self.subTest(selector=sel):
                self.assertNotRegex(body, r"\boutline(-width|-color|-style)?\s*:")

    def test_the_boards_draw_the_title_without_a_border(self):
        # The boards show the title at rest.
        for board in ("Meeting.dc.html", "Meeting-Narrow.dc.html"):
            with self.subTest(board=board):
                style = board_title(board)
                self.assertIsNotNone(style, "the board has no sample title")
                self.assertNotRegex(style or "", r"\bborder\b")

    def test_the_title_keeps_the_focus_ring(self):
        for sel, body in rules(APP_CSS, ".title-edit"):
            with self.subTest(selector=sel):
                self.assertNotRegex(body, r"\boutline\s*:\s*(none|0)\b")
        ring = [body for sel, body in rules(APP_CSS, ":focus-visible")
                if sel.strip() == ":focus-visible"]
        self.assertTrue(any("2px solid var(--accent)" in b for b in ring))


if __name__ == "__main__":
    unittest.main()
