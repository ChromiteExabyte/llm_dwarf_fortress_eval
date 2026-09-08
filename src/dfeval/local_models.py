"""Native Ollama decisions and bounded discovery of existing local models.

No operation starts a server, selects a model, downloads weights, or retries.
The supplied server is trusted to report its identity and telemetry truthfully.
Loopback transport alone is not proof that a server performs inference locally;
obvious Ollama cloud tags are rejected by the local policy.
"""

from __future__ import annotations

import copy
import ipaddress
import math
import os
import re
import threading
import time
from typing import Any
from urllib import error, parse, request

from .model_observation import MODEL_OBSERVATION_VERSION, model_input_bytes

from .policies import (ChatCompletionsPolicy, DecisionError, PolicyError,
                       SYSTEM_PROMPT, _NoRedirect, json_bytes, parse_decision, strict_json)


_DEFAULTS = {"ollama": "http://127.0.0.1:11434", "lmstudio": "http://127.0.0.1:1234",
             "llamacpp": "http://127.0.0.1:8080"}
_DISCOVERY_BYTES = 1024 * 1024
_MAX_MODELS = 256
_MAX_NATIVE_INTEGER = 2**63 - 1


def _seconds(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
        raise ValueError("timeout must be finite, positive and at most 3600 seconds")
    return float(value)


def _base_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError("base_url must be a complete loopback HTTP(S) URL")
    try:
        parts = parse.urlsplit(value)
        port = parts.port
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError()
        try:
            local = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            local = parts.hostname.lower() == "localhost"
        if not local or (port is not None and not 1 <= port <= 65535):
            raise ValueError()
        if "\\" in parts.path or "%" in parts.path or any(p in (".", "..") for p in parts.path.split("/")):
            raise ValueError()
    except ValueError:
        raise ValueError("base_url must use a loopback host without credentials, query, fragment or path traversal") from None
    return parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _endpoint(base_url: str, namespace: str, route: str) -> str:
    suffix = "/" + namespace
    return base_url + ("" if base_url.endswith(suffix) else suffix) + "/" + route


def _cloud_tag(name: str) -> bool:
    return re.search(r"(?::|-)cloud(?=$|[:/-])", name, re.IGNORECASE) is not None


def _text(value: Any, limit: int = 256) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value) <= limit and all(ord(c) >= 32 for c in value) else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= _MAX_NATIVE_INTEGER else None


