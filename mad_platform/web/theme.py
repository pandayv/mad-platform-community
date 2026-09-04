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
  --border-strong: #9FB6B1;
  --shadow: 0 1px 2px rgba(18,24,26,0.06), 0 8px 24px rgba(18,24,26,0.05);
  /* liquid-glass surface tokens: translucent panels over an ambient gradient,
     not flat opaque cards -- see body's background-image below for the field
     these surfaces actually refract. */
  --glass: rgba(255,255,255,0.6); --glass-strong: rgba(255,255,255,0.78);
  --glass-border: rgba(255,255,255,0.7); --glass-sheen: rgba(255,255,255,0.85);
  --glass-shadow: 0 1px 1px rgba(255,255,255,0.5) inset, 0 12px 40px -8px rgba(9,30,28,0.18), 0 2px 10px rgba(9,30,28,0.07);
  /* Sized in vmin, not px: fixed-pixel ellipses this large are ~85% of a
     1280px-wide desktop viewport but nearly 3x a 390px-wide phone's width,
     so the same background read as balanced multi-hue on desktop and
     almost solid green on mobile -- same CSS, wildly different result
     depending on aspect ratio. vmin scales with the smaller of the two
     viewport dimensions, so the blobs keep the same relative footprint
     (and balance against each other) on a narrow-tall phone and a
     wide-short laptop alike. */
  --ambient:
    radial-gradient(122vmin 69vmin at 8% -12%, rgba(11,110,102,0.20), transparent 60%),
    radial-gradient(100vmin 58vmin at 96% 6%, rgba(255,145,90,0.15), transparent 58%),
    radial-gradient(111vmin 76vmin at 46% 105%, rgba(94,132,255,0.12), transparent 60%);
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
    --border-strong: #3D4E4B;
    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 8px 24px rgba(0,0,0,0.35);
    --glass: rgba(26,36,35,0.6); --glass-strong: rgba(26,36,35,0.8);
    --glass-border: rgba(255,255,255,0.10); --glass-sheen: rgba(255,255,255,0.10);
    --glass-shadow: 0 1px 1px rgba(255,255,255,0.05) inset, 0 12px 40px -8px rgba(0,0,0,0.5), 0 2px 10px rgba(0,0,0,0.35);
    --ambient:
      radial-gradient(122vmin 69vmin at 8% -12%, rgba(63,191,175,0.22), transparent 60%),
      radial-gradient(100vmin 58vmin at 96% 6%, rgba(255,145,90,0.10), transparent 58%),
      radial-gradient(111vmin 76vmin at 46% 105%, rgba(94,132,255,0.14), transparent 60%);
  }
}
* { box-sizing: border-box; }
/* Defense in depth: this is the third distinct horizontal-overflow bug
   this session, each from a different root cause (a badge's flex sizing,
   a bar chart's fixed height, now a comparison table's min-width leaking
   past its own overflow:auto wrapper for reasons that resisted the
   obvious fix). Rather than keep chasing each new specific cause
   one at a time, this is a blanket safety net: nothing on this page is
   ever supposed to need horizontal page scroll, so don't allow it,
   regardless of what causes the next one. Intentionally on html, not
   body -- the specific bug this fixed showed document.documentElement's
   own scrollWidth/scrollX diverging from body's, so the guard needs to
   sit at the same level as the part that was actually scrollable. */
html { overflow-x: hidden; }
body {
  overflow-x: hidden;
  margin: 0; color: var(--ink); min-height: 100vh;
  background: var(--ambient), var(--bg);
  background-attachment: fixed;
  font-family: "Public Sans", -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.5;
}
/* Reusable glass-panel treatment. A pseudo-element carries the sheen so it
   can share the host's own border-radius without needing overflow:hidden on
   the host -- that clipped an absolutely-positioned badge before (see
   .step-badge below), so no glass panel here relies on parent clipping. */
