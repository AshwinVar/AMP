"""
Render the AMP sales two-pager (UK market, AI-layer positioning) to a branded PDF.
Pipeline: inline HTML -> PDF (xhtml2pdf / reportlab).
Run:  python docs/build_sales_pitch.py
"""
import os
from xhtml2pdf import pisa
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sales", "AMP-Sales-Pitch.pdf")

BODY, MONO = "Helvetica", "Courier"
_F = "C:/Windows/Fonts/"
try:
    pdfmetrics.registerFont(TTFont("Segoe", _F + "segoeui.ttf"))
    pdfmetrics.registerFont(TTFont("Segoe-Bold", _F + "segoeuib.ttf"))
    pdfmetrics.registerFont(TTFont("Segoe-It", _F + "segoeuii.ttf"))
    pdfmetrics.registerFontFamily("Segoe", normal="Segoe", bold="Segoe-Bold",
                                  italic="Segoe-It", boldItalic="Segoe-Bold")
    BODY = "Segoe"
except Exception as e:
    print("[font] body fallback:", e)
try:
    pdfmetrics.registerFont(TTFont("Mono", _F + "consola.ttf"))
    MONO = "Mono"
except Exception as e:
    print("[font] mono fallback:", e)

CSS = """
@page { size: a4; margin: 1.25cm 1.35cm 1.1cm 1.35cm; }
body { font-family: "__BODY__"; font-size: 9.3pt; color: #1f2530; line-height: 1.38; }
.band { background-color: #14161a; color: #ffffff; padding: 12pt 15pt; }
.band .logo { font-size: 22pt; color: #ffffff; letter-spacing: 0.5pt; }
.band .logo b { color: #f5a524; }
.band .kick { color: #f5a524; font-size: 8pt; letter-spacing: 1.4pt; }
.band .tag { font-size: 12pt; color: #e7e9ec; margin-top: 3pt; }
h2 { font-size: 10.8pt; color: #a24f06; margin: 10pt 0 3pt; padding-bottom: 2pt;
     border-bottom: 1.2pt solid #f0d9b8; }
p { margin: 3pt 0; }
ul { margin: 2pt 0 2pt 2pt; padding-left: 12pt; }
li { margin: 1.6pt 0; font-size: 9.1pt; }
strong { color: #14161a; }
em { color: #4a5262; }
.two td { width: 50%; vertical-align: top; padding-right: 8pt; }
/* maturity ladder */
table.ladder { margin: 5pt 0 2pt; border: 0.6pt solid #e3d3bd; }
table.ladder td { width: 20%; vertical-align: top; text-align: center; padding: 5pt 4pt;
     border-right: 0.5pt solid #efe6d8; font-size: 7.6pt; }
table.ladder .st { color: #14161a; font-size: 8.4pt; }
table.ladder .d { color: #5a6270; }
.chip { font-size: 6.8pt; letter-spacing: 0.5pt; padding: 1pt 4pt; }
.live { color: #ffffff; background-color: #2c8a3e; }
.next { color: #ffffff; background-color: #a24f06; }
.later { color: #55606f; background-color: #edeff2; }
.cap { color: #8a92a0; font-size: 7.4pt; letter-spacing: 1pt; }
/* pricing */
table.price { border: 0.6pt solid #e3d3bd; margin: 4pt 0; }
table.price th { background-color: #faf3ea; color: #7a3f05; text-align: left;
     font-size: 8.5pt; padding: 4pt 7pt; border-bottom: 0.6pt solid #e3d3bd; }
table.price td { font-size: 8.6pt; padding: 4pt 7pt; border-bottom: 0.5pt solid #efe6d8; vertical-align: top; }
table.price .plan { color: #14161a; }
table.price .amt { color: #a24f06; white-space: nowrap; }
.note { color: #6b7280; font-size: 8.3pt; margin-top: 2pt; }
.cta { background-color: #fbf3e8; border: 1pt solid #e9cfa8; padding: 9pt 12pt; margin-top: 8pt; }
.cta .big { font-size: 10.6pt; color: #14161a; }
.cta .line { font-size: 9.2pt; color: #7a3f05; font-style: italic; margin-top: 3pt; }
.foot { margin-top: 6pt; color: #4a5262; font-size: 8.5pt; }
.foot b { color: #14161a; }
.ok { color: #2c8a3e; }
"""

