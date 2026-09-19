# Brand assets

Local copies of MAD Platform's favicon, app icons, and social share banner,
kept here for reuse outside the website itself (profile pictures, slide
decks, other marketing contexts). The versions the live site actually
serves are the source of truth and live in `mad_platform/web/static/` —
if these ever need to change, edit there and re-copy, not the other way
around.

- `favicon.svg` — the master vector mark: a rounded-square dark-teal
  ground with four bold white bars, redrawn from the header's 4-bar
  brand mark specifically for legibility at 16px (the header mark's full
  4-shade gradient reads fine at normal size but dissolves into mud at
  favicon scale).
- `favicon-16.png`, `favicon-32.png` — raster fallbacks for browsers that
  don't support SVG favicons.
- `apple-touch-icon-180.png` — iOS/iPadOS home-screen bookmark icon.
- `icon-512.png` — large app icon, used in `site.webmanifest` and good
  as a source for any other size that comes up later.
- `site.webmanifest` — the PWA manifest referencing the icons above.
- `og-banner-1200x630.png` — the Open Graph / Twitter card image shown
  when a link to the site is shared in Slack, X, LinkedIn, iMessage, etc.
  Rendered at 2x (2400x1260 actual pixels) for a crisp look on retina
  displays.