.glass-sheen { position: relative; }
.glass-sheen::before {
  content: ""; position: absolute; inset: 0; border-radius: inherit;
  background: linear-gradient(135deg, var(--glass-sheen), transparent 45%);
  opacity: 0.5; pointer-events: none;
}
/* A slow, continuous sweep down the homepage -- a literal nod to what the
   product actually does, not decoration for its own sake. mix-blend-mode
   makes it genuinely tint whatever it crosses (page background, cards,
   text) rather than just sit on top of it, so "color shifts as it passes"
   is real, not simulated per-element. Fixed to the viewport (not the
   document) so it reads the same regardless of scroll position or how
   tall the page is. Off entirely under reduced-motion. */
.scan-beam {
  position: fixed; left: 0; right: 0; top: -220px; height: 220px; z-index: 30;
  pointer-events: none; mix-blend-mode: overlay;
  /* two layered gradients: a wide soft color band for the tint effect,
     plus a slim near-white core at its center so there's an actual bright
     line to track with the eye -- the first version was color-tint only,
     which read as barely-there. */
  background:
    linear-gradient(180deg, transparent 46%, rgba(255,255,255,0.95) 49.5%, rgba(255,255,255,0.95) 50.5%, transparent 54%),
    linear-gradient(180deg, transparent, rgba(11,110,102,0.85) 40%, rgba(94,132,255,0.75) 60%, transparent);
  animation: scan-beam-sweep 9s ease-in-out infinite;
}
@keyframes scan-beam-sweep { 0%, 100% { top: -220px; } 50% { top: 100vh; } }
@media (prefers-reduced-motion: reduce) { .scan-beam { display: none; } }

.mono, code { font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace; }
a { color: var(--brand-dark); }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; border-radius: 3px; }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}

/* One full-viewport "screen" per landing section, so a laptop-height window
   shows exactly one section at a time instead of two-and-a-half at once.
   min-height (not height) so a section with more content than fits one
   screen still grows rather than clipping.

   Deliberately no scroll-snap: it was here in an earlier pass ("proximity,
   not mandatory, so it nudges rather than fights scrolling"), but in
   practice proximity snapping kept pulling the last section back up over
   the footer right after it, making the footer unreachable -- a real
   usability regression, not a subtle one. min-height alone already gets
   the one-section-per-screen result; the snap was a flourish on top that
   cost more than it added. */
.view { min-height: calc(100vh - 65px); display: flex; flex-direction: column; justify-content: center; }

.page { max-width: 640px; margin: 0 auto; padding: 60px 24px; }
.page.with-site-header { padding-top: 44px; }
.page.wide { max-width: 900px; }
.serif { font-family: "Newsreader", Georgia, serif; }
h1 { font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 32px; margin: 0 0 10px; letter-spacing: -0.01em; text-wrap: balance; word-break: break-word; }
h2 { font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 21px; margin: 0 0 10px; letter-spacing: -0.005em; }
.tagline { color: var(--muted); font-size: 15px; margin-bottom: 32px; }
.card {
  background: var(--glass-strong); border: 1px solid var(--glass-border); border-radius: 18px;
  padding: 28px; box-shadow: var(--glass-shadow);
  backdrop-filter: blur(28px) saturate(160%); -webkit-backdrop-filter: blur(28px) saturate(160%);
}

