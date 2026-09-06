# Yantrika brand assets

Generated for the YantraFleet → **Yantrika** rename. Mark: a hex "chassis"
(robot/device) with a white Y-shaped path converging on a hub, ending in a
single cyan waypoint dot — reads as both the letter Y and a fleet routing to
a destination. Primary blue (`#2563c9`) is carried over from the site's
existing `theme-color` so nothing else needs to change.

## Files

| File | Use |
|---|---|
| `icon-mark.svg` | Vector icon, any size, transparent bg |
| `favicon.svg` | Same mark, slightly thicker strokes for legibility at 16–32px |
| `logo-horizontal-light.svg` / `-dark.svg` | Icon + wordmark lockup for light/dark UI chrome (nav bars, docs headers) |
| `favicon.ico` | 16/24/32/48/64px multi-res, for browsers that don't support SVG favicons |
| `icon-16.png` … `icon-512.png` | Raw PNG icon at each size |
| `icon-180.png` | Apple touch icon |
| `icon-192.png` / `icon-512.png` | Web app manifest icons (PWA / Android) |
| `logo-horizontal-light.png` / `-dark.png` | Raster lockup for places that can't render SVG (READMEs on GitHub render SVG fine, but some renderers don't) |
| `og-yantrika.png` | Standalone 1200×630 social/OG card for the new brand, if you want a separate one from the site's existing card style |

The two **existing** site OG cards — `marketing/assets/og-yantrafleet.png`
and `marketing/assets/og-vda-5050-conformance.png` — were rebuilt in place
(same filenames, so no HTML references needed to change) with the new mark
and "Yantrika" wordmark, keeping their original dark-card layout, headline
copy, and pill tags.

## Already wired in

`<link rel="icon">` / `<link rel="apple-touch-icon">` tags were added to
every page's `<head>` (all of `marketing/*.html`, `console/index.html`,
`academy/index.html`, `docs/index.html`), pointing at `/assets/brand/...`
(root-relative — works because nginx's `/` root is `marketing/` in
production; see `marketing/README.md`'s note on the same convention for
`/docs/` links, and its local-preview caveat).

## Not done here — needs a decision, not just a file

- ~~**The text rename** ("YantraFleet" → "Yantrika" in prose, `<title>`s,
  `og:site_name`, JSON-LD, page copy) — ~1,277 occurrences across 168 files.~~
  Done in a later pass, together with the domain/GitHub-org placeholders in
  `marketing/README.md`, rather than mixed into this asset commit.
- **A web app manifest** (`site.webmanifest`) referencing `icon-192.png` /
  `icon-512.png` — trivial to add once you confirm the app name you want
  Android/iOS "Add to Home Screen" to show.
- **PNG/ICO were rasterized without a real SVG renderer** (none was
  available on this machine — no `rsvg-convert`/`inkscape`/working
  `cairosvg`). They're drawn directly with PIL from the same coordinates as
  the SVGs, so they match, but if you later touch the SVGs by hand, re-run
  `python -m PIL` regeneration rather than hand-editing the PNGs to keep
  them in sync (ask and I'll regenerate).

## Colors

| Token | Hex | Use |
|---|---|---|
| Brand blue | `#2563c9` | Hex mark fill, existing `theme-color` |
| Ink | `#0f172a` | Wordmark on light backgrounds |
| Cyan accent | `#22d3ee` | Single "live/active" accent dot — use sparingly, never as a second brand color |
| Gray | `#64748b` (light) / `#94a3b8` (dark) | Tagline / secondary text |
