# OpenRouter tool-capability probe — 2026-09-18 (spec session, external instrument)

**Status of this evidence.** These are attributed external measurements in the sense of `50-drivers.md`: made by a spec-session script against OpenRouter's chat-completions API, not by the harness's loop driver under its wire recorder. They inform the roster in `SUITE-AUTHORING.md` and the Q8 outcomes in `90-open-questions.md`; they do **not** settle Q8, which requires the same effects reproduced under harness instruments. Total spend for everything on this page: $0.0005.

## Method — reproduce before trusting

One request per row, `POST https://openrouter.ai/api/v1/chat/completions`, with: a two-message conversation (system: "You are a tool-using assistant. When a tool can answer, call it; do not answer from memory."; user: "Look up the probe value stored under the key 'alpha' and tell me what it is."); one tool, `get_probe_value(key: string)`; `max_tokens: 400`; `temperature: 0`; `reasoning: {"effort": "low"}` except where noted; `usage: {"include": true}`; `provider: {"data_collection": "deny"}`. A **pinned** row adds `provider.order: ["<slug>"]`, `allow_fallbacks: false`, `require_parameters: true`, and `quantizations: ["bf16"]` where the table says bf16. Well-formed means: exactly one tool call, name `get_probe_value`, arguments parse as JSON with `key == "alpha"`. Served provider is the response's `provider` field — OpenRouter's report, the only identity available at this layer (the harness driver records the same field; requirement 4).

## Roster probe (P-tool-probe) and served providers (P-provider-pin)

| model | pin (slug[/quant]) | served | tool calls | well-formed | tokens in / out / reasoning | cost USD |
|---|---|---|---|---|---|---|
| `openai/gpt-oss-120b` | deepinfra | DeepInfra | 1 | yes | 163 / 45 / 13 | 0.000014 |
| `openai/gpt-oss-20b` | deepinfra | DeepInfra | 1 | yes | 163 / 36 / 13 | 0.000010 |
| `deepseek/deepseek-v4-flash` | — | StreamLake | 1 | yes | 338 / 74 / 27 | 0.000024 |
| `z-ai/glm-5.3-flash` | — | StreamLake | 1 | yes | 215 / 13 / 0 | 0.000036 |
| `qwen/qwen3.7-flash` | — | Alibaba | 1 | yes | 328 / 150 / 122 | 0.000029 |
| `mistralai/mistral-small-2603` | mistral | Mistral | 1 | yes | 137 / 53 / 40 | 0.000052 |
| `openai/gpt-5.4-nano` | openai | refused | — | — | — | HTTP 404: no endpoints can handle the requested parameters |
| `google/gemini-3.5-flash-lite` | google-ai-studio | Google AI Studio | 1 | yes | 102 / 18 / 0 | 0.000076 |
| `meta-llama/llama-4-maverick` | — | DigitalOcean | 1 | yes | 216 / 8 / 0 | 0.000046 |
| `openai/gpt-oss-120b` | — | AkashML | 1 | yes | 174 / 42 / 15 | 0.000012 |
| `openai/gpt-oss-120b` | — | AkashML | 1 | yes | 174 / 41 / 13 | 0.000012 |
| `openai/gpt-oss-120b` | — | AkashML | 1 | yes | 174 / 42 / 15 | 0.000012 |
| `openai/gpt-oss-120b` | — | DeepInfra | 1 | yes | 163 / 45 / 13 | 0.000014 |
| `openai/gpt-oss-120b` | — | AkashML | 1 | yes | 174 / 41 / 13 | 0.000012 |
| `openai/gpt-oss-120b` | — | AkashML | 1 | yes | 174 / 41 / 13 | 0.000012 |
| `openai/gpt-oss-120b` | — | AkashML | 1 | yes | 174 / 41 / 13 | 0.000012 |
| `openai/gpt-oss-120b` | — | AkashML | 1 | yes | 174 / 41 / 13 | 0.000012 |
| `openai/gpt-oss-120b` | — | AkashML | 1 | yes | 174 / 41 / 13 | 0.000012 |
| `openai/gpt-oss-120b` | — | AkashML | 1 | yes | 174 / 42 / 15 | 0.000012 |

Reading: every model that ran returned one well-formed call. Every pinned request was served by its pin (9 of 9, N=1 each). The unpinned `openai/gpt-oss-120b` baseline (10 repeats) was served by two providers — AkashML ×9, DeepInfra ×1 — so routing varies within a single afternoon at N=10, and none of the 10 landed on an endpoint whose listing lacks tools. `meta-llama/llama-4-maverick` ran on the `avoid` row of the roster to see whether it would: it did, at DigitalOcean, so the avoid rating rests on the endpoint spread (2 of 5 without tools) and the missing reasoning knob, not on a failed call.

## Knob refusal vs. silent swallow (P-knob-drop) — `openai/gpt-5.4-nano`, pin `openai`

| request | outcome | tool calls | well-formed |
|---|---|---|---|
| strict, temp=0, reasoning low | refused (HTTP 404) | — | — |
| strict, no temp, reasoning low | OpenAI | 1 | yes |
| strict, no temp, no reasoning | OpenAI | 1 | yes |
| NOT strict, temp=0, reasoning low | OpenAI | 1 | yes |

Reading: the model does not accept `temperature`. With `require_parameters: true` the router **refused** the request outright (HTTP 404, "No endpoints found that can handle the requested parameters"). With `require_parameters: false` the **identical** request succeeded with no indication anywhere in the response that the knob was dropped — the silent swallow that Q8 P-knob-drop named as its falsifier, observed at N=1. The router's strict flag is therefore the loud path, and it is a router-level filter on declared endpoint parameters: it says nothing about whether a backend honors a knob it declares, which remains unobservable from here.
