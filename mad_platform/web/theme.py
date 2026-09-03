"""Shared visual design: one CSS token set and chart-rendering helpers
used by every customer- and reviewer-facing HTML surface (the report,
the scan form, the live status page, the SME review queue) -- fixed
templates, not something an LLM regenerates per run, so the same design
renders every time regardless of the underlying findings.

The severity donut and WCAG-principle bar chart are pure SVG/CSS, no
charting library -- they render identically in a downloaded or offline
report as they do live, matching the project's one-fixed-HTML-format
design already used for the report itself.
"""

from __future__ import annotations

import html as html_lib
import math

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,500;0,6..72,600;'
    '0,6..72,700;1,6..72,500&family=Public+Sans:ital,wght@0,400;0,500;0,600;0,700;0,800;'
    '1,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">'
)

THEME_CSS = """
:root {
  --ink: #12181A; --ink-soft: #3C4A49; --muted: #5B6B6A;
  --bg: #EDF2F1; --surface: #FFFFFF; --surface-alt: #E3ECE9; --border: #CBDAD6;
  --brand: #0B6E66; --brand-dark: #084F49; --brand-tint: #E1F0EE; --focus: #0B6E66;
  --crit: #C0152B; --crit-tint: #FDECEC;
  --high: #C2570A; --high-tint: #FDF1E6;
  --med:  #A67C00; --med-tint:  #FBF3DA;
  --low:  #47566B; --low-tint:  #EBEEF2;
  --ok:   #157A4F; --ok-tint:   #E4F5EC;
  --shadow: 0 1px 2px rgba(18,24,26,0.06), 0 8px 24px rgba(18,24,26,0.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ink: #EAF1EF; --ink-soft: #C7D6D3; --muted: #93A6A3;
    --bg: #0E1413; --surface: #161F1E; --surface-alt: #1D2827; --border: #2B3937;
    --brand: #3FBFAF; --brand-dark: #7FDCCF; --brand-tint: #16302C; --focus: #3FBFAF;
    --crit: #F2586A; --crit-tint: #3A1518;
    --high: #F0954C; --high-tint: #3A2412;
    --med:  #E3BE3D; --med-tint:  #362B0C;
    --low:  #9FB2C4; --low-tint:  #202A33;
    --ok:   #57D79A; --ok-tint:   #10301F;
    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 8px 24px rgba(0,0,0,0.35);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink); min-height: 100vh;
  font-family: "Public Sans", -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.5;
}
.mono, code { font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace; }
a { color: var(--brand-dark); }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; border-radius: 3px; }

.page { max-width: 640px; margin: 0 auto; padding: 60px 24px; }
.page.with-site-header { padding-top: 44px; }
.page.wide { max-width: 900px; }
.serif { font-family: "Newsreader", Georgia, serif; }
h1 { font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 32px; margin: 0 0 10px; letter-spacing: -0.01em; text-wrap: balance; word-break: break-word; }
h2 { font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 21px; margin: 0 0 10px; letter-spacing: -0.005em; }
.tagline { color: var(--muted); font-size: 15px; margin-bottom: 32px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 28px; box-shadow: var(--shadow); }

/* ---- site-wide header + footer, every page shell uses these ---- */
.brand { font-size: 12px; letter-spacing: 0.09em; text-transform: uppercase; color: var(--brand-dark); font-weight: 800; display: flex; align-items: center; gap: 7px; }
.brand .dot-b { width: 7px; height: 7px; border-radius: 50%; background: var(--brand); flex-shrink: 0; box-shadow: 0 0 0 3px var(--brand-tint); }
.site-header {
  border-bottom: 1px solid var(--border); background: var(--surface);
}
.site-header-inner {
  max-width: 900px; margin: 0 auto; padding: 18px 24px;
  display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap;
}
.site-header-inner a.brand { text-decoration: none; }
.site-nav { display: flex; align-items: center; gap: 22px; list-style: none; margin: 0; padding: 0; }
.site-nav a { color: var(--ink-soft); text-decoration: none; font-size: 13.5px; font-weight: 600; }
.site-nav a:hover { color: var(--brand-dark); }
.site-nav a.active { color: var(--brand-dark); }
.site-nav a.cta { background: var(--brand); color: #fff; padding: 8px 16px; border-radius: 7px; }
.site-nav a.cta:hover { opacity: 0.92; color: #fff; }
.site-footer { border-top: 1px solid var(--border); margin-top: 64px; }
.site-footer-inner {
  max-width: 900px; margin: 0 auto; padding: 28px 24px 40px;
  display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  font-size: 12.5px; color: var(--muted);
}
.site-footer nav { display: flex; gap: 18px; flex-wrap: wrap; }
.site-footer a { color: var(--muted); text-decoration: none; }
.site-footer a:hover { color: var(--brand-dark); }

/* ---- landing v2: scanner-forward layout ---- */
.ribbon {
  background: var(--brand-tint); border-bottom: 1px solid var(--border);
  padding: 14px 24px; text-align: center;
}
.ribbon p { margin: 0; font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 18px; color: var(--ink); }
.scan-section { max-width: 720px; margin: 0 auto; padding: 44px 24px 8px; }
.scan-card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; }
.scan-card-head h1 { font-size: 26px; margin: 0; }
.mad-lockup.centered { text-align: center; margin: 30px auto 0; }

.section { max-width: 980px; margin: 0 auto; padding: 56px 24px; }
.section-head { text-align: center; margin-bottom: 36px; }
.section-head h2 { font-size: 26px; margin-bottom: 8px; }
.section-head p { color: var(--muted); font-size: 14.5px; margin: 0; }
.section.alt { background: var(--surface); border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.section.alt > .section-head, .section.alt > .how-visual, .section.alt > .stats-grid { max-width: 980px; margin-left: auto; margin-right: auto; }

/* how it works: real screenshots, numbered */
.how-visual { display: flex; align-items: flex-start; justify-content: center; gap: 56px; flex-wrap: wrap; }
.how-step { flex: 1; min-width: 280px; max-width: 400px; text-align: center; }
.how-step .shot-frame {
  position: relative; border-radius: 10px; border: 1px solid var(--border); background: var(--surface);
  box-shadow: var(--shadow); margin-bottom: 16px;
}
.how-step img { width: 100%; display: block; border-radius: 10px; overflow: hidden; }
.how-step .step-badge {
  position: absolute; top: -14px; left: -14px; width: 34px; height: 34px; border-radius: 50%;
  background: var(--brand); color: #fff; display: flex; align-items: center; justify-content: center;
  font-family: "Newsreader", Georgia, serif; font-weight: 700; font-size: 16px; box-shadow: var(--shadow);
  z-index: 2;
}
.how-step h3 { font-size: 16px; margin: 0; }
.how-arrow { align-self: center; color: var(--border-strong); font-size: 22px; margin-top: 70px; }
@media (max-width: 760px) { .how-arrow { display: none; } }

/* why it matters: charts */
.stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 32px; align-items: start; margin-bottom: 40px; }
.stat-panel { text-align: center; }
.stat-panel .panel-label { font-family: "JetBrains Mono", monospace; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 14px; }
.pie-figure { display: flex; flex-direction: column; align-items: center; }
.pie-figure .pie-caption { margin-top: 12px; font-size: 13px; color: var(--ink-soft); }
.bar-figure { display: flex; align-items: flex-end; justify-content: center; gap: 28px; height: 140px; margin-bottom: 12px; }
.bar-figure .bar-col { display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
.bar-figure .bar { width: 44px; border-radius: 6px 6px 0 0; }
.bar-figure .bar-val { font-family: "JetBrains Mono", monospace; font-size: 12.5px; font-weight: 700; margin-bottom: 6px; }
.bar-figure .bar-lbl { font-size: 11.5px; color: var(--muted); margin-top: 8px; }
.bignum-figure { display: flex; align-items: center; justify-content: center; gap: 18px; height: 140px; }
.bignum-figure .bn { text-align: center; }
.bignum-figure .bn b { display: block; font-family: "Newsreader", Georgia, serif; font-size: 34px; line-height: 1; }
.bignum-figure .bn.bad b { color: var(--crit); }
.bignum-figure .bn.good b { color: var(--brand-dark); }
.bignum-figure .bn span { font-size: 11.5px; color: var(--muted); display: block; margin-top: 6px; }
.bignum-figure .vs { color: var(--muted); font-size: 12px; font-family: "JetBrains Mono", monospace; }
.stats-quote {
  max-width: 640px; margin: 0 auto; text-align: center; font-family: "Newsreader", Georgia, serif;
  font-style: italic; font-size: 18px; line-height: 1.55; color: var(--ink-soft);
}

/* ---- landing hero ---- */
.hero-band {
  background:
    radial-gradient(ellipse 900px 420px at 15% -10%, var(--brand-tint), transparent),
    var(--bg);
  border-bottom: 1px solid var(--border);
  position: relative; overflow: hidden;
}
.hero-band::after {
  /* the "scan line" -- a quiet nod to what the tool actually does, not decoration for its own sake */
  content: ""; position: absolute; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--brand), transparent);
  animation: scan-sweep 5s ease-in-out infinite; opacity: 0.55;
}
@media (prefers-reduced-motion: reduce) { .hero-band::after { animation: none; top: 40%; } }
@keyframes scan-sweep { 0%, 100% { top: 8%; } 50% { top: 92%; } }
.hero-inner { max-width: 1080px; margin: 0 auto; padding: 56px 24px 48px; position: relative; }
.hero-split { display: grid; grid-template-columns: 1.1fr 1fr; gap: 48px; align-items: center; }
.hero-shot { position: relative; }
.hero-shot img {
  width: 100%; border-radius: 12px; border: 1px solid var(--border);
  box-shadow: 0 20px 50px -12px rgba(11,110,102,0.28), var(--shadow);
  transform: rotate(1.2deg);
}
.mad-lockup { font-size: 15px; color: var(--ink-soft); margin: -4px 0 24px; }
.mad-lockup .hl { color: var(--brand-dark); font-weight: 800; }
.mad-lockup .sub { display: block; font-size: 12px; color: var(--muted); margin-top: 2px; }
@media (max-width: 860px) { .hero-split { grid-template-columns: 1fr; } .hero-shot img { transform: none; } }
.hero-eyebrow {
  display: inline-flex; align-items: center; gap: 7px; font-family: "JetBrains Mono", monospace;
  font-size: 11.5px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--brand-dark);
  background: var(--brand-tint); border: 1px solid var(--brand); border-radius: 999px; padding: 5px 13px 5px 10px;
  margin-bottom: 18px;
}
.hero-inner h1 { font-size: 40px; max-width: 18ch; margin-bottom: 16px; }
.hero-lede { font-family: "Newsreader", Georgia, serif; font-style: italic; font-size: 19px; line-height: 1.55; color: var(--ink-soft); max-width: 58ch; margin-bottom: 0; }
.stat-row { display: flex; gap: 28px; flex-wrap: wrap; margin-top: 32px; }
.stat-row .stat b { display: block; font-family: "Newsreader", Georgia, serif; font-size: 26px; color: var(--brand-dark); line-height: 1; }
.stat-row .stat span { font-size: 12px; color: var(--muted); }
.how-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin: 40px 0 8px; }
.how-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
.how-card .step-n { font-family: "JetBrains Mono", monospace; font-size: 11px; color: var(--brand-dark); font-weight: 700; margin-bottom: 8px; }
.how-card h3 { font-size: 14.5px; margin: 0 0 6px; font-weight: 700; }
.how-card p { font-size: 12.5px; color: var(--muted); margin: 0; line-height: 1.5; }

/* ---- numbered trust/FAQ/terms lists ---- */
.trust-list { list-style: none; counter-reset: trust-item; margin: 0; padding: 0; }
.trust-list > li {
  counter-increment: trust-item; position: relative; padding: 0 0 22px 44px; margin-bottom: 22px;
  border-bottom: 1px solid var(--border);
}
.trust-list > li:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
.trust-list > li::before {
  content: counter(trust-item); position: absolute; left: 0; top: 1px;
  font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 17px; color: var(--brand-dark);
  width: 28px; height: 28px; border-radius: 50%; background: var(--brand-tint);
  display: flex; align-items: center; justify-content: center;
}
.trust-list h3 { font-size: 15px; margin: 0 0 6px; font-weight: 700; }
.trust-list p { margin: 0; color: var(--ink-soft); font-size: 14px; line-height: 1.6; }
.trust-section-label {
  font-family: "JetBrains Mono", monospace; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted); margin: 34px 0 16px 44px;
}
.trust-section-label:first-child { margin-top: 0; }
@media (max-width: 680px) { .how-row { grid-template-columns: 1fr; } .hero-inner h1 { font-size: 32px; } }

label.f-label { display: block; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 700; margin-bottom: 6px; }
input[type=url], input[type=password] {
  width: 100%; padding: 12px 14px; font-size: 15px; border: 1px solid var(--border); background: var(--bg);
  color: var(--ink); border-radius: 8px; margin-bottom: 14px; font-family: inherit;
}
button, .btn {
  display: inline-flex; align-items: center; gap: 6px; background: var(--brand); color: #fff; border: none;
  padding: 12px 20px; font-size: 15px; font-weight: 700; border-radius: 8px; cursor: pointer; text-decoration: none;
  font-family: inherit;
}
button:hover, .btn:hover { opacity: 0.92; }
.btn-secondary, .btn.ghost { background: transparent; color: var(--brand-dark); border: 1.5px solid var(--brand); }
.error-box { background: var(--crit-tint); border: 1px solid var(--crit); color: var(--crit); border-radius: 8px; padding: 14px 18px; margin-top: 16px; }
.success-box { background: var(--ok-tint); border: 1px solid var(--ok); color: var(--ok); border-radius: 8px; padding: 14px 18px; margin-top: 16px; }

.badge {
  display: inline-flex; align-items: center; gap: 5px; padding: 4px 11px; border-radius: 999px; font-size: 11px;
  font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap; font-family: "JetBrains Mono", monospace;
}
.sev-critical { background: var(--crit-tint); color: var(--crit); }
.sev-high { background: var(--high-tint); color: var(--high); }
.sev-medium { background: var(--med-tint); color: var(--med); }
.sev-low { background: var(--low-tint); color: var(--low); }
.sev-ok { background: var(--ok-tint); color: var(--ok); }
.sev-pending { background: var(--med-tint); color: var(--med); }

.score-dial {
  width: 84px; height: 84px; border-radius: 50%; flex-shrink: 0;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  border: 5px solid; font-variant-numeric: tabular-nums;
}
.score-dial .n { font-size: 26px; font-weight: 800; line-height: 1; }
.score-dial .l { font-size: 9px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); margin-top: 2px; }

.dash-row { display: flex; gap: 16px; margin: 0 0 22px; flex-wrap: wrap; }
.dash-card { flex: 1; min-width: 220px; background: var(--surface-alt); border-radius: 10px; padding: 18px 20px; }
.dash-card .dc-title { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 700; margin-bottom: 12px; }
.dash-score { display: flex; align-items: center; gap: 16px; min-height: 84px; }
.dash-score .score-note { font-size: 12.5px; color: var(--ink-soft); }
.donut-wrap { display: flex; align-items: center; gap: 16px; }
.donut-legend { list-style: none; margin: 0; padding: 0; font-size: 12.5px; display: flex; flex-direction: column; gap: 6px; }
.donut-legend li { display: flex; align-items: center; gap: 7px; }
.donut-legend b { margin-left: auto; font-family: "JetBrains Mono", monospace; font-variant-numeric: tabular-nums; padding-left: 10px; }
.lg-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.cat-chart { display: flex; flex-direction: column; gap: 9px; justify-content: center; min-height: 84px; }
.cat-row { display: grid; grid-template-columns: 92px 1fr 22px; align-items: center; gap: 9px; font-size: 12px; }
.cat-lbl { color: var(--ink-soft); }
.cat-bar-track { background: var(--border); border-radius: 4px; height: 8px; overflow: hidden; }
.cat-bar-fill { height: 100%; border-radius: 4px; background: var(--brand); }
.cat-n { font-family: "JetBrains Mono", monospace; font-size: 11.5px; text-align: right; color: var(--muted); }

.summary-box { background: var(--brand-tint); border: 1px solid var(--border); border-radius: 10px; padding: 18px 22px; margin-bottom: 24px; }
.summary-box .lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--brand-dark); font-weight: 800; margin-bottom: 6px; }
.summary-box p { margin: 0; font-size: 14.5px; color: var(--ink-soft); }

.findings-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.findings-table th { text-align: left; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); padding: 0 10px 9px; border-bottom: 1px solid var(--border); }
.findings-table th.num, .findings-table td.num { text-align: right; }
.findings-table td { padding: 12px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
.findings-table tr:last-child td { border-bottom: none; }
.findings-table td.rail { padding: 0; width: 4px; }
.findings-table td.rail span { display: block; width: 4px; height: 100%; min-height: 32px; border-radius: 2px; }
.findings-table .finding-title { font-weight: 700; }
.findings-table .finding-detail { color: var(--ink-soft); font-size: 12.5px; margin-top: 4px; max-width: 360px; }
.findings-table .fix-cell { font-family: "JetBrains Mono", monospace; font-size: 11.5px; color: var(--ink-soft); max-width: 240px; }
.empty { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 24px; text-align: center; color: var(--muted); }

.meta-line { color: var(--muted); font-size: 13.5px; margin: 0 0 16px; }
.actions { display: flex; gap: 10px; margin-top: 20px; flex-wrap: wrap; }

.stage-list { list-style: none; padding: 0; margin: 18px 0 0; }
.stage-list li { padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 13.5px; display: flex; justify-content: space-between; }
.stage-list li:last-child { border-bottom: none; }
.stg-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 9px; }
.spinner { display: inline-block; width: 15px; height: 15px; border: 2px solid var(--border); border-top-color: var(--brand); border-radius: 50%; animation: spin 0.8s linear infinite; vertical-align: middle; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }

.q-list { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.q-list th { text-align: left; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); padding: 0 12px 10px; border-bottom: 1px solid var(--border); }
.q-list td { padding: 13px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.q-list tr:last-child td { border-bottom: none; }
.field { margin: 10px 0; font-size: 14px; }
.field b { color: var(--ink); }

footer.note { max-width: 900px; margin: 32px auto 0; padding: 0 24px 40px; color: var(--muted); font-size: 12px; }
"""

