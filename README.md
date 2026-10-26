# Vigil

Vigil is the crypto-futures-desk engine running in your browser: today's main news, a news check from Claude's
research, genuine signals only, and how past calls turned out. It never places orders.

This repository is what Netlify deploys:

- `site/`: the built app (rebuilt from the project and pushed; not edited by hand).
- `netlify/functions/sync.mjs`: keeps your devices in step. Each device encrypts before sending, so the server
  stores only scrambled data, under a one-way hash of your sync code.
- `netlify/functions/mcp.mjs`: the Vigil connector for Claude (a remote MCP server). Claude, with your normal
  Claude login, reads Vigil's latest data and saves its research news check and the agent desk's calls for the app.
- `netlify/functions/claude.mjs`: the app's side of the connector (it uploads its latest data and collects what Claude
  left). Stored encrypted with a key made from the connector token.
- `netlify/functions/feed.mjs`: fetches news feeds that refuse browser requests (CoinDesk).
- `netlify.toml`: publish `site/`, no build step; Netlify installs `@netlify/blobs` for the functions.

## One-time setup

1. **Netlify:** Add new project, Import an existing project, GitHub, pick this repository, Deploy (the settings
   come from netlify.toml). Every later push deploys by itself; the app shows a Reload bar when it is ready.
2. **Vigil:** open the site, answer the two setup questions. On your other devices, open Settings, Sync, Turn on sync
   on the first one and scan its QR code with the others.
3. **Claude:** in Vigil, Settings, Set up the connector. Then in Claude, Settings, Connectors, Add custom connector:
   name `Vigil`, the URL Vigil shows, and the request header `Authorization` with the value Vigil shows. Turn it on
   in chats under +, Connectors, and add it to your scheduled research tasks.

Not financial advice. Most leveraged traders lose money.