/* ---- site-wide header + footer, every page shell uses these ---- */
.brand { font-size: 12px; letter-spacing: 0.09em; text-transform: uppercase; color: var(--brand-dark); font-weight: 800; display: flex; align-items: center; gap: 7px; }
.brand .dot-b { width: 7px; height: 7px; border-radius: 50%; background: var(--brand); flex-shrink: 0; box-shadow: 0 0 0 3px var(--brand-tint); }
.site-header {
  position: sticky; top: 0; z-index: 40;
  border-bottom: 1px solid var(--glass-border); background: var(--glass-strong);
  backdrop-filter: blur(20px) saturate(160%); -webkit-backdrop-filter: blur(20px) saturate(160%);
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

/* ---- landing v3: Google-style hero, one clean scan bar, no card chrome ---- */
.hero-block { max-width: 640px; margin: 0 auto; padding: 24px 24px 40px; text-align: center; }
.hero-title {
  font-size: 52px; font-weight: 600; letter-spacing: -0.015em; margin: 0 0 14px;
  text-wrap: balance;
}
.hero-title-mark { font-weight: 800; color: var(--brand-dark); }
.hero-tagline { font-family: "Newsreader", Georgia, serif; font-weight: 500; font-size: 20px; color: var(--ink-soft); margin: 0; }

/* scroll-margin-top so a browser jumping to #scan (the header's "Scan a
   site" link, from another page) doesn't tuck it under the sticky header
   -- covers the header at both its one-line (~65px) and wrapped-to-two-
   lines (~95px, narrow phones) heights. */
.scan-section { max-width: 640px; margin: 0 auto; padding: 0 24px 8px; scroll-margin-top: 90px; }
.scan-form { display: flex; flex-direction: column; gap: 12px; }
.scan-field { position: relative; }
/* Compound selector (class + element + attribute) so this reliably beats
   the older global input[type=url] rule later in this file regardless of
   source order -- that equal-specificity, later-wins conflict is exactly
   why the URL field used to have a visible box and the email field next
   to it didn't (email isn't type=url, so it never matched that rule).
   Each field carries its own glass surface now -- no outer card wraps
   them, so the fields read as the page's only "boxed" elements, closer
   to how a search bar sits bare on its own page. */
.scan-field input[type=url], .scan-field input[type=email] {
  display: block; width: 100%; box-sizing: border-box; margin: 0;
  border: 1px solid var(--glass-border); background: var(--glass-strong); color: var(--ink);
  border-radius: 12px; padding: 13px 16px; font-size: 15.5px; font-family: inherit;
  box-shadow: var(--glass-shadow);
  backdrop-filter: blur(20px) saturate(160%); -webkit-backdrop-filter: blur(20px) saturate(160%);
}
.scan-field input::placeholder { color: var(--muted); }
.scan-field input:focus { outline: none; border-color: var(--brand); box-shadow: 0 0 0 3px var(--brand-tint); }
.scan-field:has(.info-tip) input[type=email] { padding-right: 44px; }
.scan-field .info-tip { position: absolute; right: 13px; top: 50%; transform: translateY(-50%); }
.scan-submit {
  align-self: flex-end; justify-content: center; font-size: 14px;
  border-radius: 10px !important; padding: 10px 20px !important; margin-top: 2px;
}
@media (max-width: 640px) { .hero-title { font-size: 38px; } .scan-submit { align-self: stretch; } }

/* tooltip: replaces a permanently-visible sentence of fine print next to
   the email field. Shows on hover AND focus (not hover-only) so it's
   reachable by keyboard, and the input itself carries the same text via
   aria-describedby so a screen reader user gets it without needing to
   trigger the tooltip at all. */
.info-tip { position: relative; display: inline-flex; margin-left: 4px; }
.tip-icon {
  width: 20px; height: 20px; border-radius: 50%; flex-shrink: 0;
  background: var(--surface-alt); border: 1px solid var(--border-strong); color: var(--muted);
  font-size: 11px; font-weight: 700; font-family: "JetBrains Mono", monospace;
  display: flex; align-items: center; justify-content: center; cursor: help;
}
.tip-text {
  position: absolute; bottom: calc(100% + 10px); right: -10px; width: 220px;
  background: var(--ink); color: var(--surface); font-size: 12.5px; line-height: 1.5; font-weight: 500;
  padding: 11px 13px; border-radius: 10px; box-shadow: var(--shadow); text-align: left;
  opacity: 0; transform: translateY(4px); pointer-events: none; transition: opacity 0.15s ease, transform 0.15s ease;
  z-index: 5;
}
.info-tip:hover .tip-text, .info-tip:focus-within .tip-text { opacity: 1; transform: translateY(0); }

/* No border here on purpose: a 1px divider under mix-blend-mode:overlay
   (the scan beam passes over this whole section) flares into a bright,
   glitchy-looking line whenever the beam crosses it -- confirmed by
   forcing the beam to that exact position. Whitespace alone (this much
   margin+padding) still reads as a clear separation from the form above
   without giving the beam anything to blow out. */
.mad-lockup.centered { text-align: center; margin-top: 40px; padding-top: 24px; }

.section { max-width: 980px; margin: 0 auto; padding: 56px 24px; }
.section-head { text-align: center; margin-bottom: 56px; }
.section-head h2 { font-size: 26px; margin-bottom: 8px; }
.section-head p { color: var(--muted); font-size: 14.5px; margin: 0; }

/* how it works: real screenshots, numbered */
.how-visual { display: flex; align-items: flex-start; justify-content: center; gap: 56px; flex-wrap: wrap; }
.how-step { flex: 1; min-width: 280px; max-width: 400px; text-align: center; }
.how-step .shot-frame {
  position: relative; border-radius: 18px; border: 1px solid var(--glass-border); background: var(--glass-strong);
  box-shadow: var(--glass-shadow); margin-bottom: 16px;
  backdrop-filter: blur(28px) saturate(160%); -webkit-backdrop-filter: blur(28px) saturate(160%);
}
.how-step img { width: 100%; display: block; border-radius: 14px; padding: 4px; }
.how-step .step-badge {
  position: absolute; top: -14px; left: -14px; width: 34px; height: 34px; border-radius: 50%;
  background: linear-gradient(160deg, color-mix(in srgb, var(--brand) 100%, white 25%), var(--brand-dark));
  color: #fff; display: flex; align-items: center; justify-content: center;
  font-family: "Newsreader", Georgia, serif; font-weight: 700; font-size: 16px;
  box-shadow: 0 1px 0 rgba(255,255,255,0.4) inset, var(--shadow);
  z-index: 2;
}
.how-step h3 { font-size: 16px; margin: 0; }

/* why it matters: charts presented as one integrated data strip, not
   three separate dashboard widgets -- a divider between columns instead
   of a card boundary around each, so the ambient background stays visible
   and the numbers themselves carry the weight instead of a box around them. */
.stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0; align-items: stretch; margin-bottom: 44px; }
.stat-panel { text-align: center; padding: 8px 28px; }
.stat-panel + .stat-panel { border-left: 1px solid var(--glass-border); }
@media (max-width: 760px) {
  .stats-grid { grid-template-columns: 1fr; gap: 36px; }
  .stat-panel + .stat-panel { border-left: none; border-top: 1px solid var(--glass-border); padding-top: 36px; }
}
/* Every panel gets the same two-row skeleton -- a fixed-height "topline"
   (empty for pie/bignum, the growth badge for the bar chart) then a
   fixed-height "visual" row -- so all three captions start at exactly the
   same y regardless of what each panel's own content needs. Matching
   pixel budgets across three different chart types was fragile (two
   separate bugs already came from exactly that); matching *structure*
   instead makes the alignment a guarantee, not an estimate. */
