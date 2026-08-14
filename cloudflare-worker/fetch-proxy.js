/**
 * Cloudflare Worker used as a free stand-in for a paid proxy on AISource
 * rows with use_proxy=True whose block is Cloudflare identifying the
 * Django server's own datacenter IP/ASN as bot traffic (confirmed: the
 * same URLs return 200 from a residential/non-datacenter IP with no proxy
 * at all). Fetches the target URL from Cloudflare's edge network instead
 * and streams the response back untouched.
 *
 * Deploy: Cloudflare dashboard -> Workers & Pages -> Create -> paste this
 * file -> Deploy. Then set the WORKER_TOKEN secret (Settings -> Variables
 * -> Encrypt) to a long random string - anyone who doesn't know it gets a
 * 403, which stops this from becoming an open proxy for random traffic.
 *
 * Call shape: GET https://<worker>.workers.dev/?token=<WORKER_TOKEN>&url=<url-encoded target>
 */
export default {
  async fetch(request, env) {
    const requestUrl = new URL(request.url);
    const token = requestUrl.searchParams.get('token');
    if (!env.WORKER_TOKEN || token !== env.WORKER_TOKEN) {
      return new Response('Forbidden', { status: 403 });
    }

    const target = requestUrl.searchParams.get('url');
    if (!target) {
      return new Response('Missing url param', { status: 400 });
    }

    let targetUrl;
    try {
      targetUrl = new URL(target);
    } catch (e) {
      return new Response('Invalid url param', { status: 400 });
    }
    if (targetUrl.protocol !== 'http:' && targetUrl.protocol !== 'https:') {
      return new Response('Unsupported protocol', { status: 400 });
    }

    const upstream = await fetch(targetUrl.toString(), {
      headers: {
        'User-Agent': request.headers.get('User-Agent') || 'Mozilla/5.0',
        'Accept': request.headers.get('Accept') || '*/*',
      },
      redirect: 'follow',
    });

    const responseHeaders = new Headers(upstream.headers);
    responseHeaders.delete('set-cookie');

    return new Response(upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  },
};
