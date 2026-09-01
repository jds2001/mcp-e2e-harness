"""The codex driver: `codex exec` against a harness-authored, isolated environment.

Implements the binding contract in documentation/50-drivers.md ("codex", specified
2026-09-01 from the uscode-mcp handoff's measurements on codex-cli 0.147.0). The
seven requirements, and where each lands here:

1. **API-key credentials only.** The invocation runs against a fresh, harness-authored
   ``CODEX_HOME`` that contains no ``auth.json``, so ChatGPT-login state is absent by
   construction -- the state in which the noweb mechanism was measured unproven
   (``web_search`` still on the wire) never arises. The custom provider authenticates
   via ``env_key = "OPENAI_API_KEY"``: the key reaches codex by process-env
   inheritance only, and preflight refuses the run loudly when it is unset
   (``required_env``).
2. **Isolated CODEX_HOME per invocation.** ``write_driver_config`` creates
   ``codex-home/`` under the invocation directory with a harness-written
   ``config.toml`` holding ONLY the MCP registration; ``build_turn`` points codex at
   it via the ``CODEX_HOME`` env override (executed material, recorded in meta).
   Ambient user config -- which was measured leaking MCP servers and plugin tools,
   one of which (``node_repl``) started mid-probe -- is never consulted. This is the
   codex analogue of the neutral working directory, load-bearing for attribution.
3. **Web disable via the noweb custom provider, never the config knob.**
   ``tools.web_search=false`` was measured dead under both auth modes; it is still
   passed, as a belt, with that caveat recorded. The mechanism is the provider:
   web-tool registration follows the provider's ``supports_standalone_web_search``
   declaration, builtins declare it immutably, custom providers declare nothing.
   ``wire_api = "responses"`` (0.147.0 refuses ``"chat"``).
4. **The custom provider IS the harness recorder.** The provider's ``base_url`` is
   the invocation's ApiSurfaceRecorder URL (plus ``/v1``), which forwards to the real
   backend -- one interposition point yields both the web-disable and the per-scored-
   invocation wire verification. The recorder's rules already match the contract:
   tool names only, never headers (Authorization carries the real key), never message
   content.
5. **Per-invocation effect verification.** ``disallowed_builtins`` names the hosted
   web tools for the wire check, and ``web_event_markers`` carries codex's own event
   vocabulary (the spaced ``"web search"`` form is the literal 0.147.0 event-line
   prefix; the ancestor once scored a run web-silent by omitting it). Either signal
   is an instrument breach: the runner marks the row not tool-attributable and BREAKS
   the cell.
6. **Key injection for spawned MCP servers.** Codex sanitizes the env of MCP servers
   it spawns, so ``sanitizes_mcp_env = True``: the runner hands the harness proxy a
   transient 0600 secrets file (outside the run tree; process-env only) instead of
   relying on inheritance. Only the file's path appears in the config artifact.
7. **Native vocabulary.** ``reasoning_effort`` is passed verbatim as codex's own
   ``model_reasoning_effort``; every knob is recorded verbatim in meta; no mapping to
   any other vendor's scale exists here or anywhere.

The load-bearing configuration travels as ``-c`` argv overrides so the attribution
record can be asserted from the executed command (never from intent); the isolation
that cannot live on argv (CODEX_HOME) is asserted from the turn's env overrides,
which the runner applies and records. Sessions are unsupported (``codex exec`` has no
resume idiom the harness trusts), so crowded cells are refused at preflight.

Q2-settling evidence, measured 2026-09-01 under this harness's instruments (codex-cli
0.147.0, real key, runs/2026-09-01-codex-driver-proof and -codex-secret-canary; the
ruling itself is the spec session's):

* Wire capture across a real cell: codex-family models ("code mode") send NO ``tools``
  array; the roster travels in ``client_metadata["x-codex-turn-metadata"].
  code_mode_tool_names`` (found by full-body probe after the tools-array capture came
  back empty on a working turn), which the recorder now reads. The captured surface
  held the MCP tools the consumer called and no web tool; zero web events; answer
  grounded in the traced call. Caveat for the scorer: code-mode metadata is the
  client's declaration of the backend-injected harness -- wire-observed and not
  model-mediated, but it describes the served toolset rather than constituting it the
  way a ``tools`` array does.
* Model dependence: gpt-5.2 DOES use the ``tools`` array and carries a hosted
  ``web_search`` (``external_web_access: false``) that no config knob removes; the
  breach check BREAKS such cells. Model choice is load-bearing.
* Secrets canary: a SUT that refuses to start without its ``$secret`` served
  normally under codex (which sanitizes spawned-server env), the value appeared in no
  artifact, and the 0600 file was deleted at invocation end.
"""
from __future__ import annotations

