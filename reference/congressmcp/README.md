# Seed code — congressMCP's §17 harness, kept as reference only

These files are the implementation this harness was generalized from (congressMCP's `tests/e2e/`), moved here verbatim when the generic package was built under `src/mcp_e2e_harness/`. They are provenance and pedagogy, exactly like `documentation/examples/congressmcp/` is for the spec side: they contain server-specific constants (`CONGRESSMCP_*` env vars, bill-text tool names, the congress cache layout, the codex no-web provider campaign) that would be defects if found in the harness itself (`documentation/10-harness.md`, division of labor).

Nothing in here is imported by the package or the tests. When a design question comes up ("why does the harness refuse a non-empty run dir?", "why is the answer never taken from the model's own account of its tool calls?"), the comments in `run_suite.py` carry the incidents that earned each rule.
