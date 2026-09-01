# Handoff to mcp-e2e: Codex driver findings measured in the uscode-mcp spec session, 2026-09-01

All measured on codex-cli 0.147.0 (the ChatGPT-desktop-integrated build), macOS, this machine. The uscode-mcp spec keeps these as observations O31–O35 (its `documentation/90-observations.md`) plus its own suite gate; the driver requirements below are yours. Everything here is re-measurable; probe recipes included.

## 1. The web-search knob is dead under BOTH auth modes (O31, O33)

`-c tools.web_search=false` + `--sandbox read-only` left the web-search tool live and used under ChatGPT auth (7 invocations, incl. an r.jina.ai proxy fetch attempt) AND under API-key auth (9 invocations, incl. a DNS-over-HTTPS attempt via dns.google to route around the shell sandbox). Login mode is not a control. This independently corroborates the congressMCP F-series finding; the congressMCP noweb-provider mechanism is the fix and was reproduced here (below).

Sharpest exhibit: in the ChatGPT-auth locked run the model FABRICATED the fetch target (`2024-05-20T15:30:22Z` against a pinned truth of `2025-09-24T16:23:38Z`) — a web tool that is present-but-inadequate manufactures false attribution. The API-auth runs answered honestly. n=1 each.

Probe recipe: pin a fetch-only-answerable truth first (e.g. `lastModified` from a govinfo package summary, verified by direct curl), then `codex exec` with the prompt "Fetch <url> and report the exact value... If you cannot access the network at all, reply with exactly: NO NETWORK ACCESS", counting `web search:` events in output.

## 2. The noweb custom provider works — reproduced (O34)

Custom provider at the SAME `https://api.openai.com/v1`, `wire_api="responses"` (note: `wire_api="chat"` is refused by 0.147.0), key via env: zero web-search events, sandboxed curl failed DNS, honest final answer. Mechanism per the congressMCP comment: web-tool registration follows the provider's `supports_standalone_web_search` declaration; builtins declare it and cannot be overridden; custom providers declare nothing.

## 3. Wire-level facts about custom providers (O32)

A local wiretap standing in as the provider showed, while ChatGPT-logged-in:
- Codex routes to custom providers fine under ChatGPT login, BUT the request tools array still contained `web_search` in that state — the noweb effect requires API-key credentials (matrix cell that congressMCP's comment and O34 fill in). If the driver ever runs with a ChatGPT-logged-in CODEX_HOME, the provider trick is not proven there.
- **Credential isolation**: the Authorization header to a custom provider carries exactly the provider `env_key` value (verified against a dummy), never the ChatGPT token. A proxy cannot ride ChatGPT billing; any custom-provider backend needs its own key.
- **Ambient surface leakage**: the tools array offered to the provider included the user's config-level MCP servers (`congressmcp-dev`, `node_repl`) and plugin tools. And during the O34 reproduction, `node_repl` STARTED AND RAN mid-probe — arbitrary Node, i.e. a network-capable channel no provider choice removes. **Isolated CODEX_HOME is load-bearing for attribution, not hygiene.**

## 4. Env sanitization for spawned MCP servers — corroborated (O35)

Canary MCP server spawned by codex saw `PATH` and `HOME` but not a parent-env `CANARY_E11`. Combined with the congressMCP postmortem (config env table → `mcp-config.toml` artifact; `-c` overrides → `meta.json` `command`): the key-injection spawn helper is required; process-env-only, never an artifact. (The uscode-mcp server will fail fast at startup when keyless — its spec now mandates that — so a helper failure will be loud, not an F31-style misleading error.)

## 5. The uscode-mcp suite's gate (for reference — owned by that spec)

A Codex cross-vendor cell against the uscode-mcp suite runs only with: noweb provider (env-only key) + isolated CODEX_HOME + key-injection spawn helper for the MCP server + per-run effect verification (zero web-search events in the run record; api-surface capture shows no web tool). Version-pinned; re-measure on codex upgrade.
