"""
Render docs/FlowMES-Complete-Documentation.md into a polished, branded PDF.

Pipeline:  Markdown  ->  HTML (python-markdown)  ->  PDF (xhtml2pdf / reportlab)

Run:  python docs/build_docs_pdf.py
"""
import os
import re

import markdown
from xhtml2pdf import pisa
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "FlowMES-Complete-Documentation.md")
OUT = os.path.join(HERE, "FlowMES-Complete-Documentation.pdf")

# ── Fonts ─────────────────────────────────────────────────────────
# Register Windows Unicode fonts so em-dashes, arrows and box-drawing
# characters in the diagrams render. Falls back to the built-in
# Helvetica/Courier if a font file is missing.
BODY_FONT, MONO_FONT = "Helvetica", "Courier"
_F = "C:/Windows/Fonts/"
try:
    pdfmetrics.registerFont(TTFont("Segoe", _F + "segoeui.ttf"))
    pdfmetrics.registerFont(TTFont("Segoe-Bold", _F + "segoeuib.ttf"))
    pdfmetrics.registerFontFamily("Segoe", normal="Segoe", bold="Segoe-Bold",
                                  italic="Segoe", boldItalic="Segoe-Bold")
    BODY_FONT = "Segoe"
except Exception as e:
    print("[font] body fallback to Helvetica:", e)
try:
    pdfmetrics.registerFont(TTFont("Mono", _F + "consola.ttf"))
    pdfmetrics.registerFont(TTFont("Mono-Bold", _F + "consolab.ttf"))
    pdfmetrics.registerFontFamily("Mono", normal="Mono", bold="Mono-Bold")
    MONO_FONT = "Mono"
except Exception as e:
    print("[font] mono fallback to Courier:", e)

# ── Character cleaning ────────────────────────────────────────────
# Colour emoji have no glyphs in any embeddable PDF font, so map the
# meaningful ones to text and strip the decorative ones. Dashes,
# arrows and box-drawing survive (the registered fonts cover them).
_REPLACE = {
    "🔒": "", "🔑": "", "🌍": "(public) ", "🏭": "", "📌": "", "🎯": "",
    "🔹": "- ", "✅": "[x] ", "⚠️": "! ", "⚠": "! ", "🟢": "", "🐙": "",
    "🌐": "", "📩": "", "📄": "", "📈": "", "🔍": "", "🎉": "", "🚀": "",
    "🧰": "", "▶": ">", "◀": "<", "▲": "^", "▼": "v", "●": "*", "■": "#",
    "₹": "Rs ", "✓": "Yes", "✦": "-", "✸": "-", "•": "-", "→": " -> ",
}
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF]"
)


def clean(text: str) -> str:
    for k, v in _REPLACE.items():
        text = text.replace(k, v)
    return _EMOJI_RE.sub("", text)


# ── Styling (xhtml2pdf-compatible CSS) ────────────────────────────
CSS = f"""
@page {{ size: a4; margin: 1.8cm 1.7cm 1.9cm 1.7cm; }}
body   {{ font-family: "{BODY_FONT}"; font-size: 9.5pt; color: #1f2937; line-height: 1.42; }}
h1     {{ font-family: "{BODY_FONT}"; font-size: 20pt; color: #4f46e5; margin: 14pt 0 6pt; }}
h2     {{ font-family: "{BODY_FONT}"; font-size: 14pt; color: #0f172a; margin: 12pt 0 4pt;
          border-bottom: 1pt solid #c7d2fe; padding-bottom: 2pt; }}
h3     {{ font-family: "{BODY_FONT}"; font-size: 11pt; color: #4338ca; margin: 9pt 0 3pt; }}
h4     {{ font-family: "{BODY_FONT}"; font-size: 10pt; color: #334155; margin: 7pt 0 2pt; }}
p, li  {{ font-size: 9.5pt; }}
a      {{ color: #4f46e5; text-decoration: none; }}
strong {{ color: #0f172a; }}
hr     {{ border: none; border-top: 0.6pt solid #cbd5e1; margin: 8pt 0; }}
table  {{ -pdf-keep-in-frame-mode: shrink; border: 0.5pt solid #cbd5e1; margin: 5pt 0; }}
th     {{ background-color: #eef2ff; color: #312e81; font-family: "{BODY_FONT}";
          font-size: 8.5pt; text-align: left; padding: 3pt 5pt; border: 0.5pt solid #cbd5e1; }}
td     {{ font-size: 8.5pt; padding: 3pt 5pt; border: 0.5pt solid #e2e8f0; vertical-align: top; }}
code   {{ font-family: "{MONO_FONT}"; font-size: 8pt; background-color: #eef2f7; color: #0b3b6f; }}
pre    {{ font-family: "{MONO_FONT}"; font-size: 7pt; color: #e2e8f0;
          background-color: #0f172a; padding: 7pt; margin: 6pt 0; }}
pre code {{ background-color: #0f172a; color: #e2e8f0; font-size: 7pt; }}
blockquote {{ color: #475569; border-left: 2pt solid #a5b4fc; padding-left: 8pt; margin: 5pt 0; }}
"""


def build():
    with open(SRC, encoding="utf-8") as f:
        md = clean(f.read())
    html_body = markdown.markdown(
        md, extensions=["tables", "fenced_code", "sane_lists"]
    )
    html = f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{html_body}</body></html>"
    with open(OUT, "wb") as out:
        result = pisa.CreatePDF(html, dest=out, encoding="utf-8")
    if result.err:
        print(f"[warn] xhtml2pdf reported {result.err} issue(s) but a PDF was still written.")
    size = os.path.getsize(OUT)
    print(f"Wrote {OUT} ({size/1024:.0f} KB)")


if __name__ == "__main__":
    build()
