# Blocked-feed mirror

Some AISource feeds live behind a WAF that rejects this server's own IP
(and, inconsistently, the Cloudflare Worker's edge IP too - see
`cloudflare-worker/fetch-proxy.js`) but respond fine to GitHub's network.
`.github/workflows/mirror-blocked-feeds.yml` fetches every feed listed in
`mirror-config.json` from a GitHub Actions runner every 15 minutes and
publishes the result to the `feeds-mirror` branch. The matching
`AISource.url` in Django then points at the mirrored copy instead of the
real site, so the feed-level fetch that determines the "فشل جلب المصدر"
AIImportLog status never touches the blocked site at all. Per-article image/
full-text scraping still hits the real site directly (or via the proxy, if
`use_proxy` stays on) - that's fine, those failures are already silently
tolerated and never marked as a source failure.

## Diagnosing a newly-blocked source

Before reaching for this, confirm it's actually needed - most sources work
fine directly:

1. `python manage.py audit_proxy_sources` - if it can flip the source off
   `use_proxy`, it doesn't need anything special.
2. If it still 403s direct and via the Cloudflare Worker, it's a candidate
   for mirroring here.

## Adding a source

1. Add `{"name", "url", "output"}` to `mirror-config.json` (`output` is the
   path the mirrored copy is written to, conventionally `feeds/<slug>.xml`).
2. Push to `main` and trigger the workflow once manually (Actions tab ->
   "Mirror blocked feeds" -> "Run workflow") so the file exists on
   `feeds-mirror` immediately instead of waiting up to 15 minutes.
3. Point that AISource's `url` at:
   `https://raw.githubusercontent.com/<owner>/<repo>/feeds-mirror/<output>`