.stat-topline { height: 32px; display: flex; align-items: center; justify-content: center; margin-bottom: 8px; }
.stat-visual { height: 210px; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.pie-figure { display: flex; flex-direction: column; align-items: center; }
/* Was scoped ".pie-figure .pie-caption" -- an ancestor restriction that
   only the first panel's caption actually satisfies (bar/bignum captions
   are plain <p> siblings of .bar-figure/.bignum-figure, not descendants
   of .pie-figure), so the other two silently fell back to unstyled
   default paragraph text: bigger, darker, different margin -- exactly
   the "different font under each chart" the user was seeing. */
.pie-caption { margin-top: 14px; font-size: 13.5px; color: var(--ink-soft); }
/* The real year-over-year change is 23% -- honest, zero-baselined bars
   will always look fairly close in height at that gap, and exaggerating
   it with a truncated axis would be exactly the misleading-chart trick
   this site shouldn't use. The growth badge makes the difference explicit
   via text instead of asking a ~20% height gap to read as "different" on
   its own. Lives in .stat-topline now, not stacked directly above the
   bars -- that's what let a tall bar's value label collide with it. */
.bar-growth {
  display: inline-flex; align-items: center; gap: 5px; padding: 4px 11px;
  font-family: "JetBrains Mono", monospace; font-size: 11px; font-weight: 700; letter-spacing: 0.02em;
  color: var(--brand-dark); background: var(--brand-tint); border-radius: 999px;
}
/* .bar-col used to be height:100% with justify-content:flex-end, forcing
   its own content (value label + bar + year label) to fit inside a fixed
   190px box. Once a bar's real height plus its two labels exceeded that,
   the flex column's default flex-shrink:1 silently compressed the bar to
   fit -- which is exactly why a taller-target bar rendered at nearly the
   same height as the shorter one: it was being squashed back down to fit
   the budget, not actually reaching its target. .bar-col is auto-height
   now (sized to its real content, no shrink pressure possible);
   align-items:flex-end on .bar-figure alone is what bottom-aligns the
   columns so the bars still share one baseline -- and .stat-visual's
   210px budget is sized generously above the tallest real bar (120px)
   plus both labels, so there's no overflow this time either. */
.bar-figure { display: flex; align-items: flex-end; justify-content: center; gap: 32px; }
.bar-figure .bar-col { display: flex; flex-direction: column; align-items: center; }
.bar-figure .bar { width: 48px; border-radius: 7px 7px 0 0; flex-shrink: 0; transition: height 0.2s linear; }
.bar-figure .bar-val { font-family: "JetBrains Mono", monospace; font-size: 13.5px; font-weight: 700; margin-bottom: 6px; font-variant-numeric: tabular-nums; }
.bar-figure .bar-lbl { font-size: 11.5px; color: var(--muted); margin-top: 8px; }
.bignum-figure { display: flex; align-items: center; justify-content: center; gap: 20px; }
.bignum-figure .bn { text-align: center; }
.bignum-figure .bn b { display: block; font-family: "Newsreader", Georgia, serif; font-size: 42px; line-height: 1; font-variant-numeric: tabular-nums; }
.bignum-figure .bn.bad b { color: var(--crit); }
.bignum-figure .bn.good b { color: var(--brand-dark); }
.bignum-figure .bn span { font-size: 11.5px; color: var(--muted); display: block; margin-top: 8px; }
.bignum-figure .vs { color: var(--muted); font-size: 12px; font-family: "JetBrains Mono", monospace; }
.stats-quote {
  max-width: 640px; margin: 0 auto; text-align: center; font-family: "Newsreader", Georgia, serif;
  font-style: italic; font-size: 18px; line-height: 1.55; color: var(--ink-soft);
}

/* how we compare: a real feature table, scannable at a glance -- check/
   cross marks carry the answer, cell text stays to a word or two, and the
   handful of facts that actually need a sentence (the citations) live in
   one footnote under the table instead of bloating every cell. Wrapped in
   its own horizontal-scroll container so the table never forces the page
   itself to scroll sideways on a phone. */
/* min-width:0 is load-bearing here, not decoration: .compare-wrap is a
   flex child of .view (flex-direction:column), and flex items default to
   a content-based automatic minimum width -- overflow-x:auto alone
   doesn't reliably override that (the spec's "auto-min-size becomes 0"
   provision needs overflow on both axes to kick in consistently across
   browsers). Without this, the table's min-width:560px silently forced
   the whole page 150px wider than the viewport, confirmed live
   (window.scrollX could reach 151 on a 390px phone) -- the same flex-
   sizing bug class hit twice already tonight elsewhere on this page. */
.compare-wrap { overflow-x: auto; min-width: 0; margin-bottom: 20px; }
.compare-table { width: 100%; min-width: 560px; border-collapse: collapse; }
.compare-table th, .compare-table td { padding: 14px 16px; border-bottom: 1px solid var(--glass-border); font-size: 14px; line-height: 1.4; vertical-align: middle; }
.compare-table thead th { font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 16px; text-align: center; padding-bottom: 4px; border-bottom: none; }
.compare-table thead .col-sub { display: block; font-family: "JetBrains Mono", monospace; font-weight: 500; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--muted); margin-top: 3px; }
.compare-table thead tr:last-child th { padding-bottom: 14px; border-bottom: 1px solid var(--glass-border); }
.compare-table td:first-child, .compare-table th:first-child {
  font-family: "JetBrains Mono", monospace; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--muted); font-weight: 600; text-align: left; white-space: nowrap; padding-right: 20px;
}
.compare-table td:not(:first-child) { text-align: center; }
.compare-table th.mad-col, .compare-table td.mad-col { background: var(--brand-tint); }
.compare-table th.mad-col { color: var(--brand-dark); }
.compare-table thead tr:first-child th.mad-col { border-radius: 10px 10px 0 0; }
.compare-table tbody tr:last-child td.mad-col { border-radius: 0 0 10px 10px; }
.compare-table tbody tr:last-child th, .compare-table tbody tr:last-child td { border-bottom: none; }
.compare-table .mark-yes, .compare-table .mark-no { font-size: 17px; font-weight: 700; }
.compare-table .mark-yes { color: var(--brand-dark); }
.compare-table .mark-no { color: var(--muted); }
.compare-table .mark-partial { font-size: 11.5px; color: var(--muted); }
.compare-footnote {
  max-width: 720px; margin: 0 auto 40px; text-align: center;
  font-size: 12.5px; line-height: 1.6; color: var(--muted);
}
.compare-footnote sup { color: var(--brand-dark); font-weight: 700; }