_PRINCIPLE_BY_DIGIT = {"1": "Perceivable", "2": "Operable", "3": "Understandable", "4": "Robust"}
_PRINCIPLE_ORDER = ["Perceivable", "Operable", "Understandable", "Robust"]
_SEVERITY_ORDER = ["critical", "high", "medium", "low"]
SEVERITY_VAR = {"critical": "var(--crit)", "high": "var(--high)", "medium": "var(--med)", "low": "var(--low)"}


def wcag_principle(criterion: str) -> str:
    """The leading digit of a WCAG success-criterion number maps to one of
    the four POUR principles -- e.g. "4.1.2 Name, Role, Value" -> Robust.
    Falls back to "Other" for anything that doesn't parse as expected,
    rather than raising, since this only feeds a chart, not a decision.
    """
    digit = criterion.strip()[:1]
    return _PRINCIPLE_BY_DIGIT.get(digit, "Other")


def principle_counts(criteria: list[str]) -> dict[str, int]:
    counts = {p: 0 for p in _PRINCIPLE_ORDER}
    for c in criteria:
        p = wcag_principle(c)
        counts[p] = counts.get(p, 0) + 1
    return counts


def severity_donut_svg(counts: dict[str, int]) -> str:
    """Segments always render in fixed critical->high->medium->low order
    regardless of which counts are zero, so the ring and the legend below
    it never disagree on ordering.
    """
    total = sum(counts.get(s, 0) for s in _SEVERITY_ORDER)
    r = 40
    circumference = 2 * math.pi * r
    if total == 0:
        circles = f'<circle cx="48" cy="48" r="{r}" fill="none" stroke="var(--border)" stroke-width="14"/>'
    else:
        parts = [f'<circle cx="48" cy="48" r="{r}" fill="none" stroke="var(--border)" stroke-width="14"/>']
        offset = 0.0
        for sev in _SEVERITY_ORDER:
            count = counts.get(sev, 0)
            if count == 0:
                continue
            length = (count / total) * circumference
            parts.append(
                f'<circle cx="48" cy="48" r="{r}" fill="none" stroke="{SEVERITY_VAR[sev]}" stroke-width="14" '
                f'stroke-dasharray="{length:.2f} {circumference - length:.2f}" stroke-dashoffset="{-offset:.2f}"/>'
            )
            offset += length
        circles = "".join(parts)
    legend = "".join(
        f'<li><span class="lg-dot" style="background:{SEVERITY_VAR[sev]}"></span>{sev.capitalize()}<b>{counts.get(sev, 0)}</b></li>'
        for sev in _SEVERITY_ORDER
    )
    return (
        f'<div class="donut-wrap"><svg width="88" height="88" viewBox="0 0 96 96" role="img" '
        f'aria-label="{total} findings by severity"><g transform="rotate(-90 48 48)">{circles}</g>'
        f'<text x="48" y="45" text-anchor="middle" font-family="JetBrains Mono, monospace" font-size="19" '
        f'font-weight="800" fill="var(--ink)">{total}</text>'
        f'<text x="48" y="59" text-anchor="middle" font-family="Public Sans, sans-serif" font-size="7.5" '
        f'fill="var(--muted)" letter-spacing="0.4">FINDINGS</text></svg>'
        f'<ul class="donut-legend">{legend}</ul></div>'
    )


