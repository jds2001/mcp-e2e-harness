"""The ``openrouter`` deployment of the loop driver: routing policy, catalog, cost model.

Everything here is harness configuration that a manifest never carries (20-manifest.md,
loop cells): the deployment's base URL and credential variable, the strict-routing
block every loop request sends, and the free catalog reads the runner uses for
pre-run cost legibility (50-drivers.md, loop requirement 6) and pin resolution
(requirement 4). A second deployment is a further ``Deployment`` here; nothing in the
driver or the consumer names a host.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import __version__


@dataclass(frozen=True)
class Deployment:
    name: str
    upstream: str          # scheme + host, no path
    api_prefix: str        # path prefix the OpenAI-compatible routes hang off
    key_env: str           # harness env var carrying the credential
    upstream_env: str      # harness env var overriding the upstream (a gateway; tests)


OPENROUTER = Deployment(
    name="openrouter",
    upstream="https://openrouter.ai",
    api_prefix="/api/v1",
    key_env="OPENROUTER_API_KEY",
    upstream_env="MCP_E2E_OPENROUTER_UPSTREAM",
)

DEPLOYMENTS: dict[str, Deployment] = {OPENROUTER.name: OPENROUTER}

# Request keys the harness constructs; a knob naming one would silently fight the
# scaffold, so the manifest validator refuses it.
RESERVED_REQUEST_KEYS = ("model", "messages", "tools", "provider", "usage", "stream")

DATA_POLICIES = ("deny", "allow")

_PIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9.-]*)?$")


def parse_pin(tag: str) -> tuple[str, str | None]:
    """``deepinfra/bf16`` -> (``deepinfra``, ``bf16``); ``openai`` -> (``openai``, None).

    The tag is an endpoint tag verbatim from the deployment's endpoint listing: the
    slug is the provider, the optional suffix is the quantization (or a variant the
    listing names, such as ``turbo``, which is then sent as-is).
    """
    if not isinstance(tag, str) or not _PIN_RE.match(tag):
        raise ValueError(f"provider pin {tag!r} is not an endpoint tag of the form slug or slug/quant")
    slug, _, quant = tag.partition("/")
    return slug, (quant or None)


def provider_block(pin: str | None, data_policy: str) -> dict:
    """The ``provider`` object every loop request carries (requirements 4 and 7).

    ``require_parameters`` is set on every request, pinned or not (hardened 2026-09-18
    from the P-knob-drop observation: without it the router silently dropped an
    unsupported knob; with it, it refused loudly). ``data_collection: deny`` is the
    default disclosure preference; ``allow`` is the recorded opt-out. A pinned cell
    names its provider as the sole allowed one with fallbacks off, and its
    quantization when the pin tag carries one.
    """
    if data_policy not in DATA_POLICIES:
        raise ValueError(f"data_policy must be one of {DATA_POLICIES}, got {data_policy!r}")
    block: dict[str, Any] = {"require_parameters": True, "data_collection": data_policy}
    if pin:
        slug, quant = parse_pin(pin)
        block["order"] = [slug]
        block["allow_fallbacks"] = False
        if quant:
            block["quantizations"] = [quant]
    return block


def normalize_provider(name: str | None) -> str:
    """Lowercase alphanumerics only: the response's display name ("Google AI Studio")
    against a listing slug ("google-ai-studio") when no listing lookup is available."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def served_matches_pin(served: str | None, pin: str, names_for_slug: list[str] | None) -> bool:
    """Whether a response's ``provider`` string is the pinned provider.

    The response reports the provider's display name only (measured 2026-09-18:
    ``DeepInfra`` for the ``deepinfra/bf16`` endpoint), so the comparison is against
    the display names the endpoint listing maps to the pin's slug when the runner
    could read the listing, and against the normalized slug otherwise. A missing
    provider string is never a match: an unrecordable identity is a mismatch, not a
    pass (Q8 P-provider-pin).
    """
    if not served:
        return False
    slug, _ = parse_pin(pin)
    if names_for_slug:
        return served in names_for_slug
    return normalize_provider(served) == normalize_provider(slug)


# ------------------------------------------------------------------- catalog

class CatalogError(RuntimeError):
    """A free catalog read failed; the caller degrades to 'unavailable', never guesses."""


