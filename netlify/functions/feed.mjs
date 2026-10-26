// Fetches the news feeds that refuse browser requests or Netlify's plain proxy (CoinDesk answered the proxy with 404),
// from the server with an ordinary reader's headers. Pinned to exact feed addresses: this is not a general relay.
const FEEDS = {
  coindesk: "https://www.coindesk.com/arc/outboundfeeds/rss/",
  cointelegraph: "https://cointelegraph.com/rss",
  decrypt: "https://decrypt.co/feed",
  theblock: "https://www.theblock.co/rss.xml",
};

export async function handle(req, fetchFn = fetch) {
  if (req.method !== "GET") return new Response("GET only", { status: 405 });
  const src = new URL(req.url).pathname.split("/").filter(Boolean)[2] || "";      // /api/feed/<src>/...
  const url = FEEDS[src];
  if (!url) return new Response("unknown feed", { status: 404 });
  let r;
  try {
    r = await fetchFn(url, { headers: { "User-Agent": "Mozilla/5.0 (compatible; VigilReader/1.0; +https://netlify.app)", Accept: "application/rss+xml, application/xml;q=0.9, */*;q=0.5" },
                             redirect: "follow", signal: AbortSignal.timeout(8000) });
  } catch (e) {
    return new Response("feed did not answer", { status: 502, headers: { "X-Upstream": "timeout or network" } });
  }
  const body = await r.text();
  if (!r.ok || !/<(rss|feed|channel)\b/i.test(body.slice(0, 2000))) {
    return new Response("feed answered " + r.status, { status: 502, headers: { "X-Upstream-Status": String(r.status) } });
  }
  return new Response(body, { status: 200, headers: { "Content-Type": "application/xml; charset=utf-8", "Cache-Control": "public, max-age=300",
                                                       "Netlify-CDN-Cache-Control": "public, max-age=300" } });
}

export default async (req) => handle(req);

export const config = { path: "/api/feed/*" };