HTML = """<html><head><meta charset="utf-8"><style>__CSS__</style></head><body>

<div class="band">
  <div class="kick">A MARX8 PLATFORM &nbsp;&middot;&nbsp; THE AI LAYER FOR MANUFACTURING</div>
  <div class="logo"><b>AMP</b></div>
  <div class="tag">The intelligence layer that runs on top of your MES &mdash; and a full MES, if you need one.</div>
</div>

<h2>What AMP is</h2>
<p>AMP is the <strong>AI layer that sits on top of the MES layer</strong> in your factory. It takes a
system that only <em>records and executes</em> production and makes it <strong>predict, prioritise and
act</strong>. Run AMP on top of the MES you already have, or use AMP&rsquo;s <strong>built-in MES</strong>
&mdash; either way you get the intelligence layer manufacturing has been missing, from one dashboard on
any device.</p>

<h2>What today&rsquo;s MES systems are missing</h2>
<p>Traditional MES &mdash; Siemens, SAP and the rest &mdash; tell you a machine <em>is</em> down. They
don&rsquo;t tell you it will fail next week, they don&rsquo;t rank the loss costing you most, and they
don&rsquo;t act. They are systems of <strong>record</strong>, not systems of <strong>decision</strong>.
That missing piece &mdash; <strong>smart, cognitive, self-optimising manufacturing</strong> &mdash; is
exactly what AMP adds on top.</p>

<h2>The AI layer AMP adds</h2>
<table class="two"><tr><td>
<ul>
<li><strong>Predictive</strong> &mdash; risk-scores every machine and flags trouble before it stops the line.</li>
<li><strong>Cognitive insight</strong> &mdash; ranks your biggest losses in &pound; and gives a morning briefing of what to fix first.</li>
</ul>
</td><td>
<ul>
<li><strong>Autonomous agents</strong> &mdash; watch the floor and <em>propose</em> actions (reorder, maintenance, escalation); your team approves. The factory starts running itself, under human control.</li>
<li><strong>AI copilot</strong> &mdash; ask your factory questions in plain English; answers come from live data.</li>
</ul>
</td></tr></table>

<h2>Full MES included</h2>
<p>No need to buy two systems. AMP brings <strong>real-time machine monitoring &amp; OEE</strong>,
<strong>work orders &amp; BOM</strong> (completing a job auto-moves material and finished goods),
<strong>smart inventory</strong> and <strong>quality &amp; maintenance</strong> &mdash; live, multi-tenant
and secure, with role-based access for Admin, Supervisor and Operator, and full traceability per batch and machine.</p>

<h2>How it works</h2>
<p><strong>1. Connect</strong> &mdash; sit AMP on top of your existing MES/ERP, or stream machines directly
over standard IoT (MQTT); start with tablet or manual entry if you prefer &mdash; no rip-out. &nbsp;
<strong>2. See</strong> &mdash; one live dashboard across every plant. &nbsp; <strong>3. Act</strong> &mdash;
AI prioritises the biggest money-losers and proposes fixes; your team approves. Cloud-hosted and secure,
with each company&rsquo;s data walled off from every other.</p>

<h2>Where AMP is going &mdash; the climb</h2>
<table class="ladder"><tr>
<td><div class="st"><b>MES</b></div><div class="d">Execute &amp; record</div><div><span class="chip live">LIVE</span></div></td>
<td><div class="st"><b>Smart Factory</b></div><div class="d">Connected + AI decisions</div><div><span class="chip live">LIVE</span></div></td>
<td><div class="st"><b>Autonomous</b></div><div class="d">AI acts on the data</div><div><span class="chip next">NEXT</span></div></td>
<td><div class="st"><b>Cognitive</b></div><div class="d">A self-learning plant</div><div><span class="chip later">ROADMAP</span></div></td>
<td><div class="st"><b>Lights-Out</b></div><div class="d">Runs in the dark</div><div><span class="chip later">VISION</span></div></td>
</tr></table>
<div class="cap">TODAY &nbsp;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&mdash;&rarr; &nbsp;THE ROADMAP</div>
<p style="margin-top:5pt"><strong>Shipping next:</strong> learned predictive models (rules &rarr; trained ML),
direct machine/PLC connectivity via edge agents (OPC-UA, Modbus, Siemens S7), wider agent autonomy under
your policy, a deeper digital twin, and an energy &amp; sustainability module. AMP is built as a platform,
so each new capability simply switches on &mdash; never a rip-and-replace.</p>

<h2>Built to be trusted</h2>
<ul>
<li><span class="ok">&#10003;</span> Role-based access, encrypted logins, a full audit trail, daily <strong>restore-tested</strong> backups, migration-gated releases.</li>
<li><span class="ok">&#10003;</span> Multi-tenant isolation is <strong>adversarially tested</strong> &mdash; one factory can never see another&rsquo;s data.</li>
<li><span class="ok">&#10003;</span> <strong>Honest about today vs tomorrow:</strong> today&rsquo;s intelligence is rule-based automation, an AI copilot and approve-first agents; trained ML and direct PLC links are on the roadmap above. What you use today is live &mdash; already in an early pilot with a compressor manufacturer.</li>
</ul>

<h2>Pricing &mdash; UK <span class="note">(per plant / month, billed monthly)</span></h2>
<table class="price">
<tr><th>Plan</th><th>Price</th><th>What&rsquo;s included</th></tr>
<tr><td class="plan">Starter</td><td class="amt">&pound;299</td><td>Real-time monitoring, downtime &amp; OEE, shift performance, live alerts, up to 5 users.</td></tr>
<tr><td class="plan">Growth</td><td class="amt">&pound;699</td><td>Everything in Starter + work orders &amp; planning, smart inventory &amp; purchasing, quality &amp; maintenance, the AI copilot &amp; agents, up to 15 users.</td></tr>
<tr><td class="plan">Enterprise</td><td class="amt">Custom</td><td>Everything in Growth + executive OEE &amp; AI insights, multiple plants, on-premise option, and priority onboarding &amp; support.</td></tr>
</table>
<p class="note">30-day free trial &middot; works on top of your existing MES or standalone &middot; month-to-month, your data stays yours.</p>

<div class="cta">
  <div class="big"><strong>See it on your own factory&rsquo;s numbers.</strong> Book a 30-minute demo, then run a low-risk, time-boxed pilot on one line.</div>
  <div class="line">Stop recording your factory. Start running it.</div>
</div>
<div class="foot"><b>MARX8</b> &nbsp;&middot;&nbsp; info@marx8.com &nbsp;&middot;&nbsp; app.marx8.com</div>

</body></html>"""


def build():
    html = HTML.replace("__CSS__", CSS).replace("__BODY__", BODY).replace("__MONO__", MONO)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        result = pisa.CreatePDF(html, dest=f, encoding="utf-8")
    if result.err:
        print(f"[warn] xhtml2pdf reported {result.err} issue(s); a PDF was still written.")
    print(f"Wrote {OUT} ({os.path.getsize(OUT)/1024:.0f} KB)")


if __name__ == "__main__":
    build()
