# Blocked-feed mirror

Some AISource feeds live behind a WAF that rejects this server's own IP
(and, inconsistently, the Cloudflare Worker's edge IP too - see
`cloudflare-worker/fetch-proxy.js`) but respond fine to GitHub's network.
`.github/workflows/mirror-blocked-feeds.yml` fetches every feed from a
GitHub Actions runner every 15 minutes and publishes the result to the
`feeds-mirror` branch. The matching `AISource.url` in Django then points at
the mirrored copy instead of the real site, so the feed-level fetch that
determines the "فشل جلب المصدر" AIImportLog status never touches the
blocked site at all. Per-article image/full-text scraping still hits the
real site directly (or via the proxy, if `use_proxy` stays on) - that's
fine, those failures are already silently tolerated and never marked as a
source failure.

The list of feeds to mirror is a `MirroredFeedConfig` row per source,
managed from the dashboard (السيستم والعمليات > المصادر المحظورة) - the
workflow pulls it live from `mirrored_feeds_config_api_view`
(`/ai-dashboard/api/mirrored-feeds-config/`) on every run.
`mirror-config.json` in this directory is only the offline fallback the
workflow falls back to if that endpoint can't be reached (e.g. the server's
down); it isn't the source of truth day to day.

## Diagnosing a newly-blocked source

Before reaching for this, confirm it's actually needed - most sources work
fine directly:

1. `python manage.py audit_proxy_sources` - if it can flip the source off
   `use_proxy`, it doesn't need anything special.
2. If it still 403s direct and via the Cloudflare Worker, it's a candidate
   for mirroring here.

## Adding a source

**Preferred - from the dashboard:** السيستم والعمليات > المصادر المحظورة >
إضافة مصدر. Pick the AISource and confirm its real feed URL. Saving points
that AISource's `url` at the mirror automatically. Trigger the workflow
once manually afterwards (Actions tab -> "Mirror blocked feeds" -> "Run
workflow") so the mirrored copy exists immediately instead of waiting up to
15 minutes for the next scheduled run.

**Fallback - if the dashboard/API is unreachable:** add
`{"name", "url", "output"}` to `mirror-config.json` directly (`output`
conventionally `feeds/<slug>.xml`), push to `main`, run the workflow once
manually, then point that AISource's `url` at:
`https://raw.githubusercontent.com/<owner>/<repo>/feeds-mirror/<output>`