def principle_bar_chart(counts: dict[str, int]) -> str:
    max_count = max(counts.values(), default=0) or 1
    rows = "".join(
        f'<div class="cat-row"><span class="cat-lbl">{p}</span>'
        f'<div class="cat-bar-track"><div class="cat-bar-fill" style="width:{counts.get(p, 0) / max_count * 100:.0f}%"></div></div>'
        f'<span class="cat-n">{counts.get(p, 0)}</span></div>'
        for p in _PRINCIPLE_ORDER
    )
    return f'<div class="cat-chart">{rows}</div>'


def score_dial(score: int, color: str) -> str:
    return f'<div class="score-dial" style="border-color:{color};color:{color}"><div class="n">{score}</div><div class="l">Score</div></div>'


def score_note(severity_counts: dict[str, int]) -> str:
    critical = severity_counts.get("critical", 0)
    high = severity_counts.get("high", 0)
    if critical:
        noun = "issue" if critical == 1 else "issues"
        verb = "needs" if critical == 1 else "need"
        return f"{critical} critical {noun} {verb} immediate attention."
    if high:
        return f"{high} high-severity issue{'s' if high != 1 else ''} found, nothing critical."
    if sum(severity_counts.values()):
        return "Only medium- and low-severity issues found."
    return "No confirmed findings on the pages checked."


def dashboard_row(score: int, score_color: str, severity_counts: dict[str, int], p_counts: dict[str, int]) -> str:
    return f"""<div class="dash-row">
  <div class="dash-card"><div class="dc-title">Site score</div>
    <div class="dash-score">{score_dial(score, score_color)}<div class="score-note">{html_lib.escape(score_note(severity_counts))}</div></div>
  </div>
  <div class="dash-card"><div class="dc-title">By severity</div>{severity_donut_svg(severity_counts)}</div>
  <div class="dash-card"><div class="dc-title">By WCAG principle</div>{principle_bar_chart(p_counts)}</div>
</div>"""
