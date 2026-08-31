"""Generic e2e harness for MCP consumers.

Drives a real consumer model against an MCP server under test, through a real driver,
and records what happened. The harness executes and records; it never scores
(documentation/10-harness.md). Everything server-specific arrives in a suite manifest
(documentation/20-manifest.md); Layer-1 trace checks are declarative data
(documentation/30-checks.md).
"""

__version__ = "0.1.0"
