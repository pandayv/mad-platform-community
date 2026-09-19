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

from mad_platform.severity import SEVERITY_ORDER

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,500;0,6..72,600;'
    '0,6..72,700;1,6..72,500&family=Public+Sans:ital,wght@0,400;0,500;0,600;0,700;0,800;'
    '1,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">'
)

# Four-bar mark: four equal-height columns, one hue family stepping light
# to dark left to right -- four agents (crawl/analyze/verify/act), not a
# repeated single shape. Second version of this mark; the first (three
# horizontal bars, one color at varying opacity) turned out to closely
# resemble Ericsson's "three sausages" mark once actually rendered and
# compared side by side rather than just reasoned about -- the earlier
# comment here claimed a check "against the accessibility/security-tool
# space" had cleared it, which shows the failure mode: checking only
# adjacent industries misses a famous mark from a completely unrelated
# one. This version was checked directly against Ericsson, Cisco,
# McDonald's, Marriott, SoundCloud, Microsoft, Slack, and Tableau by
# rendering each at matched scale next to this mark -- no resemblance
# found. Four discrete vertical bars in one hue's tints is a different
# composition from all of them (Ericsson: three horizontal, one color;
# Cisco/SoundCloud: many thin bars of varying height, a skyline/soundwave
# silhouette; McDonald's/Marriott: two continuous connected strokes
# forming a literal M, not discrete bars at all; Slack: four bars in four
# unrelated saturated hues arranged in a radial pinwheel, not a row;
# Tableau: a grid of plus-signs).
#
# Needs its own four CSS custom properties (--brand-mark-2/3/4 above,
# --brand for the first bar) rather than a single inherited currentColor
# -- that's the one real cost of four distinct tints over one repeated
# color. Safe wherever this renders inside the app's own <style> block
# (every web page, and the stored/downloadable report, which embeds
# THEME_CSS directly) since the custom properties resolve there; NOT used
# anywhere in the raw email body, which is the one HTML context in this
# codebase that can't be trusted to keep a <style> block (see
# reporter._EMAIL_SEVERITY_COLOR's comment for why that one place is
# hardcoded hex instead).
BRAND_MARK = (
    '<svg class="brand-mark" viewBox="0 0 40 40" fill="none" aria-hidden="true">'
    '<rect x="6" y="6" width="6" height="28" rx="3" fill="var(--brand)"/>'
    '<rect x="14" y="6" width="6" height="28" rx="3" fill="var(--brand-mark-2)"/>'
    '<rect x="22" y="6" width="6" height="28" rx="3" fill="var(--brand-mark-3)"/>'
    '<rect x="30" y="6" width="6" height="28" rx="3" fill="var(--brand-mark-4)"/>'
    "</svg>"
)

