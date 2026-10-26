/* The engine's home: Pyodide (Python compiled to WebAssembly) runs desk.py here, off the page's thread. The data
   folder (/home/desk: settings, journal, runs) lives in IndexedDB and is saved after every call. */
// The Python runtime lives in a versioned folder (cached for a year; a new Pyodide version gets a new folder).
const RUNTIME = "runtime/pyodide-0.26.4/";

// The 10 MB WebAssembly file ships gzipped (Netlify Drop stalls on files over 10 MB, and it halves the download).
// Pyodide fetches it with the global fetch, so that one request is answered here, decompressed.
const realFetch = self.fetch.bind(self);
self.fetch = async (input, init) => {
  const url = input instanceof Request ? input.url : String(input);
  if (!url.endsWith("/pyodide.asm.wasm")) return realFetch(input, init);
  if (typeof DecompressionStream === "undefined") throw new Error("this browser is too old for the app (it needs Chrome 80+, Safari 16.4+ or Firefox 113+)");
  const res = await realFetch(url + ".gz");
  if (!res.ok) throw new Error("could not load the Python runtime (HTTP " + res.status + ")");
  const buf = new Uint8Array(await res.arrayBuffer());
  const gz = buf[0] === 0x1f && buf[1] === 0x8b;          // a server may already have decoded it
  const body = gz ? new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip")) : buf;
  return new Response(body, { status: 200, headers: { "Content-Type": "application/wasm" } });
};

importScripts(RUNTIME + "pyodide.js");

let py = null;
// One save at a time, in order: a log entry written while a run is saving waits for it instead of racing it.
let saving = Promise.resolve();
function save() {
  saving = saving.catch(() => {}).then(() => syncfs(false));
  return saving;
}

function syncfs(populate) {
  return new Promise((resolve, reject) => py.FS.syncfs(populate, (err) => (err ? reject(err) : resolve())));
}

async function init() {
  py = await loadPyodide({ indexURL: new URL(RUNTIME, self.location.href).href });
  py.FS.mkdirTree("/home/desk");
  py.FS.mount(py.FS.filesystems.IDBFS, {}, "/home/desk");
  await syncfs(true);
  const files = await (await fetch("engine/files.json", { cache: "no-cache" })).json();
  for (const f of files.files) {
    const res = await fetch("engine/" + f.path, { cache: "no-cache" });
    if (!res.ok) throw new Error("missing engine file " + f.path);
    const dir = "/engine/" + f.path.split("/").slice(0, -1).join("/");
    py.FS.mkdirTree(dir);
    py.FS.writeFile("/engine/" + f.path, new Uint8Array(await res.arrayBuffer()));
  }
  const bridge = await (await fetch("bridge.py", { cache: "no-cache" })).text();
  py.runPython(bridge);
  py.globals.get("housekeeping")();
  await syncfs(false);
  return JSON.parse(py.globals.get("info")());
}

const ready = init();

self.onmessage = async (event) => {
  const { id, fn, args } = event.data;
  try {
    const meta = await ready;
    if (fn === "ready") {
      self.postMessage({ id, ok: true, result: meta });
      return;
    }
    const allowed = ["run", "read_text", "chart_bars", "connection_check", "export_data", "import_data", "fed_nights_ics", "info", "upcoming",
      "settings", "save_settings", "repair_settings", "health", "usdt_rate", "log_event", "read_log", "records", "agent_call",
      "perp_check", "perp_cached", "diagnostics", "sync_export", "sync_merge", "news_set", "news_get", "news_clear",
      "claude_link_get", "claude_link_set", "claude_link_clear"];
    if (!allowed.includes(fn)) throw new Error("unknown call " + fn);
    const result = py.globals.get(fn)(...(args || []));
    // Only calls that can change the data folder are saved to the browser's storage (one tab writes: see app.js).
    if (["run", "chart_bars", "import_data", "save_settings", "repair_settings", "log_event", "agent_call", "perp_check", "sync_merge", "news_set", "news_clear",
      "claude_link_set", "claude_link_clear"].includes(fn)) await save();
    self.postMessage({ id, ok: true, result });
  } catch (err) {
    self.postMessage({ id, ok: false, error: String((err && err.message) || err) });
  }
};