/* No border-top here on purpose: the scan beam is position:fixed and
   sweeps based on viewport height regardless of scroll position, so it
   passes over every section on the page, not just the hero -- a 1px
   border under its mix-blend-mode:overlay flares into a bright, glitchy
   line whenever the beam crosses it (confirmed by hand earlier tonight
   on a different section, same fix applies here: whitespace alone still
   reads as separation without giving the beam a hard edge to blow out). */
.compare-example { max-width: 640px; margin: 44px auto 0; text-align: center; padding-top: 8px; }
.compare-example .compare-label {
  font-family: "JetBrains Mono", monospace; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--muted); margin-bottom: 14px;
}
.compare-example .compare-stat {
  font-family: "Newsreader", Georgia, serif; font-weight: 700; font-size: 48px;
  color: var(--crit); line-height: 1; margin-bottom: 14px;
}
.compare-example p { margin: 0; font-size: 15px; line-height: 1.65; color: var(--ink-soft); }

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
/* .sub used to be a separate block line ("caption" under the acronym
   headline) -- deliberately merged inline now so the whole phrase reads
   as one continuous line ("...Platform for digital accessibility
   compliance") instead of an artificial headline/caption split that kept
   reading as "stuck in two lines" no matter how the internal wrapping was
   tuned. Muted color is the only remaining distinction. */