class Catalog:
    """Free (keyless) reads of the deployment's model and endpoint listings.

    These are harness-side GETs made by the runner before any model turn -- not
    consumer traffic -- so they do not pass through the recorder. Each listing is
    fetched at most once per instance.
    """

    def __init__(self, deployment: Deployment, upstream: str, timeout_s: float = 20.0):
        self.deployment = deployment
        self.base = upstream.rstrip("/") + deployment.api_prefix
        self.timeout_s = timeout_s
        self._models: list[dict] | None = None
        self._endpoints: dict[str, list[dict]] = {}
        self._providers: list[dict] | None = None

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(
            self.base + path,
            headers={"Accept": "application/json",
                     "User-Agent": f"mcp-e2e-harness/{__version__} (catalog read)"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise CatalogError(f"GET {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CatalogError(f"GET {path}: {exc}") from None

    def models(self) -> list[dict]:
        if self._models is None:
            payload = self._get("/models")
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise CatalogError("GET /models: no 'data' list in the response")
            self._models = data
        return self._models

    def model_pricing(self, model: str) -> dict[str, float] | None:
        """List price per token ({"prompt": ..., "completion": ...}); None when unlisted."""
        for entry in self.models():
            if entry.get("id") == model:
                pricing = entry.get("pricing") or {}
                try:
                    return {"prompt": float(pricing.get("prompt")),
                            "completion": float(pricing.get("completion"))}
                except (TypeError, ValueError):
                    return None
        return None

    def endpoints(self, model: str) -> list[dict]:
        if model not in self._endpoints:
            payload = self._get(f"/models/{model}/endpoints")
            data = payload.get("data") if isinstance(payload, dict) else None
            endpoints = data.get("endpoints") if isinstance(data, dict) else None
            if not isinstance(endpoints, list):
                raise CatalogError(f"GET /models/{model}/endpoints: no endpoints list in the response")
            self._endpoints[model] = endpoints
        return self._endpoints[model]

    def providers(self) -> list[dict]:
        if self._providers is None:
            payload = self._get("/providers")
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise CatalogError("GET /providers: no 'data' list in the response")
            self._providers = data
        return self._providers

    def provider_names_for_slug(self, model: str, slug: str) -> list[str]:
        """Display names the listing reports for endpoints whose tag slug is ``slug``."""
        names: list[str] = []
        for endpoint in self.endpoints(model):
            tag = str(endpoint.get("tag") or "")
            if tag.split("/")[0] == slug:
                name = endpoint.get("provider_name")
                if isinstance(name, str) and name not in names:
                    names.append(name)
        return names

    def endpoint_for_pin(self, model: str, pin: str) -> dict | None:
        for endpoint in self.endpoints(model):
            if endpoint.get("tag") == pin:
                return endpoint
        return None

    def provider_record(self, slug_or_name: str) -> dict | None:
        """The ``/providers`` entry for a slug or display name; what the deployment
        publishes about a provider (policy URLs, headquarters, datacenters)."""
        for entry in self.providers():
            if entry.get("slug") == slug_or_name or entry.get("name") == slug_or_name:
                return entry
        return None

    def slug_for_provider_name(self, model: str, name: str) -> str | None:
        for endpoint in self.endpoints(model):
            if endpoint.get("provider_name") == name:
                return str(endpoint.get("tag") or "").split("/")[0] or None
        return None


# ---------------------------------------------------------------- cost model

# 50-drivers.md (loop, "Cost model, grounded in real request sizes"): summed request
# bytes per invocation from uscde-mcp C1 under claude-code, and the one measured
# tokens-per-byte ratio (37,193 tokens for a 105,242-byte body; other tokenizers
# differ, call it +/-30%). Output is the answer plus tool-call arguments plus
# reasoning; the reasoning term is unbounded and the estimate says so.
EST_INPUT_BYTES = {"crowded": 586_489, "fresh": 128_544}
EST_BYTES_PER_TOKEN = 105_242 / 37_193
EST_OUTPUT_TOKENS = 5_000


def estimate_invocation_usd(context: str, pricing: dict[str, float] | None) -> dict:
    """A labeled estimate for one invocation; ``usd`` is None when the price is unknown."""
    input_tokens = round(EST_INPUT_BYTES.get(context, EST_INPUT_BYTES["fresh"]) / EST_BYTES_PER_TOKEN)
    record = {
        "context": context,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": EST_OUTPUT_TOKENS,
        "basis": ("byte-based token estimate from measured C1 request sizes at "
                  f"{EST_BYTES_PER_TOKEN:.2f} bytes/token (+/-30% across tokenizers); output at "
                  "minimum reasoning effort. Reasoning tokens are the unbounded term and are NOT "
                  "bounded by this estimate."),
        "usd": None,
    }
    if pricing:
        record["usd"] = round(input_tokens * pricing["prompt"] + EST_OUTPUT_TOKENS * pricing["completion"], 6)
    return record
