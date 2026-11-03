"""Turn a filing published as a web page into a PDF, keeping each statement whole on one page.

Usage: python scripts/render_web_filing.py filing.html out.pdf
Needs: pip install playwright, then playwright install chromium-headless-shell

A statement split across pages loses its units line or its column headings, so code must refuse
its numbers. The filing is untrusted, so its scripts are stripped and disabled and the network is off.
"""
import re
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

KEEP_WHOLE = """
const heading = /^(condensed\\s+)?(consolidated\\s+)?(unaudited\\s+)?(statements?\\s+of|balance\\s+sheets?)/i;
for (const el of document.querySelectorAll("p, div, h1, h2, h3, h4, center, td")) {
  const text = (el.innerText || "").trim();
  if (text.length > 120 || !heading.test(text) || text !== text.toUpperCase()) continue;
  const block = el.closest("table") || el;
  const before = block.previousElementSibling;
  const start = before && (before.innerText || "").trim().length < 60 && (before.innerText || "").trim() ? before : block;
  start.style.breakBefore = "page";
}
for (const table of document.querySelectorAll("table")) table.style.breakInside = "avoid";
"""


def main(source: str, out: str) -> None:
    html = re.sub(r"<script\b.*?</script>", "", Path(source).read_text(errors="ignore"), flags=re.I | re.S)
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        clean = Path(tmp) / "filing.html"
        clean.write_text(html)
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(java_script_enabled=False, offline=True).new_page()
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith("file:") else r.abort())
        page.goto(f"file://{clean}", wait_until="domcontentloaded", timeout=120000)
        page.evaluate(KEEP_WHOLE)
        page.pdf(path=out, format="Letter", print_background=True,
                 margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"})
        browser.close()


if __name__ == "__main__":
    main(*sys.argv[1:3])
