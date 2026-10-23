# tests/test_ui_contrast.py
# Non-text contrast of the page's own colours (WCAG 2.2 1.4.11, #298), worked
# out from workspace/ui/tokens.css: the border of fields and buttons
# (--control-line) and the focus ring (--accent) reach 3:1 against each surface
# a control sits on, in the light and the dark theme. The meeting title, which
# has no border, shows a --control-line boundary when the pointer is over it
# and keeps the focus ring when it has focus. The design boards draw controls
# with the same border colour as the tokens. tools/accessibility_check.py
# measures the live page; these tests keep the tokens from drifting back.

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = (ROOT / "workspace" / "ui" / "tokens.css").read_text(encoding="utf-8")
APP_CSS = (ROOT / "workspace" / "ui" / "app.css").read_text(encoding="utf-8")
BOARDS = ROOT / "docs" / "design" / "boards"
NON_TEXT = 3.0      # WCAG 2.2 1.4.11; no rounding up


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


class LuminanceTests(unittest.TestCase):
    def test_the_srgb_threshold_is_wcag_2_2s(self):
        # A blended channel can fall between the WCAG 2.0 threshold (0.03928)
        # and the WCAG 2.2 one (0.04045); there it is linear.
        v = 10.2      # 0.0400 of full scale
        self.assertAlmostEqual(v / 255 / 12.92, luminance((v, v, v)), places=12)


class ControlBorderContrastTests(unittest.TestCase):
    def test_the_control_border_reaches_3_to_1_on_every_surface(self):
        for theme, tokens in themes().items():
            line = rgba(tokens["--control-line"])
            for name, bg in surfaces(tokens).items():
                with self.subTest(theme=theme, surface=name):
                    self.assertGreaterEqual(ratio(over(line, bg), bg), NON_TEXT)

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
        # token) as the token's value, never the values before #298.
        before_298 = ("rgba(20,41,102,0.28)", "rgba(255,255,255,0.32)", "rgba(20,41,102,0.24)")
        text = "".join(b.read_text(encoding="utf-8") for b in sorted(BOARDS.glob("*.dc.html")))
        for old in before_298:
            self.assertNotIn(old, text, "a board draws a control with the old border colour")
        for theme, tokens in themes().items():
            with self.subTest(theme=theme):
                self.assertIn(tokens["--control-line"].replace(" ", ""), text)


class TextAndRingOnColourTests(unittest.TestCase):
    """The tick on a done step, "Optional" on the current setup step, and the
    focus ring on pink (#298, from the real-window run of the extended check)."""

    def test_the_done_tick_reaches_4_5_to_1(self):
        for theme, tokens in themes().items():
            with self.subTest(theme=theme):
                done = rgba(tokens["--done"])[:3]
                self.assertGreaterEqual(ratio(rgba(tokens["--done-fg"])[:3], done), 4.5)

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
        text = "".join(b.read_text(encoding="utf-8") for b in sorted(BOARDS.glob("*.dc.html")))
        self.assertNotIn("#00A275", text.upper(), "a board draws a done step in the old green")
        self.assertIn(themes()["light"]["--done"].strip().upper(), text.upper())


class MeetingTitleBoundaryTests(unittest.TestCase):
    def test_hovering_the_title_shows_a_control_border(self):
        hover = [body for sel, body in rules(APP_CSS, ".title-edit textarea:hover")]
        self.assertTrue(any(re.search(r"\boutline\s*:\s*1px solid var\(--control-line\)", b)
                            for b in hover),
                        "the meeting title has no visible boundary on hover")

    def test_the_hover_boundary_gives_way_to_the_focus_ring(self):
        for sel, body in rules(APP_CSS, ".title-edit textarea:hover"):
            if "outline" in body:
                self.assertIn(":not(:focus-visible)", sel)

    def test_the_title_keeps_the_focus_ring(self):
        for sel, body in rules(APP_CSS, ".title-edit"):
            with self.subTest(selector=sel):
                self.assertNotRegex(body, r"\boutline\s*:\s*(none|0)\b")
        ring = [body for sel, body in rules(APP_CSS, ":focus-visible")
                if sel.strip() == ":focus-visible"]
        self.assertTrue(any("2px solid var(--accent)" in b for b in ring))


if __name__ == "__main__":
    unittest.main()
