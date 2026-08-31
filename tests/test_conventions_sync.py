"""AGENTS.md and .claude/CLAUDE.md must stay byte-identical (CONTRIBUTING.md)."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_agents_md_matches_claude_md():
    agents = (REPO / "AGENTS.md").read_bytes()
    claude = (REPO / ".claude" / "CLAUDE.md").read_bytes()
    assert agents == claude, (
        "AGENTS.md and .claude/CLAUDE.md have drifted; edit both together "
        "(see CONTRIBUTING.md, 'Keeping AGENTS.md and .claude/CLAUDE.md in sync')")
