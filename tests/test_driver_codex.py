"""The codex driver against its contract (documentation/50-drivers.md, reqs 1-7)."""
from __future__ import annotations

import pytest

from mcp_e2e_harness.drivers.base import DriverAttributionError, TurnContext, TurnSpec
from mcp_e2e_harness.drivers.codex import ENV_KEY, PROVIDER_ID, CodexDriver, toml_quote


def ctx(tmp_path, **overrides) -> TurnContext:
    defaults = dict(model="gpt-5.6-luna", knobs={"reasoning_effort": "medium"},
                    mcp_config_path=tmp_path / "codex-home" / "config.toml",
                    server_name="sut", tool_surface="full", prompt="hello",
                    dest=tmp_path, api_base_url="http://127.0.0.1:5555")
    defaults.update(overrides)
    return TurnContext(**defaults)


def test_toml_quote_escapes_backslashes_and_quotes():
    assert toml_quote('a"b\\c') == '"a\\"b\\\\c"'


def test_contract_attributes():
    driver = CodexDriver()
    assert driver.sanitizes_mcp_env is True              # req 6
    assert driver.required_env == (ENV_KEY,)             # req 1
    assert driver.supports_api_capture                   # req 4
    assert driver.supports_sessions is False
    assert "web_search" in driver.disallowed_builtins    # req 5
    assert "web search" in driver.web_event_markers      # the spaced literal form


def test_argv_closes_the_channels_and_routes_through_the_recorder(tmp_path):
    driver = CodexDriver()
    turn = driver.build_turn(ctx(tmp_path))
    argv = turn.argv
    assert argv[:4] == ["codex", "exec", "-m", "gpt-5.6-luna"]
    assert argv[argv.index("-s") + 1] == "read-only"
    assert 'approval_policy="never"' in argv
    assert "tools.web_search=false" in argv
    assert "features.plugins=false" in argv  # measured effective; suppresses plugin-sync fetch
    # The noweb provider IS the recorder (reqs 3+4).
    assert f'model_provider={toml_quote(PROVIDER_ID)}' in argv
    assert f'model_providers.{PROVIDER_ID}.base_url="http://127.0.0.1:5555/v1"' in argv
    assert f'model_providers.{PROVIDER_ID}.wire_api="responses"' in argv
    assert f'model_providers.{PROVIDER_ID}.env_key={toml_quote(ENV_KEY)}' in argv
    # Native vocabulary, verbatim (req 7).
    assert f'model_reasoning_effort={toml_quote("medium")}' in argv
    # The prompt travels on stdin; the answer by file.
    assert argv[-1] == "-"
    assert "hello" not in argv
    assert turn.answer_from == "file"
    assert turn.answer_path == tmp_path / "agent-last-message.txt"
    # Isolated CODEX_HOME by env override (req 2).
    assert turn.env_overrides == {"CODEX_HOME": str(tmp_path / "codex-home")}


def test_no_reasoning_flag_when_knob_absent(tmp_path):
    turn = CodexDriver().build_turn(ctx(tmp_path, knobs={}))
    assert not any("model_reasoning_effort" in a for a in turn.argv)


def test_sessions_are_refused(tmp_path):
    with pytest.raises(DriverAttributionError, match="session"):
        CodexDriver().build_turn(ctx(tmp_path, session=("open", "sid-1")))
    with pytest.raises(DriverAttributionError, match="session"):
        CodexDriver().build_turn(ctx(tmp_path, session=("resume", "sid-1")))


def test_config_toml_is_mcp_registration_only(tmp_path):
    driver = CodexDriver()
    path = driver.write_driver_config(tmp_path, {
        "sut": {"command": "/usr/bin/python3",
                "args": ["-m", "mcp_e2e_harness.proxy", "--server-config", 'we"ird path']},
    })
    assert path == tmp_path / "codex-home" / "config.toml"
    text = path.read_text()
    assert "[mcp_servers.sut]" in text
    assert 'command = "/usr/bin/python3"' in text
    assert '"we\\"ird path"' in text
    # Without per-server pre-approval, approval_policy="never" cancels every MCP call
    # client-side (ancestor probe G / the F29 dead-cell shape).
    assert 'default_tools_approval_mode = "approve"' in text
    # Policy and provider are asserted from argv, never smuggled here.
    assert "model_provider" not in text
    assert "approval_policy" not in text


