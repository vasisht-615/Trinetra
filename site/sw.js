/* Offline shell: the app files and the Python runtime are cached; market data and feeds always go to the network.
   A new version waits until the page asks for it (the "Reload" bar), so one page never mixes files from two versions. */
const CACHE = "desk-1.3.2-345a11bfbb3b";
const SHELL = ["./", "app.css", "app.js", "bridge.py", "engine/files.json", "engine/references/event_stats.json", "engine/references/release_calendar.json", "engine/references/release_history.json", "engine/references/scenarios.json", "engine/scripts/desk.py", "fonts/atkinson-hyperlegible-next-latin-wght-normal.woff2", "fonts/bricolage-grotesque-latin-wght-normal.woff2", "icon-180.png", "icon-512.png", "icon.svg", "index.html", "manifest.webmanifest", "runtime/pyodide-0.26.4/pyodide-lock.json", "runtime/pyodide-0.26.4/pyodide.asm.js", "runtime/pyodide-0.26.4/pyodide.asm.wasm.gz", "runtime/pyodide-0.26.4/pyodide.js", "runtime/pyodide-0.26.4/python_stdlib.zip", "vendor/LICENSE-lightweight-charts.txt", "vendor/lightweight-charts.standalone.production.js", "vendor/qrcode.js", "vigil.pine", "worker.js"];
self.addEventListener("install", (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL))));
self.addEventListener("activate", (e) => e.waitUntil(caches.keys()
  .then((ks) => Promise.all(ks.filter((k) => k.startsWith("desk-") && k !== CACHE).map((k) => caches.delete(k))))
  .then(() => self.clients.claim())));
self.addEventListener("message", (e) => { if (e.data === "update-now") self.skipWaiting(); });
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== self.location.origin || u.pathname.includes("/proxy/")) return;
  e.respondWith(caches.open(CACHE).then((c) => c.match(e.request, { ignoreSearch: true })).then((hit) => hit || fetch(e.request)));
});
