# YantraFleet marketing site

The public site: a landing page whose primary call to action starts a live
fleet with no signup, a deep page on the free VDA 5050 conformance tester,
plus About, Features, Security, Docs (link-out), Get Involved, Changelog and
a site search. Plain hand-written HTML/CSS/JS — no build step, no bundler, no
framework, no `package.json` — consistent with the rest of this repo
(`console/`, `academy/`, `docs/`).

```
marketing/
  index.html                       landing page + the "Try it" button
  vda-5050-conformance-test.html   the conformance tester (SEO landing page)
  about.html
  features.html
  security.html                    summary + links to docs/SECURITY.md, docs/COMPLIANCE.md
  docs.html                        link-out page to the real docs/
  get-involved.html                GitHub Issues + CONTRIBUTING.md (no fake contact form)
  changelog.html                   excerpt of the real CHANGELOG.md
  search.html                      client-side search over the pages above
  assets/styles.css                shared stylesheet for all pages
  assets/main.js                   one tiny script: mobile nav toggle
  assets/demo.js                   progressive enhancement for the "Try it" button
  assets/og-yantrafleet.png        1200x630 OpenGraph/Twitter card (site-wide)
  assets/og-vda-5050-conformance.png   1200x630 card for the conformance page
  sitemap.xml
  robots.txt
```

Unlike `console/index.html` and `academy/index.html` (deliberately
single-file apps), this is a multi-page site, so the CSS lives once in
`assets/styles.css` rather than being duplicated into every page — still
plain, hand-written, unbundled CSS, just shared.

## Two filenames that must not change

* **`vda-5050-conformance-test.html`** — every HTML report `yantra-conform`
  generates hard-codes a footer link to
  `<your-domain>/vda-5050-conformance-test.html`
  (`tools/conformance/yantraconform/report_html.py`, `TOOL_URL`). Renaming
  this file breaks that link in every report already sitting in somebody's
  inbox. Both `deploy/*/validate.sh` pin the name.
* **`assets/styles.css`** — every page links it; there is no fallback.

## The "Try it with a live fleet" button

`index.html`'s primary call to action is a plain
`<form method="post" action="/api/demo/session">`. That is the whole
feature, and it works with JavaScript off: the door
(`ops/yantraops/sandbox_http.py`) sees `text/html` in the `Accept` header,
answers `303` with the new sandbox's console URL in `Location:`, and the
browser follows it into a running fleet.

`assets/demo.js` only *enhances* it — it asks `GET /api/demo/limits` whether
the demo is available and hides the block if not, and it turns a `429`/`503`
into a sentence rather than a browser error page. Blocked or broken, the
form underneath is untouched.

For this to work at all, the deployment must:

1. serve `/api/demo/` (the shared nginx template already has the location and
   its rate-limit zone — see `deploy/aws/nginx/yantrafleet.conf.template`);
2. have applied `supabase/0009_demo_sandbox.sql`, deliberately, after reading
   its THREAT MODEL block;
3. have `yantra-sandbox` and `yantra-sandbox-reap.timer` enabled, and a
   service_role key in `/etc/yantrafleet-sandbox.env` for the reaper.

Steps 2 and 3 are written out in both `deploy/aws/README.md` and
`deploy/azure/README.md`. Until they are done the button is present but the
door answers `502`; `demo.js` hides it on a deployment where
`/api/demo/limits` says the demo is unavailable.

## Content sourcing

Every factual claim on these pages traces back to this repository's own
`README.md`, `docs/SECURITY.md`, `docs/COMPLIANCE.md`, `TEST-REPORT.md`,
`tools/conformance/README.md`, `connector/README.md`, `academy/README.md`, or
`ops/yantraops/sandbox_http.py`. There is no pricing page, no customer logos
or testimonials, no fabricated usage numbers, and no invented functionality —
none of that exists in the project.

The 52-row check table on `vda-5050-conformance-test.html` is a rendering of
the tester's own catalogue, i.e. the output of `yantra-conform checks --json`
(`tools/conformance/yantraconform/checks.py`). Check ids, titles, "expected"
lines, severities and clause references are copied from there rather than
written by hand. **If the rule set changes, regenerate that table** rather
than editing rows individually — `yantra-conform checks --json` prints the
whole set with no broker and no network.