from pathlib import Path

from .base import Driver, DriverAttributionError, TurnContext, TurnSpec

PROVIDER_ID = "harness_noweb"
ENV_KEY = "OPENAI_API_KEY"


def toml_quote(value: str) -> str:
    """Quote a string as a TOML basic string, for config.toml and `-c key=value`.

    Codex parses the value of ``-c key=value`` as TOML and falls back to a literal
    only when that parse fails, so an unquoted path or URL is ambiguous; every string
    is quoted with backslashes and quotes escaped. The argv is passed as a list, so
    there is no shell layer, only TOML's.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class CodexDriver(Driver):
    id = "codex"
    executable = "codex"
    # The hosted web tools as they appear in a Responses-API tools array (type-named,
    # no "name" field -- the recorder records name-or-type). Any of these on the wire
    # is a breach, per requirement 5.
    disallowed_builtins = ("web_search", "web_search_preview")
    # Codex's own event vocabulary for web-tool activity in its output streams.
    # "web search" (with the space) is the literal event-line prefix codex 0.147.0
    # prints; never remove the spaced form.
    web_event_markers = ("web.run", "web_search", "web-search", "web search", "web__run")
    probe_model = ""  # capture-capable; the calibration probe is not this driver's path
    api_base_env = "OPENAI_BASE_URL"
    api_default_upstream = "https://api.openai.com"
    sanitizes_mcp_env = True
    required_env = (ENV_KEY,)
    supports_sessions = False

    def write_driver_config(self, dest: Path, server_entries: dict[str, dict]) -> Path:
        """Create the isolated CODEX_HOME with a config.toml holding the MCP surface.

        The provider and policy configuration deliberately do NOT live here: they
        travel as ``-c`` overrides on the argv so the attribution record is asserted
        from the executed command. This file is the harness-authored replacement for
        the ambient user config (requirement 2) and carries the MCP registration --
        including ``default_tools_approval_mode = "approve"`` per server, without
        which ``approval_policy = "never"`` cancels every MCP call client-side (the
        ancestor's probe G; its absence is the F29 dead-cell shape).
        """
        home = dest / "codex-home"
        home.mkdir(parents=True, exist_ok=True)
        lines = ["# Harness-authored isolated CODEX_HOME config (50-drivers.md, codex #2).",
                 "# MCP registration only; policy and provider travel as asserted -c overrides."]
        for name, entry in server_entries.items():
            lines += [
                "",
                f"[mcp_servers.{name}]",
                f"command = {toml_quote(entry['command'])}",
                "args = [" + ", ".join(toml_quote(a) for a in entry.get("args", [])) + "]",
                'default_tools_approval_mode = "approve"',
            ]
            env = entry.get("env") or {}
            if env:
                lines += ["", f"[mcp_servers.{name}.env]"]
                lines += [f"{key} = {toml_quote(str(value))}" for key, value in env.items()]
        path = home / "config.toml"
        path.write_text("\n".join(lines) + "\n")
        return path

    def build_turn(self, ctx: TurnContext) -> TurnSpec:
        mode, _ = ctx.session
        if mode != "single":
            raise DriverAttributionError(
                "the codex driver does not support sessions, so it cannot host crowded "
                "cells; a crowding pre-turn that cannot verifiably resume would mislabel "
                "a fresh cell as crowded.")
        base_url = (ctx.api_base_url or "http://dry-run.invalid").rstrip("/") + "/v1"
        answer_path = ctx.dest / "agent-last-message.txt"
        argv = [
            self.executable, "exec", "-m", ctx.model,
            # The neutral cwd is deliberately not a git repository.
            "--skip-git-repo-check",
            # Persist no session state between prompts.
            "--ephemeral",
            # Read-only sandbox: the shell can neither write nor reach the network,
            # and with approval_policy="never" there is no path to escalate out --
            # every escalation request is denied. MCP still flows because the
            # harness-authored config pre-approves the registered servers' tools
            # (default_tools_approval_mode="approve"). Never --approve-for-me: its
            # automatic reviewer approved the model's own sandbox-escape requests
            # (ancestor probe E7, observed reaching the network).
            "-s", "read-only",
            "-c", 'approval_policy="never"',
            # Belt only: measured DEAD under both auth modes (handoff O31/O33 -- the
            # web tool stayed live and was used, including sandbox-evasion attempts).
            # The provider mechanism below is the actual web-disable.
            "-c", "tools.web_search=false",
            # The noweb custom provider IS the harness recorder (requirements 3+4):
            # custom providers declare no supports_standalone_web_search, so the CLI
            # registers no web tool; base_url routes every request through the
            # recorder, which forwards to the real backend.
            "-c", f"model_providers.{PROVIDER_ID}.name="
                  + toml_quote("Harness recording provider (no standalone web tool)"),
            "-c", f"model_providers.{PROVIDER_ID}.base_url={toml_quote(base_url)}",
            "-c", f"model_providers.{PROVIDER_ID}.env_key={toml_quote(ENV_KEY)}",
            "-c", f'model_providers.{PROVIDER_ID}.wire_api="responses"',
            "-c", f"model_provider={toml_quote(PROVIDER_ID)}",
            "-o", str(answer_path),
        ]
        # Native vocabulary (requirement 7): reasoning_effort verbatim in codex's own
        # key; all other knobs are recorded in meta, not translated.
        effort = ctx.knobs.get("reasoning_effort")
        if effort:
            argv += ["-c", f"model_reasoning_effort={toml_quote(str(effort))}"]
        argv.append("-")  # prompt from stdin, never argv
        return TurnSpec(
            argv=argv, stdin_text=ctx.prompt, answer_from="file", answer_path=answer_path,
            # The isolated CODEX_HOME (requirement 2): executed material, applied by
            # the runner and recorded in meta. ctx.mcp_config_path is
            # .../codex-home/config.toml as written by write_driver_config.
            env_overrides={"CODEX_HOME": str(ctx.mcp_config_path.parent)},
        )

    def attribution_record(self, turn: TurnSpec) -> dict:
        argv = turn.argv

        def missing(what: str) -> DriverAttributionError:
            return DriverAttributionError(
                f"the codex turn lacks {what}. The attribution record must be asserted from "
                "the executed command and environment, and this turn does not close the "
                "channel it claims to. Harness bug; cell refused as instrument-broken.")

        try:
            sandbox = argv[argv.index("-s") + 1]
        except (ValueError, IndexError):
            raise missing("-s <sandbox_mode>") from None
        if sandbox != "read-only":
            raise missing(f"-s read-only (got {sandbox!r})")
        if 'approval_policy="never"' not in argv:
            raise missing('-c approval_policy="never"')
        if "--approve-for-me" in argv:
            raise missing("no --approve-for-me: its automatic reviewer approves the model's "
                          "own sandbox-escape requests (ancestor probe E7)")
        if any(a.startswith("--dangerously") for a in argv):
            raise missing("no --dangerously-* flag: that reopens the network wholesale")
        if f"model_provider={toml_quote(PROVIDER_ID)}" not in argv:
            raise missing(f'-c model_provider="{PROVIDER_ID}" (the builtin providers register '
                          "the CLI's own web tool and no tools.* config removes it)")
        base_url = next((a.split("=", 1)[1] for a in argv
                         if a.startswith(f"model_providers.{PROVIDER_ID}.base_url=")), None)
        if not base_url:
            raise missing(f"-c model_providers.{PROVIDER_ID}.base_url=<harness recorder>")
        if f'model_providers.{PROVIDER_ID}.wire_api="responses"' not in argv:
            raise missing(f'-c model_providers.{PROVIDER_ID}.wire_api="responses"')
        if f"model_providers.{PROVIDER_ID}.env_key={toml_quote(ENV_KEY)}" not in argv:
            raise missing(f"-c model_providers.{PROVIDER_ID}.env_key (API-key credentials only)")
        if "tools.web_search=false" not in argv:
            raise missing("-c tools.web_search=false (belt; the provider is the mechanism)")
        codex_home = turn.env_overrides.get("CODEX_HOME")
        if not codex_home:
            raise missing("a CODEX_HOME env override (the isolated, harness-authored home is "
                          "load-bearing for attribution: ambient config leaked MCP servers and "
                          "plugin tools, one of which ran mid-probe)")
        return {
            "sandbox_mode": "read-only",
            "approvals": 'never (escalations denied; registered MCP tools pre-approved '
                         'per-server via default_tools_approval_mode="approve")',
            "model_provider": f"{PROVIDER_ID} (custom Responses-API provider; registers no "
                              "standalone web tool; base_url is the harness recorder)",
            "provider_base_url": base_url,
            "wire_api": "responses",
            "env_key": f"{ENV_KEY} (value by process env only; never an artifact)",
            "tools.web_search": "false (belt; measured dead under both auth modes -- effect "
                                "rests on the provider mechanism and is verified per "
                                "invocation on the wire and in the event stream)",
            "codex_home": f"{codex_home} (harness-authored, fresh per invocation; no ChatGPT "
                          "login state exists in it, so credentials are API-key only)",
        }
