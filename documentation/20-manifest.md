# The suite manifest — normative schema

The manifest is the entire interface between a suite (a server project's e2e material) and the harness. One JSON file, loaded verbatim. This file specifies its schema. The worked example is `examples/congressmcp/prompts.json.example` — note it predates this schema and deviates from it in the ways its README lists; the schema here is normative, the example is provenance.

Conventions: keys beginning with `_` are commentary — the harness ignores their content but they participate in the manifest content hash (the hash is over the file bytes, so a commentary edit is a new manifest version, which is correct: commentary carries scoring guidance). Unknown non-underscore keys are a load error, not a warning — a typoed knob that silently no-ops is an instrument defect waiting to be found the expensive way.

## Top level

| key | required | what |
|---|---|---|
| `suite` | yes | identity: `name`, `spec` (pointer to the suite's normative spec — path or URL, recorded not fetched), `manifest_version` (suite-chosen string) |
| `server` | yes | registration of the MCP server under test — see below |
| `fixtures` | no | named grounding objects — see below |
| `rubrics` | no | named scoring rubrics for prompts with null `pass`/`fail` |
| `cells` | yes | the cell grid — see below |
| `checks` | no | declarative Layer-1 rules, schema in `30-checks.md` |
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
| `driver` | yes | driver id (e.g. `claude-code`); the roster of supported drivers is an implementation property, but each driver's attribution capabilities must satisfy `10-harness.md` before it may host attribution cells |
| `model` | yes | model id, verbatim in the driver's vocabulary |
| `knobs` | yes | object of driver-native settings, verbatim (`thinking`, `reasoning_effort`, …); never translated, never defaulted by the harness |
| `role` | yes | short matrix role (`floor`, `ceiling`, `capability-floor`, `isolation`, `cross-vendor-*`, or suite-defined) |
| `context` | yes | context condition, one of `fresh` or `crowded`, plus free text; `crowded` means the prompt lands mid-task with other tools registered, per the driver's documented crowding procedure |
| `tool_surface` | yes | `"full"` or an explicit list of tool names the driver exposes to the consumer; attribution-dependent scoring is valid only for list-valued surfaces (`10-harness.md`) |
| `merge_gating` | yes | boolean; whether a failure blocks the suite owner's merge |
| `groups` | yes | which prompt groups run in this cell |
| `prompts` | no | explicit prompt-id list, narrowing `groups` |
| `variant` | no | name of the prompt variant this cell uses (e.g. `single_step`); default is the base `prompt` |
| `setup` | no | ordered list of `{tool, args}` calls the harness makes directly against the server before the prompt — never via a model turn; what ran and what it returned is recorded in meta. The generic form of the ancestor's cache pre-warm |
| `env` | no | extra environment for the server process in this cell (same secret handling as `server.env`) |
| `notes` | no | free text: what the cell isolates, and any preregistration for it |

The ancestor's `cache: {mode: cold}` semantics — a fresh, empty state directory per invocation — generalize to: **the harness always provides a fresh neutral working directory and fresh server process per cell invocation**; any state carried in must arrive via `setup` or `env`, explicitly. There is no warm-by-accident.

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