One known wrinkle, inherited from the repo's own docs rather than introduced
here: the root `README.md`'s badge says "525 tests" while the more granular
`TEST-REPORT.md` sums to 633 across the same 10 suites. The marketing copy
sidesteps this with the round, defensible phrasing "10 automated test suites,
500+ tests, all passing" (true under either number) everywhere except the
Changelog page, which reproduces each release's own historical wording
verbatim. The conformance tester's own 188 tests are quoted separately
because they are a separate suite (`tools/conformance/tests`) that
`TEST-REPORT.md` does not yet count.

## Before this goes live: two placeholders to replace

Both are used consistently across every file listed below, so a find-and-
replace across `marketing/` catches all of it. Run the two commands at the
end of this section and then verify with the grep in "Check your work".

### 1. The domain — `https://yantrafleet.example.com`

| File | Tags / lines carrying it |
|---|---|
| `index.html` | `<link rel="canonical">`, `og:url`, `og:image`, `twitter:image`, and the JSON-LD `@graph`: `WebSite` `@id`+`url`, its `SearchAction` `urlTemplate`, `SoftwareApplication` `@id`+`url`, `FAQPage` `@id` |
| `vda-5050-conformance-test.html` | `<link rel="canonical">`, `og:url`, `og:image`, `twitter:image`, and the JSON-LD `@graph`: `SoftwareApplication` `@id`+`url`+`isPartOf`, `FAQPage` `@id`, both `BreadcrumbList` `item` URLs |
| `about.html`, `features.html`, `security.html`, `docs.html`, `get-involved.html`, `changelog.html` | `<link rel="canonical">`, `og:url`, `og:image`, `twitter:image`, and both `BreadcrumbList` `item` URLs |
| `search.html` | `<link rel="canonical">` |
| `sitemap.xml` | every `<loc>` (8 of them) |
| `robots.txt` | the `Sitemap:` line (and the comment header) |

As of this writing that is 11 occurrences each in `index.html` and
`vda-5050-conformance-test.html`, 6–7 in each other page, 1 in `search.html`,
9 in `sitemap.xml` and 2 in `robots.txt`. Do not count by hand — use the grep
under "Check your work".

**One more place, outside this directory.** The conformance tester bakes a
site URL into every report it generates:
`tools/conformance/yantraconform/report_html.py` sets `HOME_URL` and
`TOOL_URL` to `https://yantrika.ai/...`. That is a real domain, not the
`example.com` placeholder, so the grep below will not catch it. If
`yantrika.ai` is your domain there is nothing to do; if it is not, change it
there as well or every report you hand a vendor links to somebody else's
site.

### 2. The GitHub repository URL — `https://github.com/YOUR_GITHUB_USERNAME/yantrafleet`

Same placeholder convention already used in `deploy/aws/user-data.sh` and
`deploy/azure/custom-data.sh`. It appears in:

| File | Where |
|---|---|
| every `*.html` | the nav "View on GitHub" button, the footer "GitHub repository" and "License (MIT)" links |
| `index.html` | additionally: the `git clone` line in the quickstart block, the JSON-LD `codeRepository`, the two `deploy/*/README.md` links |
| `vda-5050-conformance-test.html` | additionally: the `git clone` line, the JSON-LD `codeRepository`, and three `/tree/main/tools/conformance` links |
| `docs.html`, `get-involved.html`, `about.html`, `changelog.html` | additionally: the in-page links to `CONTRIBUTING.md`, `LICENSE`, `CHANGELOG.md`, `docker/`, `deploy/*/README.md` and issue templates, which point at GitHub precisely because nginx does not serve the repo root |
| `assets/demo.js` | the fallback link shown when the live demo is unavailable |

