"""
Render the six docs/training/*.md into one polished, branded PDF.

Pipeline:  Markdown -> HTML (python-markdown) -> PDF (xhtml2pdf / reportlab)
Run:  python docs/build_training_pdf.py

Note: Mermaid diagrams appear here as their source (fenced blocks); they render
visually in the web version (the published Artifact).
"""
import os
import re

import markdown
from xhtml2pdf import pisa
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
TDIR = os.path.join(HERE, "training")
OUT = os.path.join(TDIR, "AMP-Training-Package.pdf")

DOCS = [
    ("The Founder Technical Handbook", "AMP-FOUNDER-TECHNICAL-HANDBOOK.md"),
    ("Architecture Cheatsheet", "AMP-ARCHITECTURE-CHEATSHEET.md"),
    ("Module Map", "AMP-MODULE-MAP.md"),
    ("Data Flows", "AMP-DATA-FLOWS.md"),
    ("Code-Change Guide", "AMP-CODE-CHANGE-GUIDE.md"),
    ("Video Course Script", "AMP-VIDEO-COURSE-SCRIPT.md"),
]

# ── Fonts (Windows Unicode so em-dashes/arrows/box-drawing render) ──
BODY_FONT, MONO_FONT = "Helvetica", "Courier"
_F = "C:/Windows/Fonts/"
try:
    pdfmetrics.registerFont(TTFont("Segoe", _F + "segoeui.ttf"))
    pdfmetrics.registerFont(TTFont("Segoe-Bold", _F + "segoeuib.ttf"))
    pdfmetrics.registerFont(TTFont("Segoe-It", _F + "segoeuii.ttf"))
    pdfmetrics.registerFontFamily("Segoe", normal="Segoe", bold="Segoe-Bold",
                                  italic="Segoe-It", boldItalic="Segoe-Bold")
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

# ── Character cleaning ──
_REPLACE = {
    "✅": "[OK] ", "🟡": "[~] ", "❌": "[x] ", "⚠️": "! ", "⚠": "! ",
    "🎙️": "NARRATION: ", "🖥️": "SCREEN: ", "📊": "DIAGRAM: ",
    "🔒": "", "🔑": "", "🏭": "", "📌": "", "🎯": "", "🟢": "", "⚙️": "",
    "→": " -> ", "⇄": " <-> ", "↔": " <-> ", "⇒": " => ",
    "•": "- ", "▶": ">", "◀": "<", "●": "*", "■": "#", "◆": "-",
    "₹": "Rs ", "✓": "Yes", "✗": "No",
}
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF]"
)


def clean(text: str) -> str:
    for k, v in _REPLACE.items():
        text = text.replace(k, v)
    return _EMOJI_RE.sub("", text)


CSS = f"""
@page {{ size: a4; margin: 1.7cm 1.6cm 1.8cm 1.6cm; }}
body   {{ font-family: "{BODY_FONT}"; font-size: 9.3pt; color: #171a1f; line-height: 1.44; }}
h1     {{ font-family: "{BODY_FONT}"; font-size: 19pt; color: #b25e09; margin: 12pt 0 6pt; }}
h2     {{ font-family: "{BODY_FONT}"; font-size: 13.5pt; color: #0f172a; margin: 12pt 0 4pt;
          border-bottom: 1pt solid #e6c9a3; padding-bottom: 2pt; }}
h3     {{ font-family: "{BODY_FONT}"; font-size: 10.8pt; color: #8c4906; margin: 9pt 0 3pt; }}
h4     {{ font-family: "{MONO_FONT}"; font-size: 9.5pt; color: #334155; margin: 7pt 0 2pt; }}
p, li  {{ font-size: 9.3pt; }}
a      {{ color: #8c4906; text-decoration: none; }}
strong {{ color: #0f172a; }}
em     {{ font-style: italic; color: #3b4250; }}
hr     {{ border: none; border-top: 0.6pt solid #d7dbe1; margin: 8pt 0; }}
table  {{ -pdf-keep-in-frame-mode: shrink; border: 0.5pt solid #d7dbe1; margin: 5pt 0; }}
th     {{ background-color: #faf3ea; color: #7a3f05; font-family: "{BODY_FONT}";
          font-size: 8.3pt; text-align: left; padding: 3pt 5pt; border: 0.5pt solid #e6d6c2; }}
td     {{ font-size: 8.3pt; padding: 3pt 5pt; border: 0.5pt solid #e6e8ec; vertical-align: top; }}
code   {{ font-family: "{MONO_FONT}"; font-size: 7.8pt; background-color: #f2ede6; color: #8c4906; }}
pre    {{ font-family: "{MONO_FONT}"; font-size: 7pt; color: #e6e9ef;
          background-color: #10141b; padding: 7pt; margin: 6pt 0; }}
pre code {{ background-color: #10141b; color: #e6e9ef; font-size: 7pt; }}
blockquote {{ color: #475569; border-left: 2pt solid #d89a4a; padding-left: 8pt; margin: 5pt 0; }}
.cover {{ margin-top: 5cm; text-align: center; }}
.cover h1 {{ font-size: 30pt; border: 0; color: #b25e09; }}
.cover .sub {{ font-size: 12pt; color: #475569; margin-top: 6pt; }}
.cover .meta {{ font-family: "{MONO_FONT}"; font-size: 9pt; color: #64748b; margin-top: 22pt; }}
.cover ol {{ display: inline-block; text-align: left; margin-top: 20pt; color: #334155; }}
.brk {{ page-break-before: always; }}
"""


def build():
    parts = [
        '<div class="cover">'
        '<h1>AMP</h1>'
        '<div class="sub">Founder Technical Training Package</div>'
        '<div class="meta">Source of truth: commit 0eb94ca (master) &nbsp;|&nbsp; six documents</div>'
        '<ol><li>The Founder Technical Handbook (31 chapters)</li>'
        '<li>Architecture Cheatsheet</li><li>Module Map</li>'
        '<li>Data Flows</li><li>Code-Change Guide</li>'
        '<li>Video Course Script</li></ol>'
        '<div class="meta">Diagrams render visually in the web edition; shown here as source.</div>'
        '</div>'
    ]
    for i, (title, fname) in enumerate(DOCS):
        path = os.path.join(TDIR, fname)
        with open(path, encoding="utf-8") as f:
            md = clean(f.read())
        body = markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])
        parts.append(f'<div class="brk"></div>{body}')
        print(f"  + {fname} ({len(md)} chars)")

    html = (f"<html><head><meta charset='utf-8'><style>{CSS}</style></head>"
            f"<body>{''.join(parts)}</body></html>")
    with open(OUT, "wb") as out:
        result = pisa.CreatePDF(html, dest=out, encoding="utf-8")
    if result.err:
        print(f"[warn] xhtml2pdf reported {result.err} issue(s); a PDF was still written.")
    print(f"Wrote {OUT} ({os.path.getsize(OUT)/1024:.0f} KB)")


if __name__ == "__main__":
    build()
