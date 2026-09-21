# The suite manifest — normative schema

The manifest is the entire interface between a suite (a server project's e2e material) and the harness. One JSON file, loaded verbatim. This file specifies its schema. The worked example is `examples/congressmcp/prompts.json.example` — note it predates this schema and deviates from it in the ways its README lists; the schema here is normative, the example is provenance.

Conventions: keys beginning with `_` are commentary — the harness ignores their content but they participate in the manifest content hash (the hash is over the file bytes, so a commentary edit is a new manifest version, which is correct: commentary carries scoring guidance). Unknown non-underscore keys are a load error, not a warning — a typoed knob that silently no-ops is an instrument defect waiting to be found the expensive way. **Nullability (clarified 2026-08-31, DR-2):** for any optional field, an explicit `null` is equivalent to omitting the field — both mean "not provided", and a validator must accept either. The one place `null` carries meaning of its own is `pass`/`fail`, which are required-but-nullable: null there means "scored by the named `rubric`", per the prompts table. This was always the worked example's idiom (`examples/congressmcp/` carries `"watch": null` throughout); the schema was silent on it, which is the spec defect recorded as DR-2.

## Top level

| key | required | what |
|---|---|---|
| `suite` | yes | identity: `name`, `spec` (pointer to the suite's normative spec — path or URL, recorded not fetched), `manifest_version` (suite-chosen string) |
| `server` | yes | registration of the MCP server under test — see below |
| `fixtures` | no | named grounding objects — see below |
| `rubrics` | no | named scoring rubrics for prompts with null `pass`/`fail` |
| `cells` | yes | the cell grid — see below |
| `checks` | no | declarative Layer-1 rules, schema in `30-checks.md` |
| `measurements` | no | row measurements — mechanical per-row values recorded beside the row, never outcomes; schema in `30-checks.md` → "Row measurements" (added 2026-09-18 for uscde-mcp's C1 v2 contents clause) |
| `prompts` | yes | the prompt list — see below |

## `server` — MCP registration

The generic replacement for any hard-coded server launch logic. The harness passes this to the driver as the MCP server registration, verbatim, and records verbatim what it passed.

- `name`: the server name as the consumer will see it (tool names hang off this in some drivers; it is part of the instrument, so it is pinned here, not chosen by the harness).
- `transport`: exactly one of the shapes the MCP ecosystem defines — `{type: "stdio", command, args?, env?}` or `{type: "http", url, headers?}`. The harness does not interpret the contents beyond secret handling.
- **Secret handling**: any value in `env` or `headers` given as `{"$secret": "VAR"}` is resolved from the harness's own environment at launch, is never written to any artifact, and its resolved value joins the built-in secret-hygiene scan set (`10-harness.md`). Literal secrets in the manifest are a load error if the value's key is listed in `secret_keys` — suites name which keys are sensitive; the harness cannot guess.

## `fixtures`

Named, suite-defined objects the prompts and cells refer to — documents, datasets, records, whatever the server's domain calls them. Opaque to the harness except for two fields it understands if present: `content_hash` (recorded per run when the suite's verification tooling supplies a comparison — the harness itself does not know how to fetch a fixture) and `_`-commentary. Everything else is domain data for the suite's own grounding tooling.

## `cells`

A map of cell id → cell. Fields:

| field | required | what |
|---|---|---|
| `driver` | yes | driver id (e.g. `claude-code`); the roster of supported drivers is an implementation property, but each driver's attribution contract must be recorded in `50-drivers.md` before attribution-dependent scoring |
| `model` | yes | model id, verbatim in the driver's vocabulary |
| `knobs` | yes | object of driver-native settings, verbatim (`thinking`, `reasoning_effort`, …); never translated, never defaulted by the harness |
| `role` | yes | short matrix role (`floor`, `ceiling`, `capability-floor`, `isolation`, `cross-vendor-*`, or suite-defined) |
| `context` | yes | `fresh` or `crowded`; `crowded` means the prompt lands mid-way through a harness-owned crowding procedure with other tools registered (ruling S6) |
| `crowding` | when `context` is `crowded` | `{procedure, collision_review}`: `procedure` names a harness-provided crowding procedure (versioned name, e.g. `neutral-file-triage@1`); `collision_review` is the suite's dated attestation that the procedure's content is disjoint from the server's domain. Suites never supply crowding content; the harness records the procedure's content hash in meta |
| `tool_surface` | yes | `"full"` or an explicit list of tool names the driver exposes to the consumer; attribution-dependent scoring is valid only for list-valued surfaces (`10-harness.md`) |
| `merge_gating` | yes | boolean; whether a failure blocks the suite owner's merge |
| `groups` | yes | which prompt groups run in this cell |
| `prompts` | no | explicit prompt-id list, narrowing `groups` |
| `variant` | no | name of the prompt variant this cell uses (e.g. `single_step`); default is the base `prompt` |
| `setup` | no | ordered list of `{tool, args}` calls the harness makes directly against the server before the prompt — never via a model turn; what ran and what it returned is recorded in meta. The generic form of the ancestor's cache pre-warm. **Setup runs in its own server process, not the scored turn's** (observed 2026-09-21 in every row of `runs/2026-09-21-wo6/`, before and after WO-6: the setup response and the scored trace report different server pids), which follows from the product drivers spawning the server themselves. So setup carries only state that outlives a process — a disk cache, a database, an upstream — and the location must be one both processes share, which a neutral per-invocation working directory is not guaranteed to be; in-memory state seeded by setup does not reach the scored turn |
| `env` | no | extra environment for the server process in this cell (same secret handling as `server.env`); joins cell identity (`10-harness.md` → Cells, 2026-09-21), so two cells differing only here are different cells — the shape of a server-side A/B arm. On a key that `server.transport.env` also sets, the cell's value wins (ruled 2026-09-21: the more specific declaration governs, and an arm that overrides a base value is the expected use); behavior on collision is unobserved as of that date and is verified under WO-6 |
| `notes` | no | free text: what the cell isolates, and any preregistration for it |

The ancestor's `cache: {mode: cold}` semantics — a fresh, empty state directory per invocation — generalize to: **the harness always provides a fresh neutral working directory and fresh server process per cell invocation**; any state carried in must arrive via `setup` or `env`, explicitly. There is no warm-by-accident.

### Loop-driver cells (`driver: "loop"`) — schema frozen 2026-09-18

The concrete consumer that the 2026-09-11 deferral waited for is the maintainer's own floor use (real floor models on OpenRouter); the schema is frozen on that. The common fields above apply unchanged; a loop cell adds or constrains these:

| field | required | what |
|---|---|---|
| `endpoint` | yes | a **named deployment**, currently only `openrouter`. The base URL and the credential (`OPENROUTER_API_KEY` from the harness's environment) are harness configuration, never manifest content — a manifest carries no hosts and no secrets. A direct-vendor deployment is a further name added here when a suite needs it |
| `model` | yes | the deployment's model id verbatim (`openai/gpt-oss-120b`). No routing-shortcut suffixes (`:free` and kin): a suffix is a routing preference, not an identity, and routing is expressed in `provider` |
| `provider` | no | an endpoint tag verbatim from the deployment's endpoint listing (`deepinfra/bf16`). Present: the driver sends it as the sole allowed provider with fallbacks disabled and `require_parameters` set, the pin joins cell identity, and a served-provider mismatch BREAKS the cell (`50-drivers.md` → loop, requirement 4). Absent: the cell is unpinned and every row it produces carries `reproducibility: unpinned` |
| `scaffold` | yes | the harness loop scaffold, `name@version`, pinned by content hash in `40-instruments.md`; joins cell identity like a crowding procedure does |
| `knobs` | yes | the deployment's request fields verbatim — for OpenRouter the unified `reasoning` object (`{"effort": "low"}`), `temperature`, `max_tokens`, and so on. Never translated to another vendor's scale (`10-harness.md`). A floor-role reasoning model carries its **minimum** effort here, since most cannot switch reasoning off; whether the knob is honored is Q8 P-knob-drop territory until measured |
| `data_policy` | no | `deny` (default) or `allow`; `allow` is the recorded opt-out of requirement 7's provider preference |
| `budget_usd` | no | a per-cell spend cap: reaching it stops **that cell** (remaining prompts skipped, completed rows stand) and continues the run; the run-level cap, harness configuration, stops the run. Either stop is recorded as a run-level outcome with its scope (ruled 2026-09-18 on WO-1 finding 6: a cell cap that halted the whole grid would be unusable) |

`tool_surface` for a loop cell is constructed by the harness from the server's advertised tools, so `"full"` and a list have exactly the S7 meaning: the wire tools array must equal the surface on every scored request.

## `prompts`

| field | required | what |
|---|---|---|
| `id`, `group`, `title` | yes | identity; `id` unique across the manifest |
| `prompt` | yes | the text given to the consumer, verbatim |
| `variants` | no | map of variant name → alternative text (e.g. `single_step`); a cell selects by `variant` |
| `fixture` | no | fixture name this prompt's grounding depends on; null for prompts that deliberately name no target |
| `sourcing` | yes | `measured` (grounded by measurement, authored by the suite), `derived` (composed from hints; indicative only), or `verbatim-original` (a real user's question, verbatim) |
| `grounding` | yes for `measured` | the citation: what was measured, when, and how to reproduce it |
| `pass`, `fail` | yes, nullable | the pinned criteria; both null only when `rubric` names an entry in `rubrics` |
| `rubric` | conditional | rubric name; required when `pass`/`fail` are null |
| `watch` | no | non-criterion observations to carry into scoring; editing `watch` after a run is allowed (it pins attention, not scoring) |

## Versioning and hashing

The manifest content hash recorded per run is the SHA-256 of the manifest file bytes. Two runs are comparable only when their manifest hashes match or the suite's decision record explains the delta. The harness never normalizes, reformats, or re-serializes the manifest.
