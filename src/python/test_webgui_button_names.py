"""Every WebGUI button has a name a person can see and a screen reader can read.

Viktor 26.11.: under Software Update the buttons of "Updates from connected
devices" were empty squares again (a relapse). They carried only a Font
Awesome ``<i className="fas ...">`` icon plus an ``aria-label``; Font Awesome
comes from a CDN and renders nothing when that stylesheet does not load, so
the button showed neither text nor symbol.

This test reads the JSX source of the WebGUI (it runs without Node, so the
Python test runner checks it on every change) and requires for each
``<button>``: visible text, or an inline ``<ButtonIcon>``/``<svg>`` symbol
together with an ``aria-label``. A Font Awesome icon alone never counts.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WEBGUI_SRC = os.path.normpath(os.path.join(HERE, "..", "..", "webgui", "src"))

_BUTTON = re.compile(r"<button\b((?:[^>{]|\{[^}]*\})*)>(.*?)</button>", re.S)
_FA_ICON = re.compile(r"<i\b[^>]*/>|<i\b[^>]*>\s*</i>", re.S)
_COMMENT = re.compile(r"\{\s*/\*.*?\*/\s*\}", re.S)
_TAG = re.compile(r"<[^>]*>", re.S)


def button_problems(source):
    """Return (line, snippet) for each button without an accessible visible name."""
    problems = []
    for match in _BUTTON.finditer(source):
        attrs, body = match.group(1), match.group(2)
        body = _COMMENT.sub("", body)
        # Inline symbols: <svg> or an icon component such as <ButtonIcon>/<SelectionIcon>.
        has_symbol = re.search(r"<svg\b|<[A-Z]\w*Icon\b", body) is not None
        text = _TAG.sub("", _FA_ICON.sub("", body)).strip()
        has_label = re.search(r"\baria-label\s*=", attrs) is not None
        if text or (has_symbol and has_label):
            continue
        line = source[:match.start()].count("\n") + 1
        problems.append((line, " ".join(match.group(0).split())[:120]))
    return problems


def webgui_sources():
    for folder, _dirs, files in os.walk(WEBGUI_SRC):
        if "node_modules" in folder:
            continue
        for name in sorted(files):
            if name.endswith(".jsx") and ".test." not in name:
                yield os.path.join(folder, name)


class ButtonNameRuleTest(unittest.TestCase):
    def test_rule_rejects_font_awesome_only_button(self):
        bad = ('<button className="btn" aria-label="Download" title="Download">'
               '<i className="fas fa-download" aria-hidden="true" /></button>')
        self.assertEqual(len(button_problems(bad)), 1)

    def test_rule_rejects_svg_without_label(self):
        self.assertEqual(len(button_problems('<button><ButtonIcon name="save" /></button>')), 1)

    def test_rule_accepts_text_and_labelled_symbol(self):
        ok = ('<button onClick={() => act("download")}><ButtonIcon name="download" /> Download</button>\n'
              '<button aria-label="Move up"><ButtonIcon name="arrow-up" /></button>\n'
              "<button>{busy ? 'Checking...' : 'Check'}</button>")
        self.assertEqual(button_problems(ok), [])


class WebGuiButtonsHaveNamesTest(unittest.TestCase):
    def test_sources_found(self):
        self.assertTrue(any(True for _ in webgui_sources()), WEBGUI_SRC)

    def test_every_button_has_visible_name(self):
        found = []
        for path in webgui_sources():
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for line, snippet in button_problems(source):
                found.append(f"{os.path.relpath(path, WEBGUI_SRC)}:{line}: {snippet}")
        self.assertEqual(found, [], "buttons without visible text or labelled inline symbol:\n" + "\n".join(found))

    def test_peer_update_buttons_say_what_they_do(self):
        with open(os.path.join(WEBGUI_SRC, "components", "PeerUpdatePanel.jsx"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertFalse('className="fas' in source, "PeerUpdatePanel still uses Font Awesome icons")
        for text in ("Download", "Install", "Save"):
            self.assertRegex(source, r"<ButtonIcon name=\"[a-z-]+\" />\s*" + text)


if __name__ == "__main__":
    unittest.main()