def test_attribution_record_verifies_the_executed_turn(tmp_path):
    driver = CodexDriver()
    turn = driver.build_turn(ctx(tmp_path))
    record = driver.attribution_record(turn)
    assert record["sandbox_mode"] == "read-only"
    assert record["provider_base_url"] == '"http://127.0.0.1:5555/v1"'
    assert "harness recorder" in record["model_provider"]
    assert "API-key only" in record["codex_home"]
    assert "measured effective" in record["features.plugins"]

    def tampered(argv=None, env=None) -> TurnSpec:
        return TurnSpec(argv=list(argv if argv is not None else turn.argv), stdin_text="",
                        env_overrides=dict(turn.env_overrides if env is None else env))

    with pytest.raises(DriverAttributionError, match="model_provider"):
        driver.attribution_record(tampered(
            [a for a in turn.argv if a != f"model_provider={toml_quote(PROVIDER_ID)}"]))
    with pytest.raises(DriverAttributionError, match="base_url"):
        driver.attribution_record(tampered(
            [a for a in turn.argv if not a.startswith(f"model_providers.{PROVIDER_ID}.base_url=")]))
    with pytest.raises(DriverAttributionError, match="approve-for-me"):
        driver.attribution_record(tampered(turn.argv + ["--approve-for-me"]))
    with pytest.raises(DriverAttributionError, match="dangerously"):
        driver.attribution_record(tampered(turn.argv + ["--dangerously-bypass-approvals"]))
    with pytest.raises(DriverAttributionError, match="read-only"):
        wrong_sandbox = list(turn.argv)
        wrong_sandbox[wrong_sandbox.index("-s") + 1] = "workspace-write"
        driver.attribution_record(tampered(wrong_sandbox))
    with pytest.raises(DriverAttributionError, match="web_search"):
        driver.attribution_record(tampered(
            [a for a in turn.argv if a != "tools.web_search=false"]))
    with pytest.raises(DriverAttributionError, match="features.plugins"):
        driver.attribution_record(tampered(
            [a for a in turn.argv if a != "features.plugins=false"]))
    with pytest.raises(DriverAttributionError, match="CODEX_HOME"):
        driver.attribution_record(tampered(env={}))


def test_environment_state_reports_no_fetch_when_absent(tmp_path):
    # No CODEX_HOME/.tmp/plugins.sha at all -- the common case, and must not be
    # conflated with "sync ran but produced no SHA".
    driver = CodexDriver()
    turn = driver.build_turn(ctx(tmp_path))
    state = driver.environment_state(turn)
    assert state["plugin_sync"]["fetched_sha"] is None


def test_environment_state_lifts_the_fetched_sha(tmp_path):
    # 50-drivers.md Residual 2: codex's own plugin-sync fetch into the isolated home,
    # measured live as a shallow clone of github.com/openai/plugins with a FETCH_HEAD.
    driver = CodexDriver()
    turn = driver.build_turn(ctx(tmp_path))
    sha_path = tmp_path / "codex-home" / ".tmp" / "plugins.sha"
    sha_path.parent.mkdir(parents=True)
    sha_path.write_text("1e285826e604f66f7208f7ac4dba0fe8341d1f57\n")
    state = driver.environment_state(turn)
    assert state["plugin_sync"]["fetched_sha"] == "1e285826e604f66f7208f7ac4dba0fe8341d1f57"


def test_environment_state_none_without_codex_home(tmp_path):
    turn = TurnSpec(argv=["codex"], stdin_text="", env_overrides={})
    assert CodexDriver().environment_state(turn) is None
