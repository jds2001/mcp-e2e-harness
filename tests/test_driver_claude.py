"""The claude-code driver: argv construction and the asserted attribution record."""
from __future__ import annotations

import json

import pytest

from mcp_e2e_harness.drivers.base import DriverAttributionError, TurnContext
from mcp_e2e_harness.drivers.claude_code import DISALLOWED_BUILTINS, ClaudeCodeDriver


def ctx(tmp_path, **overrides) -> TurnContext:
    defaults = dict(model="claude-x", knobs={"thinking": "none"},
                    mcp_config_path=tmp_path / "mcp-config.json",
                    server_name="sut", tool_surface="full", prompt="hello",
                    dest=tmp_path)
    defaults.update(overrides)
    return TurnContext(**defaults)


def test_argv_closes_the_untraced_channels(tmp_path):
    driver = ClaudeCodeDriver()
    turn = driver.build_turn(ctx(tmp_path))
    argv = turn.argv
    assert argv[:2] == ["claude", "-p"]
    assert "--strict-mcp-config" in argv
    # Surface removal, not just permission denial (Q2 probe finding, 2026-08-31).
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowed-tools") + 1] == "mcp__sut"
    disallowed = argv[argv.index("--disallowed-tools") + 1]
    assert disallowed == ",".join(DISALLOWED_BUILTINS)
    # The prompt travels on stdin, never argv.
    assert "hello" not in argv
    assert turn.stdin_text == "hello"
    assert turn.answer_from == "stdout"


def test_list_valued_surface_narrows_allowed_tools(tmp_path):
    driver = ClaudeCodeDriver()
    turn = driver.build_turn(ctx(tmp_path, tool_surface=["a", "b"]))
    allowed = turn.argv[turn.argv.index("--allowed-tools") + 1]
    assert allowed == "mcp__sut__a,mcp__sut__b"


def test_extra_servers_are_allowed_for_crowded_cells(tmp_path):
    driver = ClaudeCodeDriver()
    turn = driver.build_turn(ctx(tmp_path, extra_server_names=("shared_notes",)))
    allowed = turn.argv[turn.argv.index("--allowed-tools") + 1]
    assert allowed == "mcp__sut,mcp__shared_notes"


def test_session_flags(tmp_path):
    driver = ClaudeCodeDriver()
    opened = driver.build_turn(ctx(tmp_path, session=("open", "sid-1"))).argv
    assert opened[opened.index("--session-id") + 1] == "sid-1"
    resumed = driver.build_turn(ctx(tmp_path, session=("resume", "sid-1"))).argv
    assert resumed[resumed.index("--resume") + 1] == "sid-1"
    single = driver.build_turn(ctx(tmp_path)).argv
    assert "--session-id" not in single and "--resume" not in single


def test_attribution_record_is_asserted_from_argv(tmp_path):
    driver = ClaudeCodeDriver()
    turn = driver.build_turn(ctx(tmp_path))
    record = driver.attribution_record(turn.argv)
    assert record["strict_mcp_config"] is True
    assert "WebSearch" in record["disallowed_tools"]

    with pytest.raises(DriverAttributionError, match="strict-mcp-config"):
        driver.attribution_record([a for a in turn.argv if a != "--strict-mcp-config"])

    open_web = list(turn.argv)
    i = open_web.index("--disallowed-tools")
    open_web[i + 1] = "Bash,Read"
    with pytest.raises(DriverAttributionError, match="WebSearch"):
        driver.attribution_record(open_web)

    nonempty_builtins = list(turn.argv)
    nonempty_builtins[nonempty_builtins.index("--tools") + 1] = "default"
    with pytest.raises(DriverAttributionError, match="empty built-in set"):
        driver.attribution_record(nonempty_builtins)


def test_write_driver_config(tmp_path):
    driver = ClaudeCodeDriver()
    path = driver.write_driver_config(tmp_path, {"sut": {"command": "x", "args": []}})
    assert path == tmp_path / "mcp-config.json"
    assert json.loads(path.read_text()) == {"mcpServers": {"sut": {"command": "x", "args": []}}}


def test_cli_version_never_raises():
    version = ClaudeCodeDriver().cli_version()
    assert isinstance(version, str) and version
