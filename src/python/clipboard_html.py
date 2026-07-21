"""FlowShift clipboard HTML helpers (pure, Windows-format aware).

This module builds and parses Windows CF_HTML payloads without touching any
Windows APIs. It is safe to unit-test on any OS.
"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

START_FRAGMENT_MARKER = "<!--StartFragment-->"
END_FRAGMENT_MARKER = "<!--EndFragment-->"
_VERSION = "0.9"

_BODY_OPEN_RE = re.compile(r"(?is)<body\b[^>]*>")
_BODY_CLOSE_RE = re.compile(r"(?is)</body\s*>")
_HTML_OPEN_RE = re.compile(r"(?is)<html\b[^>]*>")
_HTML_CLOSE_RE = re.compile(r"(?is)</html\s*>")
_FIELD_RE = {
    "StartHTML": re.compile(rb"(?m)^StartHTML:(\d+)\s*$"),
    "EndHTML": re.compile(rb"(?m)^EndHTML:(\d+)\s*$"),
    "StartFragment": re.compile(rb"(?m)^StartFragment:(\d+)\s*$"),
    "EndFragment": re.compile(rb"(?m)^EndFragment:(\d+)\s*$"),
    "SourceURL": re.compile(rb"(?m)^SourceURL:(.*)$"),
}


def ensure_fragment_markers(html: str) -> str:
    """Ensure CF_HTML fragment markers are present in *html*."""
    html = html or ""
    if START_FRAGMENT_MARKER in html and END_FRAGMENT_MARKER in html:
        return html

    if _BODY_OPEN_RE.search(html) and _BODY_CLOSE_RE.search(html):
        html = _BODY_OPEN_RE.sub(lambda m: m.group(0) + START_FRAGMENT_MARKER, html, count=1)
        html = _BODY_CLOSE_RE.sub(END_FRAGMENT_MARKER + "</body>", html, count=1)
        return html

    if _HTML_OPEN_RE.search(html) and _HTML_CLOSE_RE.search(html):
        html = _HTML_OPEN_RE.sub(lambda m: m.group(0) + "<body>" + START_FRAGMENT_MARKER,
                                 html, count=1)
        html = _HTML_CLOSE_RE.sub(END_FRAGMENT_MARKER + "</body></html>", html, count=1)
        return html

    return f"<html><body>{START_FRAGMENT_MARKER}{html}{END_FRAGMENT_MARKER}</body></html>"


def build_cf_html(fragment_html: str, source_url: str | None = None) -> bytes:
    """Build a Windows CF_HTML payload from an HTML fragment.

    Offsets are byte offsets from the start of the returned byte string.
    """
    html_text = ensure_fragment_markers(fragment_html or "")
    html_bytes = html_text.encode("utf-8")

    source_line = ""
    if source_url:
        safe_url = str(source_url).replace("\r", " ").replace("\n", " ").strip()
        if safe_url:
            source_line = f"SourceURL:{safe_url}\r\n"

    header_template = (
        f"Version:{_VERSION}\r\n"
        "StartHTML:00000000\r\n"
        "EndHTML:00000000\r\n"
        "StartFragment:00000000\r\n"
        "EndFragment:00000000\r\n"
        f"{source_line}"
        "\r\n"
    )
    header_bytes = header_template.encode("utf-8")
    start_html = len(header_bytes)
    end_html = start_html + len(html_bytes)

    start_marker = html_bytes.find(START_FRAGMENT_MARKER.encode("utf-8"))
    end_marker = html_bytes.find(END_FRAGMENT_MARKER.encode("utf-8"))
    if start_marker < 0 or end_marker < 0 or end_marker < start_marker:
        raise ValueError("CF_HTML fragment markers not present")
    start_fragment = start_html + start_marker + len(START_FRAGMENT_MARKER.encode("utf-8"))
    end_fragment = start_html + end_marker

    header = (
        header_template
        .replace("StartHTML:00000000", f"StartHTML:{start_html:08d}")
        .replace("EndHTML:00000000", f"EndHTML:{end_html:08d}")
        .replace("StartFragment:00000000", f"StartFragment:{start_fragment:08d}")
        .replace("EndFragment:00000000", f"EndFragment:{end_fragment:08d}")
    )
    return header.encode("utf-8") + html_bytes


def parse_cf_html(data: bytes) -> dict | None:
    """Parse CF_HTML bytes and return a metadata dict, or None on failure."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    blob = bytes(data)

    def _field_int(name):
        m = _FIELD_RE[name].search(blob)
        if not m:
            return None
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None

    start_html = _field_int("StartHTML")
    end_html = _field_int("EndHTML")
    start_fragment = _field_int("StartFragment")
    end_fragment = _field_int("EndFragment")
    if None in (start_html, end_html, start_fragment, end_fragment):
        return None
    if not (0 <= start_html <= start_fragment <= end_fragment <= end_html <= len(blob)):
        return None

    source_url = None
    m = _FIELD_RE["SourceURL"].search(blob)
    if m:
        try:
            source_url = m.group(1).rstrip(b"\r\n").decode("utf-8", errors="strict").strip() or None
        except UnicodeDecodeError:
            source_url = m.group(1).rstrip(b"\r\n").decode("utf-8", errors="replace").strip() or None

    try:
        html_text = blob[start_html:end_html].decode("utf-8", errors="strict")
        fragment_text = blob[start_fragment:end_fragment].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None

    return {
        "html": html_text,
        "fragment": fragment_text,
        "source_url": source_url,
        "start_html": start_html,
        "end_html": end_html,
        "start_fragment": start_fragment,
        "end_fragment": end_fragment,
    }


class _TextExtractor(HTMLParser):
    _BLOCK_TAGS = {"br", "p", "div", "li", "tr", "td", "th", "section",
                   "article", "header", "footer", "h1", "h2", "h3", "h4",
                   "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append(" ")
        if tag.lower() in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_preview_text(html: str, max_chars: int = 240) -> str:
    """Convert HTML to a compact, safe text preview."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars is not None and max_chars >= 0 and len(text) > max_chars:
        return text[:max_chars]
    return text
