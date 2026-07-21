"""FlowShift clipboard HTML tests (pure + runtime helper).

Covers CF_HTML build/parse, preview text extraction, HTML item construction,
store roundtrips, and manager sync. Runs on any OS.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_html as chm
import clipboard_model as cm
from clipboard_runtime import ClipboardManager
from clipboard_store import ClipboardStore
import clipboard_win as cw

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


fragment = "<div>Grüße &amp; <b>HTML</b> € 😄</div>"
source_url = "https://example.test/äö?x=1&y=2"
cf_html = chm.build_cf_html(fragment, source_url=source_url)
parsed = chm.parse_cf_html(cf_html)

check(parsed is not None, "build_cf_html parses back")
check(parsed["fragment"] == fragment, "fragment roundtrip")
check(parsed["source_url"] == source_url, "source_url roundtrip")
check("<!--StartFragment-->" in parsed["html"] and "<!--EndFragment-->" in parsed["html"],
      "fragment markers present")
check(cf_html[parsed["start_html"]:parsed["end_html"]].decode("utf-8") == parsed["html"],
      "StartHTML/EndHTML are byte offsets")
check(cf_html[parsed["start_fragment"]:parsed["end_fragment"]].decode("utf-8") == parsed["fragment"],
      "StartFragment/EndFragment are byte offsets")
check(parsed["fragment"].endswith("😄</div>"), "multibyte characters survive roundtrip")

marked = chm.ensure_fragment_markers("<p>hello</p>")
check(marked.count("<!--StartFragment-->") == 1 and marked.count("<!--EndFragment-->") == 1,
      "ensure_fragment_markers inserts markers")

preview = chm.html_to_preview_text("<div>Hello <b>World</b> &amp; Grüße</div>")
check(preview == "Hello World & Grüße", "html_to_preview_text strips tags and decodes entities")
check(len(chm.html_to_preview_text("<p>" + ("x" * 300) + "</p>", max_chars=20)) == 20,
      "html_to_preview_text truncates to max_chars")
check(chm.parse_cf_html(b"not cf html") is None, "broken CF_HTML returns None")

item = cm.make_html_item(cf_html, preview, seq=7, source_url=source_url)
check(item["kind"] == cm.KIND_HTML and item["mime"] == "text/html", "HTML item shape")
check(item["sha256"] == cm.sha256_bytes(cf_html), "HTML item sha256 stable")
check(item["preview_text"] == preview, "HTML item preview_text set")
check(item["metadata"]["has_html"] is True and item["metadata"]["source_url"] == source_url,
      "HTML item metadata carries source_url")

tmp = tempfile.mkdtemp(prefix="fs_clip_html_")
try:
    store = ClipboardStore(tmp, "device_html")
    stored, _ = store.add_item(item, data=cf_html)
    check(store.get_data(stored["item_id"]) == cf_html, "store roundtrip keeps CF_HTML bytes")
    manifest = store.build_manifest("device-test")
    check(manifest["items"][0]["kind"] == cm.KIND_HTML, "manifest contains html kind")
    check(manifest["items"][0]["metadata"]["has_html"] is True, "manifest keeps HTML metadata")

    SETTINGS = cm.clipboard_settings({"clipboard": {"enabled": True, "sync_on_activate": True}})
    inbox = {"A": [], "B": []}

    def send_from_A(identity, msg):
        inbox["B"].append(("device:A", msg))

    def send_from_B(identity, msg):
        inbox["A"].append(("device:B", msg))

    A = ClipboardManager(os.path.join(tmp, "A"), "A", send_from_A, lambda: SETTINGS)
    B = ClipboardManager(os.path.join(tmp, "B"), "B", send_from_B, lambda: SETTINGS)

    def pump():
        idle_rounds = 0
        for _ in range(10000):
            if not inbox["A"] and not inbox["B"]:
                if idle_rounds >= 200:
                    return
                idle_rounds += 1
                time.sleep(0.01)
                continue
            idle_rounds = 0
            if inbox["A"]:
                sender_ident, msg = inbox["A"].pop(0)
                A.handle(sender_ident, msg)
            elif inbox["B"]:
                sender_ident, msg = inbox["B"].pop(0)
                B.handle(sender_ident, msg)

    stored_html = A.capture_html("device:B", cf_html)
    check(stored_html is not None and stored_html["kind"] == cm.KIND_HTML, "manager captures html item")
    A.on_profile_activated("device:B")
    pump()
    items_b = B.list_items("device:A")
    check(len(items_b) == 1 and items_b[0]["kind"] == cm.KIND_HTML and items_b[0]["available"],
          "sync transfers html item")
    check(B.get_html("device:A", items_b[0]["item_id"]) == cf_html, "target store returns html bytes")

finally:
    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass


if sys.platform != "win32":
    check(cw.read_html() is None, "read_html returns None off Windows")
    check(cw.set_html(cf_html, "fallback") is False, "set_html is a no-op off Windows")
    check(cw.has_html() is False, "has_html is false off Windows")


print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard HTML tests passed.")