THEME_CSS = """
:root {
  --ink: #12181A; --ink-soft: #3C4A49; --muted: #5B6B6A;
  --bg: #EDF2F1; --surface: #FFFFFF; --surface-alt: #E3ECE9; --border: #CBDAD6;
  --brand: #0B6E66; --brand-dark: #084F49; --brand-tint: #E1F0EE; --focus: #0B6E66;
  /* The brand mark's other three bars -- see BRAND_MARK below. One hue
     family stepping light to dark, not four unrelated colors, so it
     reads as "one brand, four parts" rather than a rainbow. */
  --brand-mark-2: #2E9187; --brand-mark-3: #5CB3A8; --brand-mark-4: #8ECFC5;
  --crit: #C0152B; --crit-tint: #FDECEC;
  --high: #C2570A; --high-tint: #FDF1E6;
  --med:  #A67C00; --med-tint:  #FBF3DA;
  --low:  #47566B; --low-tint:  #EBEEF2;
  --ok:   #157A4F; --ok-tint:   #E4F5EC;
  --border-strong: #9FB6B1;
  --shadow: 0 1px 2px rgba(18,24,26,0.06), 0 8px 24px rgba(18,24,26,0.05);
  /* The hero scan pill's width, and the .scan-section wrapper that has to
     line up with it. Both hardcoded 480px before, so changing one silently
     broke the alignment -- and the comment above .scan-bar treats 480 as a
     deliberate, argued-over number, which is exactly the kind of value
     that must not exist twice. */
  --scan-bar-max: 480px;
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
    --brand-mark-2: #5CCBBD; --brand-mark-3: #82D8CC; --brand-mark-4: #AAE6DC;
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
   sit at the same level as the part that was actually scrollable.
   Don't add it to body too "for safety" -- overflow-x:hidden on both
   html and body at once silently breaks the sticky header (Chromium
   stops tracking the viewport for position:sticky descendants), even
   though overflow-x:hidden on just one of them fully blocks horizontal
   scroll on its own. Confirmed via Playwright: verified in isolation. */
html { overflow-x: hidden; }
body {
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
.link-btn {
  background: none; border: none; padding: 0; font: inherit; cursor: pointer;
  color: var(--brand-dark); text-decoration: underline;
}
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; border-radius: 3px; }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
/* Skip link. Hidden until focused, then a normal visible control at the
   top-left -- a keyboard or screen-reader user should not have to walk the
   header on every page to reach the content, and there was no way to skip
   it at all. WCAG 2.4.1 (Bypass Blocks). Sits above .scan-beam's z-index:30
   so it can't be covered by the sweep. */
.skip-link {
  position: absolute; left: -9999px; top: 0; z-index: 100;
  background: var(--surface); color: var(--brand-dark); border: 2px solid var(--brand);
  border-radius: 0 0 8px 0; padding: 12px 18px; font-weight: 700; text-decoration: none;
}
.skip-link:focus { left: 0; }
/* The comparison table scrolls horizontally inside .compare-wrap; giving
   that wrapper tabindex="0" is what lets a keyboard user scroll it at all,
   and this makes the resulting focus visible rather than silent. */
.compare-wrap:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }

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
/* Only the hero still uses this now. It used to be shared by every major
   section (hero, why-it-matters, how-it-works, under-the-hood, compare),
   forcing each to fill the viewport regardless of how much content it
   held -- short sections got centered inside a mostly-empty full-height
   block, which read as a wall of dead space between sections and gave a
   first-time visitor no hint anything followed the hero. Those four now
   use plain .section and size to their own content; only the hero still
   deliberately fills the screen (a landing page's opening moment earning
   that space is a different case from a content section drowning in it),
   which is also why the scroll-hint below only makes sense pinned to it. */
.view { min-height: calc(100vh - 65px); display: flex; flex-direction: column; justify-content: center; position: relative; }
/* Bottom-pinned regardless of hero content height (absolute within .view,
   not part of the centered flex content) so it reads as "there's more
   below" rather than just decorating the hero copy. A real link, not a
   pure decoration -- keyboard/switch users get the same "jump to next
   section" affordance a sighted visitor gets by scrolling past it. */
.scroll-hint {
  position: absolute; left: 50%; bottom: 28px; transform: translateX(-50%);
  display: flex; align-items: center; justify-content: center;
  width: 36px; height: 36px; border-radius: 50%; border: 1px solid var(--border);
  color: var(--muted); text-decoration: none; animation: scroll-hint-bob 2.2s ease-in-out infinite;
}
.scroll-hint:hover { color: var(--brand-dark); border-color: var(--brand); }
.scroll-hint svg { width: 16px; height: 16px; }
@keyframes scroll-hint-bob {
  0%, 100% { transform: translateX(-50%) translateY(0); }
  50% { transform: translateX(-50%) translateY(6px); }
}
@media (prefers-reduced-motion: reduce) { .scroll-hint { animation: none; } }
@media (max-width: 640px) { .scroll-hint { bottom: 14px; } }

.page { max-width: 640px; margin: 0 auto; padding: 60px 24px; }
.page.with-site-header { padding-top: 44px; }
.page.wide { max-width: 900px; }
.serif { font-family: "Newsreader", Georgia, serif; }
h1 { font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 32px; margin: 0 0 10px; letter-spacing: -0.01em; text-wrap: balance; word-break: break-word; }
h2 { font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 21px; margin: 0 0 10px; letter-spacing: -0.005em; }
.tagline { color: var(--muted); font-size: 15px; margin-bottom: 32px; }
/* Same "spacing separates it, not a border" treatment as the landing
   page's .how-footnote -- explains the card above it without living
   inside it. */
.verify-footnote { margin: 20px 0 0; font-size: 12.5px; line-height: 1.6; color: var(--muted); }
.card {
  background: var(--glass-strong); border: 1px solid var(--glass-border); border-radius: 18px;
  padding: 28px; box-shadow: var(--glass-shadow);
  backdrop-filter: blur(28px) saturate(160%); -webkit-backdrop-filter: blur(28px) saturate(160%);
}

/* ---- site-wide header + footer, every page shell uses these ---- */
.brand { font-size: 12px; letter-spacing: 0.09em; text-transform: uppercase; color: var(--brand-dark); font-weight: 800; display: flex; align-items: center; gap: 7px; }
.brand .dot-b { width: 7px; height: 7px; border-radius: 50%; background: var(--brand); flex-shrink: 0; box-shadow: 0 0 0 3px var(--brand-tint); }
.brand-mark { width: 19px; height: 19px; flex-shrink: 0; vertical-align: -4px; }
.site-header {
  position: sticky; top: 0; z-index: 40;
  border-bottom: 1px solid var(--glass-border); background: var(--glass-strong);
  backdrop-filter: blur(20px) saturate(160%); -webkit-backdrop-filter: blur(20px) saturate(160%);
}
.site-header-inner {
  max-width: 1080px; margin: 0 auto; padding: 18px 24px;
  display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
}
.site-header-inner a.brand { text-decoration: none; }
/* Marketing/content pages only (this selector only matches inside
   .site-header-inner, so the review queue, report page, and status
   pages keep their existing small quiet mark on purpose -- those were
   deliberately left alone in an earlier pass, see _site_header()'s own
   docstring). Bigger mark + normal-case wordmark reads as an actual
   logo instead of a small uppercase label. */
.site-header-inner .brand {
  font-size: 21px; letter-spacing: -0.01em; text-transform: none; gap: 10px;
}
.site-header-inner .brand-mark { width: 28px; height: 28px; }
.site-nav { display: flex; align-items: center; gap: 22px; list-style: none; margin: 0; padding: 0; }
.site-nav a { color: var(--ink-soft); text-decoration: none; font-size: 13.5px; font-weight: 600; }
.site-nav a:hover { color: var(--brand-dark); }
.site-nav a.active { color: var(--brand-dark); }
.nav-toggle {
  display: none; align-items: center; justify-content: center; flex-shrink: 0; order: 2;
  width: 36px; height: 36px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--surface-alt); color: var(--ink); cursor: pointer; padding: 0;
}
.nav-toggle svg { width: 18px; height: 18px; }
.nav-toggle .icon-close { display: none; }
.nav-toggle[aria-expanded="true"] .icon-menu { display: none; }
.nav-toggle[aria-expanded="true"] .icon-close { display: block; }
/* Below this, the nav collapses behind .nav-toggle instead of wrapping to
   a second header row -- the actual cause of an earlier mobile bug (see
   _HEADER_NAV_LINKS's comment). .site-header-inner's own flex-wrap is what
   .site-nav's order/flex-basis below hand it a full-width row to wrap
   into, so no structural change is needed there, only these two rules. */
@media (max-width: 859px) {
  .nav-toggle { display: flex; }
  .site-nav {
    display: none; order: 3; flex-direction: column; align-items: stretch; gap: 2px;
    flex-basis: 100%; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border);
  }
  .site-nav.is-open { display: flex; }
  .site-nav a { padding: 9px 4px; }
}
.site-footer { border-top: 1px solid var(--border); margin-top: 64px; }
.site-footer-inner {
  max-width: 1080px; margin: 0 auto; padding: 28px 24px 40px;
  display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  font-size: 12.5px; color: var(--muted);
}
.site-footer nav { display: flex; gap: 18px; flex-wrap: wrap; }
.site-footer a { color: var(--muted); text-decoration: none; }
.site-footer a:hover { color: var(--brand-dark); }
.site-footer a.support-link { display: inline-flex; align-items: center; gap: 5px; }
.site-footer a.support-link svg { width: 13px; height: 13px; flex-shrink: 0; }

/* ---- split hero: copy column + an illustrative example-report visual,
   grounded in two references (a dark-hero charity-auction template and a
   split light/dark fintech template) that both use high contrast between
   one light zone and one dark zone, a single accent color, and glass/depth
   spent on exactly one element -- not smeared across the whole page as
   background blobs. Went through ~10 rounds of mockup iteration before
   landing here; see the artifact history if this ever needs revisiting. ---- */
/* 900px to match .site-header-inner exactly -- the mockup used 1160px,
   which read fine in isolation but put the hero out of step with the
   header logo above it (and with .section's 980px below), a real
   misalignment once seen on the actual page next to the real header. */
.hero-outer { max-width: 1080px; margin: 0 auto; padding: 20px 24px 0; box-sizing: border-box; }
.hero-grid { display: grid; grid-template-columns: 1.05fr 0.95fr; gap: 64px; align-items: stretch; }
@media (max-width: 860px) { .hero-grid { grid-template-columns: 1fr; gap: 36px; } }

/* scroll-margin-top (not on .scan-section, where it used to live): the
   #scan anchor moved to this whole column, not just the form -- anchoring
   scroll to the form alone left the headline/eyebrow above it scrolled
   off-screen (they're earlier siblings in the same flex column), landing
   a cross-page "Scan a site" click on a bare form with no context and a
   large dead gap below it (the hero .view section's own min-height
   padding, with nothing left to fill it once scrolled past the headline).
   Anchoring to the column's top shows the full hero, scan bar included. */
.hero-copy { display: flex; flex-direction: column; justify-content: center; align-items: flex-start; height: 100%; scroll-margin-top: 90px; }

/* Text-first badge, not the mono/uppercase label treatment used for
   in-page eyebrows elsewhere -- this one only ever holds "Community
   Edition", so it reads as a small status pill, not a data label. */
.hero-eyebrow {
  display: inline-flex; align-items: center; gap: 7px; font-family: "Public Sans", sans-serif;
  font-size: 12.5px; font-weight: 600; text-transform: none; letter-spacing: normal; color: var(--brand-dark);
  background: var(--brand-tint); border: 1px solid var(--border); border-radius: 20px; padding: 6px 13px 6px 12px;
  margin-bottom: 22px; max-width: 100%; backdrop-filter: none; -webkit-backdrop-filter: none;
}
.hero-eyebrow-text { min-width: 0; white-space: normal; }
.hero-eyebrow .dot-b { width: 6px; height: 6px; box-shadow: 0 0 0 3px rgba(21,122,79,0.18); background: var(--ok); }

/* Sans-serif here is a deliberate departure from the site's usual serif
   h1/h2 (see the generic h1 rule up top) -- the mockup rounds converged on
   a bold sans headline specifically for this hero, kept for just this one
   element rather than changed sitewide. */
.hero-title {
  font-family: "Public Sans", sans-serif; font-size: 44px; line-height: 1.1; font-weight: 600;
  letter-spacing: -0.015em; margin: 0 0 16px; text-wrap: balance;
}
/* var(--brand), not --brand-dark: --brand-dark is close enough to the
   surrounding near-black ink that the "emphasis" barely registered as a
   color change, just a bold-weight bump. --brand is lighter/more
   saturated, so it actually reads as a distinct accent against the
   headline's own color, not just a font-weight difference. */
.hero-title strong { font-weight: 800; color: var(--brand); }
.hero-tagline { font-family: "Public Sans", sans-serif; font-weight: 400; font-size: 17px; line-height: 1.6; color: var(--ink-soft); max-width: 46ch; margin: 0 0 26px; }

.scan-section { width: 100%; max-width: var(--scan-bar-max); box-sizing: border-box; margin-bottom: 22px; }
.scan-form { display: flex; flex-direction: column; gap: 12px; }

/* Google-proportioned: a plain surface with a light shadow, not the
   heavier glass-blur treatment used elsewhere on the page -- narrower
   (~480px, was 620px) and crisper reads more "sleek" than a wider glass
   pill did in side-by-side comparison, which settled what the width
   complaint was actually about (proportions/surface, not raw pixels).
   The leading brand mark (not a generic magnifying glass) is the one
   deliberately colored element in an otherwise quiet bar. */
.scan-bar {
  width: 100%; max-width: var(--scan-bar-max); display: flex; align-items: center; gap: 4px; box-sizing: border-box;
  border: 1px solid var(--border); background: var(--surface);
  border-radius: 999px; padding: 4px 6px 4px 18px;
  box-shadow: var(--shadow);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.scan-bar:focus-within { border-color: var(--brand); box-shadow: 0 0 0 3px var(--brand-tint), var(--shadow); }
/* Groups the logo and the URL input as one flex item so they wrap as a
   pair, not separately -- without this, wrapping .scan-bar's children
   individually put the logo alone on its own row above the input on
   narrow screens. */
.scan-bar-field { display: flex; align-items: center; gap: 4px; flex: 1; min-width: 0; }
.scan-bar .brand-mark { width: 18px; height: 18px; }
.scan-bar input {
  flex: 1; min-width: 0; border: none; background: none; outline: none;
  padding: 14px 10px; font-size: 15.5px; font-family: inherit; color: var(--ink);
}
.scan-bar input::placeholder { color: var(--muted); }
/* height + padding:0 (not the base button's vertical padding) is the
   actual fix -- the button and the input next to it had different
   vertical paddings (12px vs 15px), so even though align-items:center
   centered them relative to each other, their different heights read as
   a few pixels of misalignment. Matching them to the same explicit
   height removes that regardless of either element's own font metrics. */
.scan-bar .scan-submit {
  flex-shrink: 0; height: 44px; border-radius: 999px !important; padding: 0 22px !important; margin: 0;
  font-size: 14px;
  /* Matching the heights above removes the FONT-METRIC source of
     misalignment, but not all of it: the base .scan-submit rule sets
     align-self: flex-end, which has lower specificity than this selector
     yet was never overridden here -- so inside .scan-bar (display:flex;
     align-items:center) the button's bottom edge aligned to the ~51px
     input's rather than centering against it, sitting 3-4px low at every
     width above 480px. A @media (max-width: 480px) rule already patched
     it with `align-self: auto !important` for phones only, which is the
     giveaway: the leak was noticed at one width and fixed there instead
     of at its source. Fixed here, and that media-query patch is gone. */
  align-self: center;
}

/* Every OTHER field in the funnel (email step, code step) keeps the
   plainer boxed-field treatment -- only the homepage's URL entry gets the
   merged-pill emphasis, since it's the one field on the page. */
.scan-field { position: relative; margin-bottom: 16px; }
.scan-field input, .scan-field textarea {
  display: block; width: 100%; box-sizing: border-box; margin: 0;
  border: 1px solid var(--glass-border); background: var(--glass-strong); color: var(--ink);
  border-radius: 12px; padding: 13px 16px; font-size: 15.5px; font-family: inherit;
  box-shadow: var(--glass-shadow); resize: vertical;
  backdrop-filter: blur(20px) saturate(160%); -webkit-backdrop-filter: blur(20px) saturate(160%);
}
.scan-field input::placeholder, .scan-field textarea::placeholder { color: var(--muted); }
.scan-field input:focus, .scan-field textarea:focus { outline: none; border-color: var(--brand); box-shadow: 0 0 0 3px var(--brand-tint); }
/* The feedback form's star rating and testimonial checkbox -- plain
   radios/checkbox under the hood (no JS required to submit; the one
   script on this page only drives the live character count below),
   styled well past their default look.

   Classic pure-CSS star trick: five radio+label pairs written 5,4,3,2,1
   in DOM order, then visually un-reversed with flex-direction so star 1
   sits leftmost. That ordering is what lets a plain ~ (general sibling)
   selector mean "this star and everything before it, visually" --
   hovering or checking star N matches every label *after* N in DOM
   order, which is every star at or below N on screen. */
.rating-field { border: none; margin: 0 0 18px; padding: 0; }
.star-rating { display: flex; flex-direction: row-reverse; justify-content: flex-end; gap: 2px; margin-top: 8px; }
.star-rating input { position: absolute; opacity: 0; width: 0; height: 0; }
.star-rating label { display: block; color: var(--border); cursor: pointer; }
.star-rating label svg { width: 34px; height: 34px; display: block; }
.star-rating input:checked ~ label,
.star-rating label:hover,
.star-rating label:hover ~ label {
  color: #F0A93A; /* a warm gold, not --brand -- stars read as a rating
    convention independent of the site's own accent color, the same way
    a real star rating would on any product regardless of its brand hue */
}
.star-rating input:focus-visible + label { outline: 2px solid var(--focus); outline-offset: 3px; border-radius: 6px; }
.feedback-checkbox {
  display: flex; align-items: center; gap: 8px; font-size: 14px; color: var(--ink-soft);
  margin-bottom: 16px; cursor: pointer;
}
.feedback-checkbox input { width: 16px; height: 16px; flex-shrink: 0; accent-color: var(--brand); }
.scan-submit {
  align-self: flex-end; justify-content: center; font-size: 14px;
  border-radius: 10px !important; padding: 10px 20px !important; margin-top: 2px;
}
@media (max-width: 480px) {
  .scan-bar { flex-wrap: wrap; border-radius: 22px; padding: 14px 16px; }
  /* flex-basis: 100% on the field GROUP (logo+input together), not on the
     input alone -- that's what keeps the logo and the URL text on the same
     row when they wrap, with the button dropping to its own row below. */
  .scan-bar-field { flex-basis: 100%; }
  .scan-bar input { padding: 2px 0 10px; }
  /* No align-self override needed any more -- .scan-bar .scan-submit sets
     center for every width now, instead of this rule undoing a leak from
     the base rule for phones only. */
  .scan-bar .scan-submit { flex: 1; }
}

/* Same anti-abuse fact as the "how it works" footnote and the email-step
   page itself, just surfaced earlier -- at the actual point someone
   decides to click Scan, not several sections below it, so the email
   step (first-time visitors only) never lands as a surprise. */
/* Sits close under the pill (small margin-top, inside .scan-section) --
   a footnote to the field right above it, not a separate block. Centered
   to the pill's own width rather than the wider headline/tagline column
   above it, so it reads as anchored to the field it's annotating. */
.scan-hint {
  display: flex; align-items: center; justify-content: center; gap: 6px; font-size: 12.5px; color: var(--muted);
  margin: 8px 0 0;
}
.scan-hint svg { width: 14px; height: 14px; color: var(--ok); flex-shrink: 0; }
/* A muted amber, not --brand -- this icon is meant to read as a literal
   lightbulb (a "tip" affordance), which only works with a yellow/gold
   hue; the teal brand color didn't evoke that. Kept muted/desaturated
   rather than a vivid yellow, and distinct from the star rating's warmer,
   more saturated gold (#F0A93A) so the two don't visually collide as
   "the same color means two different things" elsewhere on the site. */
.scan-hint-tip svg { color: #B89340; }
/* A clear step below the hint (.scan-section's own margin-bottom, not a
   margin here) -- the checks are a separate block, not part of the hint's
   sentence. justify-content:space-between (not center) spreads the three
   checks to the pill's own left and right edges, so the row visually
   spans the same width as the pill above it instead of reading as a
   narrower, centered island. Sized up from 12.5px -> 14px so the row
   reads as a real trust signal, not a small-print footnote. */
.trust-row {
  display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px 16px; font-size: 14px; color: var(--ink-soft);
  width: 100%; max-width: var(--scan-bar-max); box-sizing: border-box;
}
.trust-row span { display: flex; align-items: center; gap: 6px; }
.trust-row svg { width: 15px; height: 15px; color: var(--ok); flex-shrink: 0; }

/* The one glass/depth moment in the hero -- a fixed dark ground (not
   theme-reactive, deliberately: it's a stand-in browser chrome, meant to
   read the same regardless of the page's own light/dark mode) with a
   single illustrative example report, not a real scan. brand-derived glow
   colors, not a separate accent system. */
.hero-visual-col { display: flex; flex-direction: column; gap: 12px; height: 100%; }
.hero-visual {
  position: relative; background: #12181A; border-radius: 24px; padding: 34px; min-height: 460px;
  flex: 1; overflow: hidden; box-shadow: 0 20px 50px -25px rgba(9,30,28,0.45);
  display: flex; align-items: center; justify-content: center;
}
.hero-visual::before {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background:
    radial-gradient(600px 380px at 78% 12%, rgba(63,191,175,0.28), transparent 60%),
    radial-gradient(500px 300px at 10% 90%, rgba(11,110,102,0.24), transparent 60%);
}
.hero-visual .grid-lines {
  position: absolute; inset: 0;
  background-image: linear-gradient(rgba(255,255,255,0.05) 1px,transparent 1px), linear-gradient(90deg,rgba(255,255,255,0.05) 1px,transparent 1px);
  background-size: 34px 34px; mask-image: linear-gradient(180deg,rgba(0,0,0,0.9),transparent 75%);
}
.example-card {
  position: relative; z-index: 2; width: 100%; max-width: 320px; margin: 0 auto;
  background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.16); border-radius: 16px;
  backdrop-filter: blur(6px); box-shadow: 0 30px 60px -30px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.12);
  overflow: hidden; transform: rotate(-2deg);
}
.example-card .browser-bar { display: flex; align-items: center; gap: 6px; padding: 10px 14px; border-bottom: 1px solid rgba(255,255,255,0.1); }
.example-card .browser-bar i { width: 8px; height: 8px; border-radius: 50%; background: rgba(255,255,255,0.25); }
.example-card .browser-bar .url { margin-left: 10px; font-family: "JetBrains Mono", monospace; font-size: 10.5px; color: rgba(255,255,255,0.45); }
.example-card .card-body { padding: 20px 18px; }

.example-score-row { display: flex; align-items: center; gap: 14px; padding-bottom: 16px; margin-bottom: 14px; border-bottom: 1px solid rgba(255,255,255,0.1); }
.example-score-ring { position: relative; width: 58px; height: 58px; flex-shrink: 0; }
.example-score-ring svg { width: 100%; height: 100%; }
.example-score-ring .ring-bg { stroke: rgba(255,255,255,0.14); }
.example-score-ring .ring-fg { stroke: #3FBFAF; }
.example-score-num { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-family: "Newsreader", Georgia, serif; font-size: 17px; font-weight: 700; color: #fff; }
.example-score-meta b { display: block; color: #fff; font-size: 13.5px; font-weight: 700; margin-bottom: 2px; }
.example-score-meta span { display: block; color: rgba(255,255,255,0.5); font-size: 11.5px; }

.example-sev-rows { display: flex; flex-direction: column; gap: 7px; }
.example-sev-row { display: flex; align-items: center; gap: 9px; font-size: 12.5px; color: rgba(255,255,255,0.8); background: rgba(255,255,255,0.05); border-radius: 8px; padding: 8px 10px; }
.example-sev-row .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.example-sev-row b { margin-left: auto; color: #fff; font-weight: 700; }

/* color:#14171F, not var(--ink) -- this chip's background is the fixed
   #fff a few lines up, on purpose (a floating callout that reads the same
   over the dark hero regardless of page theme, same as the rest of this
   block). var(--ink) flips to a near-white value in dark mode, which on
   this always-white chip meant near-invisible text -- the one line in
   this block that didn't match the "fixed, not theme-reactive" rule the
   rest of it already follows. */
.example-chip {
  position: absolute; background: #fff; border-radius: 11px; padding: 9px 13px; font-size: 12px; font-weight: 600;
  display: flex; align-items: center; gap: 7px; box-shadow: 0 16px 30px -14px rgba(0,0,0,0.45); z-index: 3; color: #14171F;
}
.example-chip svg { width: 14px; height: 14px; flex-shrink: 0; }
.example-chip.ok svg { color: var(--ok); }
.example-chip.warn svg { color: var(--crit); }
.example-chip-1 { top: 24px; right: 22px; }
.example-chip-2 { bottom: 38px; left: 18px; }

/* The MAD acronym expansion -- settled placement after a few rounds of
   trying it under the wordmark instead: caption under the hero image only,
   sized up a step from a plain caption for some emphasis without a border
   or its own section. No white-space:nowrap on purpose -- the column
   narrows well below the text's natural width before the mobile
   breakpoint kicks in, so nowrap would let it overflow past the image's
   edge. Normal wrapping guarantees it never renders wider than the image:
   one line at any width the site is likely to run at, two only in a
   narrow in-between range. */
.lockup-caption { margin: 0; text-align: center; font-size: 15.5px; font-weight: 500; color: var(--ink-soft); line-height: 1.4; }
.lockup-caption .hl { color: var(--brand-dark); font-weight: 800; }

@media (max-width: 640px) { .scan-submit { align-self: stretch; } }

/* One pattern, every section below the hero, modeled on the eyebrow +
   big-title + small-subtext composition (the epresence.ai-inspired
   format from earlier): a small kicker word sits above a genuinely large,
   bold title -- the title is the section's real name, sized to actually
   dominate the eyebrow and the subtext both, not just nudged a step over
   plain body text. Applies identically to every section now, not just one. */
.section { max-width: 1080px; margin: 0 auto; padding: 56px 24px; scroll-margin-top: 90px; }
.section-head { text-align: center; margin-bottom: 52px; }
.section-eyebrow { display: flex; align-items: center; justify-content: center; gap: 12px; margin-bottom: 12px; }
.section-eyebrow .line { width: 22px; height: 1px; background: var(--brand); opacity: 0.5; }
.section-eyebrow span:not(.line) { font-family: "JetBrains Mono", monospace; font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--brand-dark); font-weight: 700; }
.section-head h2 { font-size: 40px; font-weight: 700; line-height: 1.15; margin-bottom: 10px; text-wrap: balance; letter-spacing: -0.01em; }
.section-head p { color: var(--muted); font-size: 15px; margin: 0; }
.section-head p sup { color: var(--brand-dark); font-weight: 700; }
@media (max-width: 640px) { .section-head h2 { font-size: 30px; } }

/* "Why the report holds up": the real pipeline architecture (four
   specialized agents, not one model doing everything), drawn as an
   actual flow instead of a grid of description cards -- the diagram
   itself is the "architectural decision" content, so it doesn't need to
   be re-explained in paragraph form next to it. Captions stay to one
   short line each; the mechanism, not marketing language. */
.pipeline-flow { display: flex; align-items: flex-start; justify-content: center; }
.pipeline-stage { flex: 1; max-width: 190px; display: flex; flex-direction: column; align-items: center; text-align: center; }
.pipeline-badge {
  width: 52px; height: 52px; border-radius: 50%; flex-shrink: 0; margin-bottom: 14px;
  background: linear-gradient(160deg, color-mix(in srgb, var(--brand) 100%, white 25%), var(--brand-dark));
  color: #fff; display: flex; align-items: center; justify-content: center;
  box-shadow: 0 1px 0 rgba(255,255,255,0.4) inset, var(--shadow);
}
.pipeline-badge svg { width: 22px; height: 22px; }
.pipeline-stage h3 { font-size: 15px; margin: 0 0 6px; font-weight: 700; }
.pipeline-stage p { font-size: 12.5px; color: var(--ink-soft); margin: 0; line-height: 1.5; }
.pipeline-arrow { flex: 0 1 60px; padding-top: 24px; color: var(--border-strong); display: flex; align-items: center; justify-content: center; }
.pipeline-arrow svg { width: 22px; height: 14px; }
@media (max-width: 760px) {
  .pipeline-flow { flex-direction: column; align-items: stretch; gap: 4px; }
  .pipeline-stage { flex-direction: row; max-width: none; text-align: left; gap: 14px; align-items: flex-start; }
  .pipeline-badge { margin-bottom: 0; }
  .pipeline-arrow { padding-top: 0; padding-left: 25px; }
  .pipeline-arrow svg { width: 22px; height: 14px; transform: rotate(90deg); }
}

/* Supplementary architecture notes, deliberately smaller and quieter
   than the pipeline above -- real system decisions (grounding, crash
   recovery, ruleset currency) that matter but don't need equal visual
   weight to the four-stage flow that's the section's actual centerpiece. */
.arch-notes { display: flex; gap: 32px; justify-content: center; margin-top: 48px; flex-wrap: wrap; }
.arch-note { display: flex; align-items: flex-start; gap: 10px; max-width: 300px; }
.arch-note svg { width: 16px; height: 16px; color: var(--brand-dark); flex-shrink: 0; margin-top: 2px; }
.arch-note b { display: block; font-size: 13px; }
.arch-note span { display: block; font-size: 12px; color: var(--muted); margin-top: 1px; line-height: 1.4; }

/* how it works: real screenshots, numbered */
.how-visual { display: flex; align-items: flex-start; justify-content: center; gap: 56px; flex-wrap: wrap; }
.how-step { flex: 1; min-width: 280px; max-width: 400px; text-align: center; }
.how-step .shot-frame {
  position: relative; border-radius: 18px; border: 1px solid var(--glass-border); background: var(--glass-strong);
  box-shadow: var(--glass-shadow); margin-bottom: 16px;
  backdrop-filter: blur(28px) saturate(160%); -webkit-backdrop-filter: blur(28px) saturate(160%);
}
/* Fixed aspect-ratio + object-fit is the actual fix, not just recropping
   the two current screenshots to match by hand -- the two source images
   naturally come from differently-shaped content (a tall stacked hero vs.
   a wide dashboard), so anything that keeps matching them by manually
   tuning crop dimensions will drift again the next time either gets
   updated. This makes the two frames the same height unconditionally. */
.how-step img { width: 100%; aspect-ratio: 5 / 4; object-fit: cover; object-position: top; display: block; border-radius: 14px; padding: 4px; }
.how-step .step-badge {
  position: absolute; top: -14px; left: -14px; width: 34px; height: 34px; border-radius: 50%;
  background: linear-gradient(160deg, color-mix(in srgb, var(--brand) 100%, white 25%), var(--brand-dark));
  color: #fff; display: flex; align-items: center; justify-content: center;
  font-family: "Newsreader", Georgia, serif; font-weight: 700; font-size: 16px;
  box-shadow: 0 1px 0 rgba(255,255,255,0.4) inset, var(--shadow);
  z-index: 2;
}
/* A real footnote, not just another caption stacked under the images --
   spacing alone is what separates it from the screenshots (matching
   .compare-footnote's own treatment below), not a rule. A horizontal
   line here read as a stray divider mid-scroll rather than a deliberate
   section boundary -- the same reason .compare-footnote never had one. */
.how-footnote {
  margin: 36px 0 0; text-align: center; font-size: 12.5px; line-height: 1.6; color: var(--muted);
}
.how-footnote sup { color: var(--brand-dark); font-weight: 700; }
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
.compare-wrap { overflow-x: auto; min-width: 0; margin-bottom: 20px; position: relative; }
/* Mobile-only scroll affordance: below the table's own 560px min-width,
   the first render shows just the MAD column with no visual hint that the
   other two are one swipe away -- a real usability bug on a phone, not
   just a style nit. Text hint is the reliable part; the edge fade is a
   soft visual echo of it, not load-bearing on its own. */
.compare-hint {
  display: none; font-family: "JetBrains Mono", monospace; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--muted); text-align: center; margin: -8px 0 10px;
}
.compare-wrap::after {
  content: ""; position: absolute; top: 0; right: 0; bottom: 0; width: 26px; pointer-events: none;
  background: linear-gradient(90deg, transparent, var(--bg) 80%); opacity: 0;
}
@media (max-width: 640px) {
  .compare-hint { display: block; }
  .compare-wrap::after { opacity: 1; }
}
.compare-table { width: 100%; min-width: 560px; border-collapse: collapse; }
.compare-table th, .compare-table td { padding: 14px 16px; border-bottom: 1px solid var(--glass-border); font-size: 14px; line-height: 1.4; vertical-align: middle; }
.compare-table thead th { font-family: "Newsreader", Georgia, serif; font-weight: 600; font-size: 16px; text-align: center; padding-bottom: 4px; border-bottom: none; }
.compare-table thead .col-sub { display: block; font-family: "JetBrains Mono", monospace; font-weight: 500; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--muted); margin-top: 3px; }
.compare-table thead tr:last-child th { padding-bottom: 14px; border-bottom: 1px solid var(--glass-border); }
/* tbody-scoped on purpose: this styles the row-label column (e.g.
   "Multi-page scan"). The unscoped version also matched thead's second
   row, since that row has no leading empty cell and its MAD Platform
   <th> became :first-child there by accident -- which is exactly why
   "Free, always" was rendering left-aligned/mono while the "MAD
   Platform" title one row up (which does sit behind a real leading
   cell) rendered centered/serif correctly. */
.compare-table tbody td:first-child, .compare-table tbody th:first-child {
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
/* Bumped from 10.5px/--muted/400-weight: at that size and contrast these
   read as decoration, not the section headers they actually are once the
   FAQ was grouped into named categories a reader might scan for -- easy
   to miss entirely next to 15px bold-black questions right below them.
   Still monospace/uppercase (the same eyebrow treatment used elsewhere on
   the site), just legible as a real label now: bigger, bolder, and
   --brand-dark instead of --muted for real contrast against the page. */
.trust-section-label {
  font-family: "JetBrains Mono", monospace; font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--brand-dark); margin: 34px 0 16px 44px;
}
.trust-section-label:first-child { margin-top: 0; }

label.f-label, legend.f-label { display: block; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 700; margin-bottom: 6px; padding: 0; border: 0; }
/* input[type=password] only. The selector used to lead with
   input[type=url], which matched nothing: the URL field is deliberately
   type="text" (see app._safe_url_or_error -- native type="url" validation
   rejects the bare "cahm.org" most visitors actually type). A dead
   selector reads as "this field is styled here" to the next person
   changing it. The one field this really does style is the review
   login's password box. */
input[type=password] {
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

# The severity vocabulary comes from mad_platform/severity.py, not a fourth
# private list here -- see that module for what five independent copies of
# these four words cost.
_SEVERITY_ORDER = list(SEVERITY_ORDER)

# Which palette token each severity renders in. One mapping, two forms:
# SEVERITY_VAR below for anything the browser renders (it follows the
# viewer's light/dark theme), and LIGHT_HEX for the two places a literal
# value is genuinely required -- email bodies, where Gmail strips the
# <style> block that would define the custom properties.
SEVERITY_TOKEN = {"critical": "--crit", "high": "--high", "medium": "--med", "low": "--low"}
SEVERITY_VAR = {sev: f"var({token})" for sev, token in SEVERITY_TOKEN.items()}

# The light-mode value of each palette token, duplicated out of THEME_CSS
# above because THEME_CSS is one opaque string and no email client can read
# a custom property out of it.
#
# This duplication is deliberate and *tested*: tests/test_theme_palette.py
# parses the :root block out of THEME_CSS and asserts every entry here
# matches it. That is what makes this a mirror rather than a fifth
# independent palette -- editing a token in the CSS without editing it here
# fails a test instead of shipping two slightly different greens (which is
# exactly what reporter.score_color had done: #15803D against --ok's
# #157A4F).
LIGHT_HEX = {
    "--crit": "#C0152B",
    "--high": "#C2570A",
    "--med": "#A67C00",
    "--low": "#47566B",
    "--ok": "#157A4F",
    "--brand": "#0B6E66",
    "--ink": "#12181A",
    "--muted": "#5B6B6A",
}


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
                # `-offset if offset else 0.0`, not plain `-offset`: at
                # offset 0 Python formats negative zero as "-0.00" while
                # the status page's JavaScript emits "0.00". Both render
                # identically, but tests/test_chart_parity.py diffs the two
                # implementations character for character, and a parity
                # check that has to tolerate differences stops catching the
                # ones that matter.
                f'stroke-dasharray="{length:.2f} {circumference - length:.2f}" '
                f'stroke-dashoffset="{(-offset if offset else 0.0):.2f}"/>'
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
