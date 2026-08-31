"""Secret handling and the built-in secret-hygiene scan.

Two rules from the spec, neither waivable by a suite:

* Any value in the manifest's ``env``/``headers`` maps given as ``{"$secret": "VAR"}``
  is resolved from the harness's own environment at launch and is never written to any
  artifact (documentation/20-manifest.md, server registration).
* No configured secret material -- resolved ``$secret`` values, plus the harness-env
  values of variables the suite names in ``server.secret_keys`` -- appears in any trace
  record, transcript, or meta artifact (documentation/10-harness.md, Layer 1 built-ins).

Values shorter than MIN_SECRET_LEN are excluded from the scan set: a 3-character
"secret" would match half of any transcript and turn the scan into noise. That floor is
disclosed here rather than silently applied.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

MIN_SECRET_LEN = 8


class MissingSecretError(RuntimeError):
    """A ``$secret`` placeholder names an env var that is not set. Names the VAR, never a value."""


class SecretLeakError(RuntimeError):
    """Configured secret material reached an artifact. The run must halt; do not publish it."""


def is_secret_ref(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == {"$secret"}


def resolve_env_map(env: Mapping[str, object], environ: Mapping[str, str] | None = None,
                    *, where: str = "env") -> tuple[dict[str, str], list[str]]:
    """Resolve an env/headers map to plain strings; return (resolved, secret values used).

    ``{"$secret": "VAR"}`` values come from ``environ`` (default: the harness's own
    environment). A missing VAR raises loudly here rather than surfacing later as a
    server that came up keyless wearing a misleading error.
    """
    environ = os.environ if environ is None else environ
    resolved: dict[str, str] = {}
    secrets: list[str] = []
    for key, value in env.items():
        if is_secret_ref(value):
            var = value["$secret"]  # type: ignore[index]
            actual = environ.get(var, "")
            if not actual:
                raise MissingSecretError(
                    f"{where}.{key} references {{'$secret': {var!r}}} but {var} is not set in the "
                    "harness environment. Export it in the shell that runs the harness."
                )
            resolved[key] = actual
            secrets.append(actual)
        else:
            resolved[key] = str(value)
    return resolved, secrets


def collect_scan_set(manifest_data: Mapping[str, object],
                     environ: Mapping[str, str] | None = None) -> list[str]:
    """Every secret value that could reach an artifact, for the hygiene scan.

    Sources: each ``$secret`` placeholder anywhere in the server registration or a
    cell's ``env``, resolved; plus the harness-env values of every name in
    ``server.secret_keys`` (the suite names which keys are sensitive; the harness
    cannot guess). Missing ``$secret`` vars are skipped here -- resolution has its own
    loud failure path; the scan set only needs values that exist.
    """
    environ = os.environ if environ is None else environ
    values: list[str] = []

    def _from_map(env: object) -> None:
        if not isinstance(env, Mapping):
            return
        for value in env.values():
            if is_secret_ref(value):
                actual = environ.get(value["$secret"], "")  # type: ignore[index]
                if actual:
                    values.append(actual)

    server = manifest_data.get("server") or {}
    transport = server.get("transport") or {} if isinstance(server, Mapping) else {}
    _from_map(transport.get("env") if isinstance(transport, Mapping) else None)
    _from_map(transport.get("headers") if isinstance(transport, Mapping) else None)
    if isinstance(server, Mapping):
        for name in server.get("secret_keys") or []:
            actual = environ.get(str(name), "")
            if actual:
                values.append(actual)
    cells = manifest_data.get("cells") or {}
    if isinstance(cells, Mapping):
        for cell in cells.values():
            if isinstance(cell, Mapping):
                _from_map(cell.get("env"))

    seen: dict[str, None] = {}
    for v in values:
        if len(v) >= MIN_SECRET_LEN:
            seen.setdefault(v, None)
    return list(seen)


def assert_clean(text: str, secrets: list[str], where: str) -> None:
    """Halt-the-run scan: no configured secret material in ``text``.

    Never echoes the secret; the error names only where it was found.
    """
    for secret in secrets:
        if secret and secret in text:
            raise SecretLeakError(
                f"configured secret material found in {where}. Run halted; do not publish this "
                "directory. Secrets reach the server by env inheritance or $secret resolution "
                "at launch, never through an artifact."
            )