.mad-lockup .sub { color: var(--muted); }
@media (max-width: 860px) { .hero-split { grid-template-columns: 1fr; } .hero-shot img { transform: none; } }
.hero-eyebrow {
  display: inline-flex; align-items: center; gap: 6px; font-family: "JetBrains Mono", monospace;
  font-size: 10px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--brand-dark);
  background: var(--glass); border: 1px solid var(--brand); border-radius: 999px; padding: 4px 11px 4px 9px;
  margin-bottom: 16px; max-width: 100%;
  backdrop-filter: blur(12px) saturate(150%); -webkit-backdrop-filter: blur(12px) saturate(150%);
}
.hero-eyebrow-text { min-width: 0; white-space: normal; }
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
  position: relative; overflow: hidden;
  display: inline-flex; align-items: center; gap: 6px; color: #fff; border: none;
  background: linear-gradient(180deg, color-mix(in srgb, var(--brand) 100%, white 14%), var(--brand) 60%, var(--brand-dark));
  padding: 12px 20px; font-size: 15px; font-weight: 700; border-radius: 12px; cursor: pointer; text-decoration: none;
  font-family: inherit; box-shadow: 0 1px 0 rgba(255,255,255,0.35) inset, 0 6px 16px -6px rgba(9,30,28,0.45);
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}
button::before, .btn::before {
  content: ""; position: absolute; inset: 0 0 58% 0; border-radius: inherit;
  background: linear-gradient(180deg, rgba(255,255,255,0.32), transparent); pointer-events: none;
}
button:hover, .btn:hover { transform: translateY(-1px); box-shadow: 0 1px 0 rgba(255,255,255,0.35) inset, 0 10px 22px -6px rgba(9,30,28,0.5); }
.btn-secondary, .btn.ghost {
  background: var(--glass); color: var(--brand-dark); border: 1.5px solid var(--brand); box-shadow: none;
  backdrop-filter: blur(12px) saturate(150%); -webkit-backdrop-filter: blur(12px) saturate(150%);
}
.btn-secondary::before, .btn.ghost::before { display: none; }
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