```bash
# from marketing/, after you know your real values:
grep -rl 'yantrafleet.example.com' . | xargs sed -i 's#https://yantrafleet.example.com#https://YOUR-REAL-DOMAIN#g'
grep -rl 'YOUR_GITHUB_USERNAME' . | xargs sed -i 's#https://github.com/YOUR_GITHUB_USERNAME/yantrafleet#https://github.com/YOUR-ORG/YOUR-REPO#g'

# only if yantrika.ai is NOT your domain -- the links inside every
# generated conformance report:
sed -i 's#https://yantrika.ai#https://YOUR-REAL-DOMAIN#g' \
    ../tools/conformance/yantraconform/report_html.py
```

(`sed -i ''` on macOS.)

### Check your work

```bash
# from the repo root — both must print nothing before you publish
grep -rn 'yantrafleet.example.com' marketing/ || echo "domain: clean"
grep -rn 'YOUR_GITHUB_USERNAME' marketing/  || echo "repo: clean"
```

A site published with the placeholder domain still in its canonical tags is
*worse* than one with no tags at all: every page tells the crawler its real
address is somewhere else.

## Preview locally

These are plain files. Serve from the **repo root**, not from `marketing/`,
so the links into `/docs/`, `/console/` and `/academy/` resolve the way they
do in production:

```bash
# from the repo root
python3 -m http.server 8095
# then open http://localhost:8095/marketing/
```

Note the one difference from production: under nginx the marketing site *is*
the host root, so `/docs/SECURITY.md` resolves directly; under the command
above the site lives at `/marketing/` while `/docs/` is still at the root, so
the root-relative links work but the page-to-page links need the
`/marketing/` prefix the browser already has. Opening `index.html` straight
off disk works too, for everything except the root-relative links and the
demo button.

## Deploy

The marketing site owns the bare host root. That is already wired up: the
shared nginx template (`deploy/aws/nginx/yantrafleet.conf.template`, used by
**both** the AWS and Azure kits) does

```nginx
root /opt/yantrafleet/marketing;
index index.html;
location / { try_files $uri $uri/ =404; }
```

with `/console/`, `/academy/`, `/docs/`, `/ask`, `/health` and `/api/demo/`
as sibling locations. Nothing in this directory needs its own deploy step —
the boot scripts clone the repo to `/opt/yantrafleet` and nginx serves it.

**Link rule, learned the hard way:** nginx serves *only* the locations in
that template. It does **not** serve the repo root or `deploy/`, so a link to
`../LICENSE`, `../CONTRIBUTING.md`, `../CHANGELOG.md` or
`../deploy/aws/README.md` is dead in production even though it works when you
preview from the repo root. Those four are linked to GitHub instead,
deliberately. Links into `/docs/` are fine (that location exists) and are
written root-relative — `/docs/SECURITY.md`, not `../docs/SECURITY.md` — so
they cannot be misread. Before adding any link out of this directory, check
it against that template.

### Standalone static host

Because it's just static files, this directory can also be deployed on its
own — GitHub Pages, Netlify, Cloudflare Pages, S3 + CloudFront, a plain
vhost. Two things stop working, and you should decide what to do about each:

- **`/docs/`, `/console/`, `/academy/`** — repoint those links at wherever
  those actually live, or drop them.
- **the "Try it with a live fleet" button** — there is no `/api/demo/`
  endpoint on a static host, so the form would post into a 404. Either
  point the form's `action` at the full URL of a host that *is* running
  `yantra-sandbox`, or remove the `<div class="demo">` block from
  `index.html` (and the `demo.js` `<script>` tag with it).

Everything else — including the whole conformance page, which is the main
thing worth ranking — works unchanged.

## Editing

There's no build step: change the HTML/CSS/JS and reload. If you **add a
page**, four things need updating by hand, because there is no templating
here by design:

1. `sitemap.xml`
2. the top-nav list in *every* existing page (it is hand-copied)
3. the footer link columns in *every* existing page
4. the `PAGES` array in `search.html`

And give it a unique `<title>`, a unique `<meta name="description">`, its own
`canonical`, `og:*` and `twitter:*` tags, and a `BreadcrumbList` — then add
the new file to the placeholder tables above so the next person replacing the
domain does not miss it.
