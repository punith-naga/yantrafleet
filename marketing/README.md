# YantraFleet marketing site

A small static marketing site for YantraFleet: a landing page plus About,
Features, Security, Docs (link-out), Get Involved, and Changelog pages.
Plain hand-written HTML/CSS/JS — no build step, no bundler, no framework,
no `package.json` — consistent with the rest of this repo (`console/`,
`academy/`, `docs/`).

```
marketing/
  index.html         landing page
  about.html
  features.html
  security.html       summary + links to docs/SECURITY.md, docs/COMPLIANCE.md
  docs.html            link-out page to the real docs/ and deploy/*/README.md
  get-involved.html    GitHub Issues + CONTRIBUTING.md (no fake contact form)
  changelog.html        excerpt of the real CHANGELOG.md
  assets/styles.css    shared stylesheet for all pages
  assets/main.js        one tiny script: mobile nav toggle
  sitemap.xml
  robots.txt
```

Unlike `console/index.html` and `academy/index.html` (deliberately
single-file apps), this is a multi-page site, so the CSS lives once in
`assets/styles.css` rather than being duplicated into every page — still
plain, hand-written, unbundled CSS, just shared.

## Content sourcing

Every factual claim on these pages traces back to this repository's own
`README.md`, `docs/SECURITY.md`, `docs/COMPLIANCE.md`, or `TEST-REPORT.md`.
There is no pricing page, no customer logos or testimonials, no fabricated
usage numbers, and no invented functionality — none of that exists in the
project. The "Docs" page links out to the real documentation instead of
duplicating it.

One known wrinkle, inherited from the repo's own docs rather than
introduced here: the root `README.md`'s badge says "525 tests" while the
more granular `TEST-REPORT.md` sums to 541 across the same 10 suites. The
marketing copy sidesteps this by using the round, defensible phrasing
"10 automated test suites, 500+ tests, all passing" (true under either
number) everywhere except the Changelog page, which reproduces each
release's own historical wording verbatim.

## Before this goes live: two placeholders to replace

Both are used consistently across every page, `sitemap.xml`, and
`robots.txt`, so a find-and-replace across `marketing/` catches all of it.

1. **The domain** — every canonical link, `og:url` tag, and sitemap URL
   uses the placeholder `https://yantrafleet.example.com`. Replace it with
   your real domain everywhere it appears.
2. **The GitHub repository URL** — every "View on GitHub" / "GitHub
   repository" / "Report a bug" link uses
   `https://github.com/YOUR_GITHUB_USERNAME/yantrafleet` (the same
   placeholder convention already used in `deploy/aws/user-data.sh` and
   `deploy/azure/custom-data.sh`). Replace it with the real repository URL
   once one exists — do not publish this site with a guessed org/repo.

```bash
# from marketing/, after you know your real values:
grep -rl 'yantrafleet.example.com' . | xargs sed -i 's#https://yantrafleet.example.com#https://YOUR-REAL-DOMAIN#g'
grep -rl 'YOUR_GITHUB_USERNAME' . | xargs sed -i 's#https://github.com/YOUR_GITHUB_USERNAME/yantrafleet#https://github.com/YOUR-ORG/YOUR-REPO#g'
```

(`sed -i ''` on macOS.)

## Preview locally

These are plain files — open `index.html` directly in a browser, or serve
the directory:

```bash
cd marketing
python3 -m http.server 8095
# then open http://localhost:8095/
```

The inter-page links (`about.html`, `features.html`, …) work either way.
The links out to documentation (`../docs/...`, `../CHANGELOG.md`,
`../CONTRIBUTING.md`, `../LICENSE`) are relative to this directory's
position in the repo, so to have those resolve too, serve from the repo
root instead and browse to `/marketing/`:

```bash
# from the repo root
python3 -m http.server 8095
# then open http://localhost:8095/marketing/
```

## Deploy

### Option A — alongside console/academy/docs (recommended)

This mirrors how `console/`, `academy/`, and `docs/` are already served by
`deploy/aws/nginx/yantrafleet.conf.template` (see that file and
`deploy/azure/`): each is its own `location` block under one docroot. Add
a matching block for this site, e.g. as `/marketing/`:

```nginx
location = /marketing {
    return 301 /marketing/index.html;
}
location = /marketing/ {
    return 301 /marketing/index.html;
}
location /marketing/ {
    root /opt/yantrafleet;
    try_files $uri $uri/ =404;
}
```

With this layout the relative links in these pages (`../docs/...`,
`../CHANGELOG.md`, etc.) resolve correctly, because `/marketing/index.html`
plus `../docs/ARCHITECTURE.md` is `/docs/ARCHITECTURE.md` — exactly the
existing `/docs/` location block. If you deploy the marketing site as the
site's root instead (e.g. replacing the console's `/` redirect), change
every `../docs/`, `../CHANGELOG.md`, `../CONTRIBUTING.md`, and `../LICENSE`
link in `marketing/*.html` to drop the leading `../`.

### Option B — standalone static host

Because it's just static files with no server-side dependency, this
directory can also be deployed entirely on its own — GitHub Pages, any
static host (Netlify, Cloudflare Pages, S3 + CloudFront, etc.), or a
plain nginx/Apache vhost. In that case:

- Point the host at `marketing/` as the site root.
- The `../docs/...`, `../CHANGELOG.md`, `../CONTRIBUTING.md`, and
  `../LICENSE` links will 404 unless you either also publish those files
  at that relative location, or repoint them to the real GitHub URLs
  (e.g. `../docs/SECURITY.md` → `https://github.com/YOUR-ORG/YOUR-REPO/blob/main/docs/SECURITY.md`).
  The GitHub-Pages-only setup is the simplest fix: just swap those few
  relative doc links for their `github.com/.../blob/main/...` equivalents
  after you know the real repo URL.
- Don't forget the domain and GitHub-URL placeholder replacement above
  first — a standalone deploy makes the real domain matter immediately
  (canonical tags, `sitemap.xml`, `robots.txt`'s `Sitemap:` line).

## Editing

There's no build step to run after an edit — change the HTML/CSS/JS and
reload. If you add a new page, remember to also add it to `sitemap.xml`
and to the nav/footer link lists in every existing page (there's no
templating, so those are hand-copied across files by design).
