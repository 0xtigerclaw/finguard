"""Turn the Anthropic S-1 web page into a PDF, one filing page per sheet.

Usage: python scripts/render_s1.py anthropicpbc-sx1.htm data/ANTH-S1-2026.pdf
Needs: pip install playwright pymupdf, then playwright install chromium-headless-shell

The filing is untrusted, so scripts are stripped and disabled and the network is off.
"""
import re
import sys
import tempfile
from pathlib import Path

import pymupdf
from playwright.sync_api import sync_playwright

PAGE_START = '<div style="--justify:justify;--position:absolute;background-color:#FFFFFF;'
PAGES_PER_CHUNK = 25
STYLE = """<style>@page{size:612pt 792pt;margin:0}html,body{margin:0;padding:0;background:#fff}
.p{break-after:page}.p>div{margin:0!important;border:0!important;content-visibility:visible!important}</style>"""


def main(source: str, out: str) -> None:
    html = re.sub(r"<script\b.*?</script>", "", Path(source).read_text(errors="ignore"), flags=re.I | re.S)
    pages = html.split(PAGE_START)[1:]
    merged = pymupdf.open()
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(java_script_enabled=False, offline=True).new_page()
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith("file:") else r.abort())
        for start in range(0, len(pages), PAGES_PER_CHUNK):
            body = "".join(f'<div class="p">{PAGE_START}{x}</div>' for x in pages[start:start + PAGES_PER_CHUNK])
            chunk = Path(tmp) / f"{start}.html"
            chunk.write_text(f"<html><head>{STYLE}</head><body>{body}</body></html>")
            page.goto(f"file://{chunk}", wait_until="domcontentloaded", timeout=120000)
            pdf = Path(tmp) / f"{start}.pdf"
            page.pdf(path=str(pdf), prefer_css_page_size=True, print_background=True)
            merged.insert_pdf(pymupdf.open(str(pdf)))
        browser.close()
    merged.save(out, garbage=3, deflate=True)


if __name__ == "__main__":
    main(*sys.argv[1:3])