def _digest(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", value):
        return value.removeprefix("sha256:").lower()
    return None


def _identity(value: Any, model: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"name", "digest", "parameter_size", "quantization_level"}:
        raise ValueError("model_identity accepts only name, digest, parameter_size and quantization_level")
    result = {}
    for key, item in value.items():
        cleaned = _digest(item) if key == "digest" else _text(item, 256 if key == "name" else 64)
        if item is not None and cleaned is None:
            raise ValueError(f"model_identity.{key} is invalid")
        result[key] = cleaned
    if result.get("name") is not None and result["name"] != model:
        raise ValueError("model_identity.name must match the explicitly selected model")
    return result


def _read_response(opener: Any, req: request.Request, *, timeout: float, limit: int, label: str) -> bytes:
    """Bound the entire read even when a server trickles bytes indefinitely.

    A timed-out daemon can finish its existing request later; its result is
    discarded. There are no follow-up calls or state mutations in the worker.
    """
    done = threading.Event()
    received: list[bytes] = []
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            with opener.open(req, timeout=timeout) as response:
                received.append(response.read(limit + 1))
        except BaseException as exc:
            failures.append(exc)
        finally:
            done.set()

    deadline = time.monotonic() + timeout
    threading.Thread(target=invoke, daemon=True, name="dfeval-local-model-http").start()
    if not done.wait(max(0, deadline - time.monotonic())) or time.monotonic() > deadline:
        raise PolicyError(f"{label} exceeded its wall-time allowance; no retry was made. Check server readiness and timeout.")
    if failures:
        exc = failures[0]
        if isinstance(exc, error.HTTPError):
            raise PolicyError(f"{label} returned HTTP {exc.code}; check the endpoint, installed model and access settings. Error body omitted.") from None
        if isinstance(exc, PolicyError):
            raise PolicyError(f"{label} redirects are disabled; use the final loopback URL.") from None
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise exc
        raise PolicyError(f"{label} request failed ({type(exc).__name__}); check that the selected server is running at the configured URL. No retry was made.") from None
    return received[0]


def _telemetry(envelope: dict[str, Any]) -> dict[str, Any]:
    result = {key: _count(envelope.get(key)) for key in (
        "eval_count", "prompt_eval_count", "prompt_eval_cached_count", "eval_duration",
        "prompt_eval_duration", "load_duration", "total_duration")}
    for target, source in (("generation_seconds", "eval_duration"), ("prompt_seconds", "prompt_eval_duration"),
                           ("load_seconds", "load_duration"), ("total_seconds", "total_duration")):
        result[target] = result[source] / 1_000_000_000 if result[source] is not None else None
    result["completion_tokens"] = result["eval_count"]
    result["prompt_tokens"] = result["prompt_eval_count"]
    elapsed = result["generation_seconds"]
    result["generation_tokens_per_second"] = result["eval_count"] / elapsed if elapsed and result["eval_count"] is not None else None
    result["source"] = "ollama_native"
    result["native_duration_unit"] = "nanoseconds"
    return result


class OllamaPolicy(ChatCompletionsPolicy):
    """One explicitly selected native Ollama model, with the shared host schema."""

    def __init__(self, *, model: str, base_url: str = _DEFAULTS["ollama"], api_key_env: str | None = None,
                 max_completion_tokens: int = 512, timeout: float = 30,
                 max_response_bytes: int = 65536, max_request_bytes: int = 2_000_000,
                 num_ctx: int = 16384, temperature: float = 0, seed: int = 0,
                 keep_alive: str | int = "10m", think: bool = False,
                 model_identity: dict[str, Any] | None = None, opener: Any = None,
                 system_prompt: str = SYSTEM_PROMPT):
        base = _base_url(base_url)
        if isinstance(model, str) and _cloud_tag(model):
            raise ValueError("Ollama cloud-tag models are not local inference; select a downloaded local model")
        if type(num_ctx) is not int or not 128 <= num_ctx <= 1_048_576:
            raise ValueError("num_ctx must be an integer between 128 and 1048576")
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("temperature must be finite and between 0 and 2")
        if type(seed) is not int or not 0 <= seed <= 2**31 - 1:
            raise ValueError("seed must be an integer between 0 and 2147483647")
        if not ((type(keep_alive) is int and keep_alive == 0) or
                (isinstance(keep_alive, str) and len(keep_alive) <= 32 and re.fullmatch(r"(?:0|[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h))", keep_alive))):
            raise ValueError("keep_alive must be 0 or a nonnegative duration such as '10m'")
        if type(think) is not bool:
            raise ValueError("think must be a boolean")
        super().__init__(mode="local", model=model, endpoint=_endpoint(base, "v1", "chat/completions"),
                         api_key_env=api_key_env, max_completion_tokens=max_completion_tokens, timeout=timeout,
                         max_response_bytes=max_response_bytes, max_request_bytes=max_request_bytes,
                         response_format="json_schema", opener=opener, system_prompt=system_prompt)
        self.base_url, self.endpoint = base, _endpoint(base, "api", "chat")
        self.num_ctx, self.temperature, self.seed = num_ctx, temperature, seed
        self.keep_alive, self.think = keep_alive, think
        self.model_identity = _identity(model_identity, self.model)

    def public_config(self) -> dict[str, Any]:
        return {"kind": "ollama", "is_model": True, "mode": "local", "model": self.model,
                "model_observation_version": MODEL_OBSERVATION_VERSION,
                "base_url": self.base_url, "endpoint": self.endpoint, "api_key_env": self.api_key_env,
                "max_completion_tokens": self.max_completion_tokens, "token_limit_field": "options.num_predict",
                "timeout": self.timeout, "max_response_bytes": self.max_response_bytes,
                "max_request_bytes": self.max_request_bytes, "response_format": self.response_format,
                "system_prompt": self.system_prompt, "options": {"num_ctx": self.num_ctx, "temperature": self.temperature,
                                                           "seed": self.seed, "num_predict": self.max_completion_tokens},
                "keep_alive": self.keep_alive, "think": self.think, "model_identity": copy.deepcopy(self.model_identity)}

    def choose(self, observation: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
        self.last_exchange = None
        payload = {"model": self.model, "messages": [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": model_input_bytes(observation, history).decode("utf-8")},
        ], "format": self.response_format["json_schema"]["schema"], "stream": False,
            "options": {"num_predict": self._call_tokens, "num_ctx": self.num_ctx,
                        "temperature": self.temperature, "seed": self.seed},
            "keep_alive": self.keep_alive, "think": self.think}
        encoded = json_bytes(payload)
        self.last_exchange = {"request": payload, "response_text": None, "response_bytes": 0,
                              "response_truncated": False, "usage": None, "telemetry": None,
                              "reserved_output_tokens": self._call_tokens}
        if len(encoded) > self.max_request_bytes:
            raise PolicyError("Ollama request exceeds max_request_bytes; input was not sent")
        secret = os.environ.get(self.api_key_env) if self.api_key_env else None
        if self.api_key_env and not secret:
            raise PolicyError("Configured API key environment variable is empty or missing")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if secret:
            headers["Authorization"] = "Bearer " + secret
        req = request.Request(self.endpoint, data=encoded, headers=headers, method="POST")
        raw = _read_response(self._opener, req, timeout=self._call_timeout, limit=self._call_bytes, label="Ollama")
        too_long = len(raw) > self._call_bytes
        recorded = raw[:self._call_bytes].decode("utf-8", errors="replace")
        if secret:
            recorded = recorded.replace(secret, "[REDACTED_API_KEY]")
        self.last_exchange.update(response_bytes=min(len(raw), self._call_bytes), response_truncated=too_long)
        if too_long:
            self.last_exchange["response_text"] = recorded
            raise PolicyError("Ollama response exceeds byte limit; only the bounded prefix was recorded")
        try:
            envelope = strict_json(raw.decode("utf-8"))
        except (UnicodeError, DecisionError):
            self.last_exchange["response_text"] = recorded
            raise PolicyError("Ollama did not return one valid JSON response") from None
        if not isinstance(envelope, dict):
            raise PolicyError("Ollama response must be an object")
        if "error" in envelope:
            raise PolicyError("Ollama reported an API error; error body omitted. Check the model name and server configuration.")
        self.last_exchange["response_text"] = recorded
        telemetry = _telemetry(envelope)
        self.last_exchange["telemetry"] = telemetry
        prompt, completion = telemetry["prompt_tokens"], telemetry["completion_tokens"]
        self.last_exchange["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion,
                                       "total_tokens": prompt + completion if prompt is not None and completion is not None else None}
        if completion is not None and completion > self._call_tokens:
            raise PolicyError("Ollama reported output usage above the requested limit")
        if envelope.get("done") is not True or envelope.get("done_reason") != "stop":
            raise PolicyError("Ollama completion did not finish normally; no action accepted")
        message = envelope.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call") or message.get("refusal") or message.get("images"):
            raise PolicyError("Expected an Ollama assistant text response without tools, images or refusal")
        content = message.get("content")
        if secret and isinstance(content, str) and secret in content:
            raise PolicyError("Response echoed a credential; public transcript redacted and decision rejected")
        return parse_decision(content)


def discover_models(provider: str = "ollama", base_url: str | None = None, timeout: float = 3, *, opener: Any = None) -> dict[str, Any]:
    """Read one local model inventory, without loading or choosing a model.

    At most 1 MiB is read and 256 rows are returned. Larger row inventories are
    explicitly marked truncated. Missing or invalid identities fail discovery;
    optional details are projected onto bounded public fields.
    """
    if provider not in (*_DEFAULTS, "compatible"):
        raise ValueError("provider must be ollama, lmstudio, llamacpp or compatible")
    if base_url is None and provider == "compatible":
        raise ValueError("compatible discovery requires an explicit loopback base_url")
    base = _base_url(base_url if base_url is not None else _DEFAULTS[provider])
    timeout = _seconds(timeout)
    endpoint = _endpoint(base, "api", "tags") if provider == "ollama" else _endpoint(base, "v1", "models")
    client = opener or request.build_opener(request.ProxyHandler({}), _NoRedirect())
    req = request.Request(endpoint, headers={"Accept": "application/json"}, method="GET")
    started = time.monotonic()
    raw = _read_response(client, req, timeout=timeout, limit=_DISCOVERY_BYTES, label=f"{provider} model discovery")
    if len(raw) > _DISCOVERY_BYTES:
        raise PolicyError("Local model inventory exceeds the 1 MiB inspection limit")
    try:
        envelope = strict_json(raw.decode("utf-8"))
    except (UnicodeError, DecisionError):
        raise PolicyError("Local model server did not return a valid model inventory") from None
    if not isinstance(envelope, dict) or "error" in envelope:
        raise PolicyError("Local model server reported an invalid inventory or API error; body omitted")
    rows = envelope.get("models" if provider == "ollama" else "data")
    if not isinstance(rows, list):
        raise PolicyError("Local model inventory is missing its model list")
    models = []
    seen = set()
    for row in rows[:_MAX_MODELS]:
        name = _text(row.get("name") if provider == "ollama" else row.get("id")) if isinstance(row, dict) else None
        if name is None or name in seen:
            raise PolicyError("Local model inventory contains a missing, invalid or duplicate model name")
        seen.add(name)
        record: dict[str, Any] = {"id": name, "name": name}
        if provider == "ollama":
            raw_details = row.get("details") if isinstance(row.get("details"), dict) else {}
            details = {key: _text(raw_details.get(key), 64) for key in ("format", "family", "parameter_size", "quantization_level")}
            families = raw_details.get("families")
            details["families"] = [item for item in families[:16] if _text(item, 64)] if isinstance(families, list) else None
            record.update(digest=_digest(row.get("digest")), details=details, size_bytes=_count(row.get("size")),
                          modified_at=_text(row.get("modified_at"), 128), cloud_backed=_cloud_tag(name),
                          parameter_size=details["parameter_size"], quantization_level=details["quantization_level"])
        models.append(record)
    return {"kind": "local_model_inventory", "provider": provider, "base_url": base, "endpoint": endpoint,
            "models": models, "model_count": len(models), "reported_model_count": len(rows),
            "truncated": len(rows) > _MAX_MODELS, "wall_seconds": round(time.monotonic() - started, 6),
            "notes": ["Server-reported inventory only; no model was loaded, selected or downloaded.",
                      "A loopback endpoint does not prove local inference. Ollama cloud tags are marked and rejected by the local policy."]}
