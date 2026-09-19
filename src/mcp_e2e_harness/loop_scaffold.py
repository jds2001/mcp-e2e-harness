"""The loop driver's scaffold: instrument content, versioned and content-hashed.

50-drivers.md (loop, requirement 1) gives the scaffold S6 treatment: the loop's system
prompt, tool-loop policy, step cap, and calibration-probe tool definition are
harness-owned, versioned, and pinned by content hash in ``40-instruments.md``. The
scaffold version joins cell identity -- two cells differing only in scaffold version
are different cells and never pool -- so nothing here is edited in place: a content
change is a new version, registered beside the old one so old runs stay attributable
to the bytes they ran with (the same rule ``crowding.py`` follows).

The system prompt is short, generic, and domain-neutral, and it must not mention
testing, harnesses, or evaluation: a consumer that learns it is being measured is a
different consumer (10-harness.md, no-disclosure).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class LoopScaffold:
    name: str
    version: int
    # The first message of every conversation the loop runs.
    system_prompt: str
    # Model requests per turn (the crowding pre-turn and the scored turn each get a
    # fresh budget). A runaway tool loop is bounded here; the probe uses one request.
    step_cap: int
    # The mechanical loop policy, stated so it hashes with the rest of the scaffold.
    tool_loop_policy: str
    # The calibration probe (requirement 9): the only tool offered on a probe request.
    probe_tool: dict
    probe_prompt: str

    @property
    def full_name(self) -> str:
        return f"{self.name}@{self.version}"

    def content_hash(self) -> str:
        payload = {
            "name": self.name,
            "version": self.version,
            "system_prompt": self.system_prompt,
            "step_cap": self.step_cap,
            "tool_loop_policy": self.tool_loop_policy,
            "probe_tool": self.probe_tool,
            "probe_prompt": self.probe_prompt,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @property
    def probe_tool_name(self) -> str:
        return self.probe_tool["function"]["name"]


LOOP_SCAFFOLD_V1 = LoopScaffold(
    name="loop-scaffold",
    version=1,
    system_prompt=(
        "You are a helpful assistant with access to tools. When a tool can supply "
        "information you need, call it instead of relying on memory. Base your answer on "
        "what the tools return, cite or quote retrieved material where that matters, and "
        "say plainly when something could not be determined with the tools available."
    ),
    step_cap=24,
    tool_loop_policy=(
        "One model request per step; the scaffold's system prompt is the first message of "
        "every conversation, and a crowding pre-turn and the scored turn that follows it "
        "share one conversation. Every tool call a response carries is executed against the "
        "MCP server in the order given, and each result is appended as a tool message whose "
        "content is the result's text items joined by newlines (a result with no text is "
        "its JSON serialization; an error result is sent as its text). Arguments that do "
        "not parse as a JSON object are answered with an error message instead of a call. "
        "No tool_choice is sent: tool selection is the model's. The turn ends when a "
        "response carries no tool call (its text is the answer) or when the step cap is "
        "reached, in which case the turn ends without an answer and says so."
    ),
    probe_tool={
        "type": "function",
        "function": {
            "name": "get_probe_value",
            "description": "Return the value stored under a key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "The key to look up."}},
                "required": ["key"],
                "additionalProperties": False,
            },
        },
    },
    probe_prompt="Look up the value stored under the key 'alpha' and tell me what it is.",
)

SCAFFOLDS: dict[str, LoopScaffold] = {
    LOOP_SCAFFOLD_V1.full_name: LOOP_SCAFFOLD_V1,
}


def get_scaffold(full_name: str) -> LoopScaffold | None:
    return SCAFFOLDS.get(full_name)


def validate_probe_arguments(scaffold: LoopScaffold, arguments: object) -> list[str]:
    """Problems with a probe call's parsed arguments against the probe tool's schema.

    The probe schema is a flat object of typed properties, so a small validator is
    exact for it; an empty list means schema-valid.
    """
    schema = scaffold.probe_tool["function"]["parameters"]
    problems: list[str] = []
    if not isinstance(arguments, dict):
        return [f"arguments are {type(arguments).__name__}, not an object"]
    properties = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in arguments:
            problems.append(f"required property {key!r} missing")
    if schema.get("additionalProperties") is False:
        problems += [f"unexpected property {k!r}" for k in arguments if k not in properties]
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
             "object": dict, "array": list}
    for key, spec in properties.items():
        if key in arguments and spec.get("type") in types:
            expected = types[spec["type"]]
            value = arguments[key]
            if isinstance(value, bool) and spec["type"] != "boolean":
                problems.append(f"property {key!r} is a boolean, expected {spec['type']}")
            elif not isinstance(value, expected):
                problems.append(f"property {key!r} is {type(value).__name__}, expected {spec['type']}")
    return problems
