"""Harness-owned crowding procedures (ruling S6, documentation/10-harness.md).

A ``crowded`` cell's prompt lands while the consumer is genuinely mid-way through a
coherent, mundane, domain-neutral task with other tools registered. The content is
harness-owned, versioned, and task-shaped -- never suite-authored, never trivia
padding -- because it sits in the consumer's context during the scored turn: a suite's
spec session, which knows the server's internals, would be writing part of the
instrument it is scored against. Suites select a procedure by versioned name and attest
it is disjoint from their server's domain (``crowding.collision_review`` in the
manifest); on collision they select a different procedure, never write their own.

The procedure's name, version, and content hash are recorded in the cell's meta so a
scored run pins exactly what crowded it. Changing any content below is a new version,
not an edit: bump ``version`` so old runs stay attributable to the bytes they ran with.

The distractor tools are served by ``mcp_e2e_harness.distractor``, a minimal stdio MCP
server the harness registers beside the server under test.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Note:
    id: str
    title: str
    body: str


@dataclass(frozen=True)
class CrowdingProcedure:
    name: str
    version: int
    # The MCP server name the distractor registers under, as the consumer sees it.
    server_name: str
    folders: tuple[str, ...]
    notes: tuple[Note, ...] = field(repr=False)
    # The pre-turn that puts the consumer mid-task before the scored prompt lands.
    opening_prompt: str = field(repr=False)

    # Instrument validation, separate from the consumer-visible content hash.
    expected_filed: tuple[str, ...] = ("n01", "n02", "n03", "n04")

    def check_state(self, state: dict) -> dict:
        if not isinstance(state, dict) or not isinstance(state.get("filed"), dict):
            raise ValueError("state must contain a filed object")
        expected, observed = sorted(self.expected_filed), sorted(state["filed"])
        return {"expected": expected, "observed": observed,
                "passed": observed == expected}

    @property
    def full_name(self) -> str:
        return f"{self.name}@{self.version}"

    def content_hash(self) -> str:
        payload = {
            "name": self.name,
            "version": self.version,
            "server_name": self.server_name,
            "folders": list(self.folders),
            "notes": [{"id": n.id, "title": n.title, "body": n.body} for n in self.notes],
            "opening_prompt": self.opening_prompt,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


_TRIAGE_NOTES = (
    Note("n01", "Printer toner",
         "The 3rd-floor printer is showing 'toner low' again. Spare cartridges are in the supply "
         "cupboard; if there are none left we need to add them to the next stationery order."),
    Note("n02", "Friday farewell cake",
         "Collection envelope for Priya's farewell is with reception until Thursday lunchtime. "
         "Cake to be ordered from the bakery on Elm Street, pickup Friday 15:30."),
    Note("n03", "Fire drill",
         "Building management has scheduled the quarterly fire drill for the 14th at 10:00. "
         "Wardens should wear the hi-vis vests; assembly point is the far corner of car park B."),
    Note("n04", "Window cleaning",
         "Window cleaners will be on the east side Tuesday and Wednesday next week. Please keep "
         "blinds raised and windowsills clear of plants and mugs."),
    Note("n05", "Parking permit renewals",
         "Annual parking permits expire at the end of the month. Renewal forms go to facilities "
         "by the 25th; late renewals lose the reserved bays for two weeks."),
    Note("n06", "Plant watering rota",
         "The watering rota for the lobby plants has a gap in week 3. Volunteers should add their "
         "name to the sheet by the kitchen; the fern needs water twice a week."),
    Note("n07", "Meeting room clock",
         "The wall clock in the small meeting room is 12 minutes slow -- battery, probably. "
         "Spare AAs are in the top drawer of the stationery cabinet."),
    Note("n08", "Recycling bins",
         "New three-stream recycling bins arrive Monday. The old blue bins will be collected; "
         "flatten any cardboard and leave it beside the goods lift, not in front of it."),
    Note("n09", "Lost umbrella",
         "A black umbrella with a wooden handle was left in reception last Thursday. It's behind "
         "the front desk; unclaimed items go to the charity box at month end."),
    Note("n10", "Desk moves",
         "The seating plan for the pod-C desk moves is pinned by the kitchen. Crates are booked "
         "for the last Friday of the month; laptops and chairs move with their owners."),
    Note("n11", "Kitchen dishwasher",
         "The kitchen dishwasher's rinse-aid light has been on for a week. Refill bottle is under "
         "the sink; run the empty machine on hot once after refilling."),
    Note("n12", "First-aid kit audit",
         "Quarterly first-aid kit audit is due. Check the wall kits on each floor against the "
         "contents card and note anything missing on the facilities form."),
)

# Version 2 (2026-08-31) supersedes v1, whose opening prompt asked the consumer to
# keep filing "until they're done": measured on the first live crowded run
# (claude-sonnet-5 pre-turn), the pre-turn completed the whole task ("All 12 notes
# filed ... Inbox is now empty" in distractor-state.json), so the scored prompt landed
# after the competing goal was gone -- not the "genuinely mid-way through" state ruling
# S6 requires. v2 bounds the pre-turn to the first four notes, so at the scored turn
# two-thirds of the task verifiably remains. The spec's ledger (40-instruments.md)
# still pins v1; adopting v2 there is the spec session's ruling to make.
NEUTRAL_FILE_TRIAGE_V2 = CrowdingProcedure(
    name="neutral-file-triage",
    version=2,
    server_name="shared_notes",
    folders=("logistics", "facilities", "social", "archive"),
    notes=_TRIAGE_NOTES,
    opening_prompt=(
        "You're helping tidy the office's shared notes inbox before the desk moves. Use the "
        "shared_notes tools: list the unfiled notes, then work through them one at a time -- read "
        "each note and file it into whichever folder fits best (logistics, facilities, social, or "
        "archive). There are quite a few and we'll be interleaving this with other things, so for "
        "now file just the first four on the list and then stop; we'll keep going in a bit, and "
        "the rest MUST wait until you get the go-ahead."
    ),
)

# Version 1 stays registered VERBATIM: the spec's instrument ledger
# (documentation/40-instruments.md) pins neutral-file-triage@1 by content hash, and
# retiring or editing it here would break that pin's verifiability -- re-pinning is the
# spec session's call, not this file's. Do not select it for new suites: the pre-turn
# defect above is why v2 exists. tests/test_crowding.py asserts this object still
# hashes to the pinned value.
NEUTRAL_FILE_TRIAGE_V1 = CrowdingProcedure(
    name="neutral-file-triage",
    version=1,
    server_name="shared_notes",
    folders=("logistics", "facilities", "social", "archive"),
    notes=_TRIAGE_NOTES,
    opening_prompt=(
        "You're helping tidy the office's shared notes inbox before the desk moves. Use the "
        "shared_notes tools: list the unfiled notes, then work through them one at a time -- read "
        "each note and file it into whichever folder fits best (logistics, facilities, social, or "
        "archive). There are quite a few, so don't stop to summarize; just keep filing until "
        "they're done or you're asked about something else."
    ),
)

PROCEDURES: dict[str, CrowdingProcedure] = {
    NEUTRAL_FILE_TRIAGE_V1.full_name: NEUTRAL_FILE_TRIAGE_V1,
    NEUTRAL_FILE_TRIAGE_V2.full_name: NEUTRAL_FILE_TRIAGE_V2,
}


def get_procedure(full_name: str) -> CrowdingProcedure | None:
    return PROCEDURES.get(full_name)
