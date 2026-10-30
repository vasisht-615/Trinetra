# Trinetra

Trinetra is the crypto-futures-desk engine running in your browser: today's main news, a news check from Claude's
research, genuine signals only, and how past calls turned out. It never places orders.

This repository is what Netlify deploys:

- `site/`: the built app (rebuilt from the project and pushed; not edited by hand).
- `netlify/functions/sync.mjs`: keeps your devices in step. Each device encrypts before sending, so the server
  stores only scrambled data, under a one-way hash of your sync code.
- `netlify/functions/mcp.mjs`: the Trinetra connector for Claude (a remote MCP server). Claude, with your normal
  Claude login, reads Trinetra's latest data and saves its research news check and the agent desk's calls for the app.
- `netlify/functions/claude.mjs`: the app's side of the connector (it uploads its latest data and collects what Claude
  left). Stored encrypted with a key made from the connector token.
- `netlify/functions/pair.mjs`: links a new device with an 8-character code (10 minutes, one use). The sync code is
  left here encrypted with a key made from the short code, so the server never sees either code.
- `netlify/functions/feed.mjs`: fetches news feeds that refuse browser requests (CoinDesk).
- `netlify/functions/watchdog.mjs`: runs every 2 minutes and sends phone alerts through ntfy, even when the app is
  closed: a reminder an hour before each big US release, and the SOL rule's first-hour answer after Fed decisions. It
  reads Coinbase prices (Netlify's servers can't reach Binance), so an alert means "open Trinetra"; the app decides.
- `tests/`: the watchdog's tests (`node --test tests/*.test.mjs`) and the engine's scenario tests, which run the real
  `desk.py` pipeline against a scripted exchange with the clock pinned (`python3 -m unittest discover -s tests/engine -p
  'test_*.py'`; the full news-day matrix is `python3 tests/engine/run_matrix.py`, and `tests/engine/replay_history.py`
  replays the news rules on real Binance history from data.binance.vision).
- `tools/stamp_build.py`: after editing anything in `site/engine`, re-stamps the checksums and the build id so installed
  apps load the new engine.
- `netlify.toml`: publish `site/`, no build step; Netlify installs `@netlify/blobs` for the functions.

## One-time setup

1. **Netlify:** Add new project, Import an existing project, GitHub, pick this repository, Deploy (the settings
   come from netlify.toml). Every later push deploys by itself; the app shows a Reload bar when it is ready.
2. **Trinetra:** open the site, answer the two setup questions. Then Settings, Sync and devices, Turn on sync, and Link a
   device: type the 8-character code it shows on each other device (on iPhone, in the Home Screen app, which keeps its
   own data apart from Safari).
3. **Claude:** in Trinetra, Settings, Set up the connector. Then in Claude, Settings, Connectors, Add custom connector:
   name `Trinetra` and the private URL Trinetra shows (leave the OAuth fields empty). Turn it on in chats under +,
   Connectors, and add it to your scheduled research tasks.
4. **Phone alerts (optional):** install the ntfy app and subscribe to a long random topic name (it works as a password,
   so keep it private). In Netlify, Site configuration, Environment variables, add `NTFY_TOPIC` with that name, then
   deploy again. The first alert, "Trinetra alerts are on", confirms it works.


