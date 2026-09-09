"""Stateless, bounded text inference for independently constructed contexts.

The operator fixes the provider and model. Callers supply only role/content text
messages, a completion allowance, and an optional temperature. No care briefing,
action schema, history, tools, retries, or fallback model is inserted here.
Provider counts describe reported usage; missing counts are never estimated.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import math
import os
import re
import threading
import time
from typing import Any
from urllib import parse, request

from .local_models import _base_url, _cloud_tag, _count, _read_response, _telemetry
from .policies import DecisionError, PolicyError, _NoRedirect, _integer, _seconds, json_bytes, strict_json


MAX_MESSAGES = 4096
MAX_REQUEST_BYTES = 16_000_000
MAX_RESPONSE_BYTES = 16_000_000
MAX_OUTPUT_TOKENS = 131072
_ROLES = ("system", "user", "assistant")


def _temperature(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2:
        raise ValueError("temperature must be null or a finite number between 0 and 2")
    return float(value)


@dataclass(frozen=True, init=False)
class TextRequest:
    """An immutable snapshot; returned message dictionaries are fresh copies."""

    _messages: tuple[tuple[str, str], ...]
    max_output_tokens: int
    temperature: float | None

    def __init__(self, messages: list[dict[str, str]], max_output_tokens: int,
                 temperature: float | None = None):
        if type(messages) is not list:
            raise ValueError(f"messages must contain between 1 and {MAX_MESSAGES} text messages")
        messages = list.copy(messages)
        if not 1 <= len(messages) <= MAX_MESSAGES:
            raise ValueError(f"messages must contain between 1 and {MAX_MESSAGES} text messages")
        captured = []
        size = 0
        for message in messages:
            if type(message) is not dict:
                raise ValueError("Each message must be a plain role/content dictionary")
            message = dict.copy(message)
            if (any(type(key) is not str for key in message) or set(message) != {"role", "content"}
                    or type(message["role"]) is not str or message["role"] not in _ROLES
                    or type(message["content"]) is not str):
                raise ValueError("Each message must contain only role and content text; roles are system, user, assistant")
            try:
                size += len(message["content"].encode("utf-8"))
            except UnicodeError:
                raise ValueError("Message content must contain valid Unicode") from None
            if size > MAX_REQUEST_BYTES:
                raise ValueError("Message content exceeds the maximum UTF-8 request size")
            captured.append((message["role"], message["content"]))
        object.__setattr__(self, "_messages", tuple(captured))
        object.__setattr__(self, "max_output_tokens", _integer(max_output_tokens, "max_output_tokens", 1, MAX_OUTPUT_TOKENS))
        object.__setattr__(self, "temperature", _temperature(temperature))

    @property
    def messages(self) -> list[dict[str, str]]:
        return [{"role": role, "content": content} for role, content in self._messages]

    def to_dict(self) -> dict[str, Any]:
        return {"messages": self.messages, "max_output_tokens": self.max_output_tokens,
                "temperature": self.temperature}


class TextInferenceError(RuntimeError):
    """Failure with its own bounded evidence, safe to retain across concurrent calls.

    ``provider_may_be_running`` means a transport attempt did not yield a complete
    response. A supervisor can stop admission rather than accumulate replacement
    calls while a timed-out HTTP worker may still be finishing.
    """

    def __init__(self, message: str, *, exchange: dict[str, Any]):
        super().__init__(message)
        self.exchange = copy.deepcopy(exchange)
        self.provider_may_be_running = exchange.get("provider_may_be_running") is True


def _endpoint(provider: str, mode: str, endpoint: str | None) -> str:
    if endpoint is None:
        if mode != "local":
            raise ValueError("Cloud inference requires an explicit HTTPS endpoint")
        endpoint = "http://127.0.0.1:11434/" + ("api/chat" if provider == "ollama" else "v1/chat/completions")
    if (not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048
            or any(ord(char) < 33 or ord(char) == 127 for char in endpoint)):
        raise ValueError("endpoint must be a bounded complete HTTP(S) URL")
    try:
        endpoint.encode("utf-8")
        parts = parse.urlsplit(endpoint)
        port = parts.port
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or (port is not None and not 1 <= port <= 65535)
                or "\\" in endpoint or "%" in parts.path
                or any(piece in (".", "..") for piece in parts.path.split("/"))):
            raise ValueError()
        suffix = "/api/chat" if provider == "ollama" else "/chat/completions"
        if not parts.path.rstrip("/").endswith(suffix):
            raise ValueError()
        if mode == "cloud" and parts.scheme != "https":
            raise ValueError()
        if mode == "local":
            _base_url(endpoint)  # Shared literal loopback-only validation.
    except (ValueError, UnicodeError):
        raise ValueError("endpoint must use the selected provider route, HTTPS for cloud or loopback for local, without credentials, query or traversal") from None
    return endpoint


def _unknown_usage() -> dict[str, Any]:
    return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
            "cached_prompt_tokens": None, "total_tokens_source": None}


def _usage_and_timing(provider: str, body: dict[str, Any], wall: float) -> tuple[dict[str, Any], dict[str, Any]]:
    usage = _unknown_usage()
    timing = {"wall_seconds": wall, "generation_seconds": None, "prompt_seconds": None,
              "load_seconds": None, "total_seconds": None, "source": "provider_reported"}
    if provider == "ollama":
        native = _telemetry(body)
        prompt, completion = native["prompt_tokens"], native["completion_tokens"]
        usage.update(prompt_tokens=prompt, completion_tokens=completion,
                     cached_prompt_tokens=native["prompt_eval_cached_count"],
                     total_tokens=prompt + completion if prompt is not None and completion is not None else None,
                     total_tokens_source="sum_of_reported_counts" if prompt is not None and completion is not None else None)
        timing.update({key: native[key] for key in ("generation_seconds", "prompt_seconds", "load_seconds", "total_seconds")})
        timing["native"] = native
    else:
        measured = body.get("usage")
        measured = measured if isinstance(measured, dict) else {}
        details = measured.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        usage.update({key: _count(measured.get(key)) for key in ("prompt_tokens", "completion_tokens", "total_tokens")})
        usage["cached_prompt_tokens"] = _count(details.get("cached_tokens"))
        if usage["total_tokens"] is not None:
            usage["total_tokens_source"] = "provider_reported"
        native = body.get("timings")
        native = native if isinstance(native, dict) else {}
        for target, field in (("generation_seconds", "predicted_ms"), ("prompt_seconds", "prompt_ms")):
            value = native.get(field)
            if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 3_600_000:
                timing[target] = value / 1000
    # Cache counts describe part of the prompt; impossible values stay unknown.
    if (usage["cached_prompt_tokens"] is not None and usage["prompt_tokens"] is not None
            and usage["cached_prompt_tokens"] > usage["prompt_tokens"]):
        usage["cached_prompt_tokens"] = None
    return usage, timing


def _prefix(raw: bytes, limit: int) -> str:
    """Even replacement characters must fit the recorded UTF-8 byte allowance."""
    return raw[:limit].decode("utf-8", errors="replace").encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _contains_secret(value: Any, secret: str | None) -> bool:
    if not secret:
        return False
    pending = [value]
    visited: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, str) and str.__contains__(item, secret):
            return True
        if isinstance(item, (dict, list)):
            if id(item) in visited:
                continue
            visited.add(id(item))
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


class TextClient:
    """Fixed operator configuration with no mutable per-call transcript state.

    ``opener`` is a trusted dependency injection seam for tests, never an agent
    request field. A requested timeout can only shorten the operator's timeout.
    """

    def __init__(self, *, provider: str, model: str, mode: str = "local", endpoint: str | None = None,
                 api_key_env: str | None = None, timeout: float = 30,
                 max_request_bytes: int = 2_000_000, max_response_bytes: int = 2_000_000,
                 max_output_tokens: int = 32768, num_ctx: int | None = None,
                 token_limit_field: str | None = None, opener: Any = None):
        if provider not in ("ollama", "chat_completions") or mode not in ("local", "cloud"):
            raise ValueError("Select provider ollama or chat_completions and mode local or cloud")
        if (not isinstance(model, str) or not model.strip() or len(model) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in model)):
            raise ValueError("A fixed nonempty model ID of at most 256 characters is required")
        try:
            model.encode("utf-8")
        except UnicodeError:
            raise ValueError("Model ID must contain valid Unicode") from None
        if provider == "ollama" and (mode != "local" or _cloud_tag(model)):
            raise ValueError("Native Ollama requires a local model; cloud-tag models are not local inference")
        if api_key_env is not None and (not isinstance(api_key_env, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env)):
            raise ValueError("api_key_env must name an environment variable")
        if mode == "cloud" and api_key_env is None:
            raise ValueError("Cloud inference requires an API key environment variable name")
        if num_ctx is not None:
            if provider != "ollama":
                raise ValueError("num_ctx is supported only by native Ollama")
            _integer(num_ctx, "num_ctx", 128, 1_048_576)
        if provider == "ollama":
            if token_limit_field is not None:
                raise ValueError("Native Ollama uses options.num_predict; token_limit_field is compatible-only")
        else:
            if token_limit_field is None:
                token_limit_field = "max_tokens" if mode == "local" else "max_completion_tokens"
            if token_limit_field not in ("max_tokens", "max_completion_tokens"):
                raise ValueError("token_limit_field must be max_tokens or max_completion_tokens")
        self._config = {"kind": "stateless_text_inference", "provider": provider, "model": model,
                        "mode": mode, "endpoint": _endpoint(provider, mode, endpoint), "api_key_env": api_key_env,
                        "timeout": _seconds(timeout, "timeout"),
                        "max_request_bytes": _integer(max_request_bytes, "max_request_bytes", 256, MAX_REQUEST_BYTES),
                        "max_response_bytes": _integer(max_response_bytes, "max_response_bytes", 256, MAX_RESPONSE_BYTES),
                        "max_output_tokens": _integer(max_output_tokens, "max_output_tokens", 1, MAX_OUTPUT_TOKENS),
                        "num_ctx": num_ctx, "token_limit_field": token_limit_field}
        self._opener = opener if opener is not None else request.build_opener(request.ProxyHandler({}), _NoRedirect())
        self._admission_lock = threading.Lock()
        self._admission_closed = False

    @property
    def provider(self):
        return self._config["provider"]

    @property
    def model(self):
        return self._config["model"]

    @property
    def mode(self):
        return self._config["mode"]

    @property
    def endpoint(self):
        return self._config["endpoint"]

    @property
    def timeout(self):
        return self._config["timeout"]

    @property
    def max_request_bytes(self):
        return self._config["max_request_bytes"]

    @property
    def max_response_bytes(self):
        return self._config["max_response_bytes"]

    @property
    def max_output_tokens(self):
        return self._config["max_output_tokens"]

    def public_config(self) -> dict[str, Any]:
        return copy.deepcopy(self._config)

    def check_public_input(self, value: Any) -> None:
        """Reject a credential before a supervisor persists public JSON input.

        This performs no transport or mutation. It checks the credential value,
        not the public environment variable name; it is not a general privacy
        scrub. The caller still validates and bounds its own JSON fields.
        """
        secret = os.environ.get(self._config["api_key_env"]) if self._config["api_key_env"] else None
        if _contains_secret(value, secret):
            raise ValueError("Public input contains a configured credential; do not record or submit it")

    def infer(self, text_request: TextRequest, *, timeout: float | None = None) -> dict[str, Any]:
        if type(text_request) is not TextRequest:
            raise ValueError("infer requires a validated TextRequest")
        if text_request.max_output_tokens > self.max_output_tokens:
            raise ValueError("Requested output tokens exceed the operator's per-call limit")
        allowance = self.timeout if timeout is None else min(self.timeout, _seconds(timeout, "timeout"))
        payload = {"model": self.model, "messages": text_request.messages, "stream": False}
        if self.provider == "ollama":
            options = {"num_predict": text_request.max_output_tokens}
            if self._config["num_ctx"] is not None:
                options["num_ctx"] = self._config["num_ctx"]
            if text_request.temperature is not None:
                options["temperature"] = text_request.temperature
            payload["options"] = options
        else:
            payload[self._config["token_limit_field"]] = text_request.max_output_tokens
            if text_request.temperature is not None:
                payload["temperature"] = text_request.temperature
        encoded = json_bytes(payload)
        exchange = {"request": payload if len(encoded) <= self.max_request_bytes else None,
                    "request_text": _prefix(encoded, self.max_request_bytes), "request_bytes": len(encoded),
                    "request_sha256": hashlib.sha256(encoded).hexdigest(),
                    "request_truncated": len(encoded) > self.max_request_bytes,
                    "request_sent": False, "response_text": None, "response_bytes": 0,
                    "response_sha256": None, "response_truncated": False, "response_complete": False,
                    "provider_may_be_running": False, "reserved_output_tokens": text_request.max_output_tokens,
                    "usage": _unknown_usage(), "timing": {"wall_seconds": 0.0}, "finish_reason": None}
        secret = os.environ.get(self._config["api_key_env"]) if self._config["api_key_env"] else None
        if _contains_secret(payload, secret):
            exchange.update(request=None, request_text=None, request_omitted_reason="credential_in_request")
            raise TextInferenceError("Request contained a configured credential; input was not sent", exchange=exchange)
        if len(encoded) > self.max_request_bytes:
            raise TextInferenceError("Text request exceeds max_request_bytes; input was not sent", exchange=exchange)
        if self._config["api_key_env"] and not secret:
            raise TextInferenceError("Configured API key environment variable is empty or missing", exchange=exchange)
        if secret and (any(ord(char) < 32 or ord(char) > 126 for char in secret)):
            raise TextInferenceError("Configured API key is not valid header text", exchange=exchange)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if secret:
            headers["Authorization"] = "Bearer " + secret
        req = request.Request(self.endpoint, data=encoded, headers=headers, method="POST")
        started = time.monotonic()
        with self._admission_lock:
            if self._admission_closed:
                exchange["client_admission_closed"] = True
                raise TextInferenceError("Client admission is closed after an uncertain prior transport call", exchange=exchange)
        exchange.update(request_sent=True, provider_may_be_running=True)
        try:
            raw = _read_response(self._opener, req, timeout=allowance, limit=self.max_response_bytes, label="Text inference")
        except PolicyError as exc:
            with self._admission_lock:
                self._admission_closed = True
            exchange["timing"]["wall_seconds"] = max(0.0, time.monotonic() - started)
            raise TextInferenceError(str(exc), exchange=exchange) from None
        wall = max(0.0, time.monotonic() - started)
        too_long = len(raw) > self.max_response_bytes
        retained = raw[:self.max_response_bytes]
        exchange.update(response_bytes=len(retained), response_sha256=hashlib.sha256(retained).hexdigest(),
                        response_truncated=too_long, response_complete=not too_long,
                        provider_may_be_running=too_long, timing={"wall_seconds": wall})
        if too_long:
            with self._admission_lock:
                self._admission_closed = True
            # A partial JSON string can encode a credential in arbitrary escape
            # forms. With authentication configured, omit it rather than guess.
            exchange["response_text"] = None if secret else _prefix(retained, self.max_response_bytes)
            if secret:
                exchange["response_omitted_reason"] = "unparseable_authenticated_response"
            raise TextInferenceError("Text response exceeds max_response_bytes; no text returned", exchange=exchange)
        try:
            decoded = raw.decode("utf-8")
            body = strict_json(decoded)
        except (UnicodeError, DecisionError, RecursionError):
            exchange["response_text"] = None if secret else _prefix(retained, self.max_response_bytes)
            if secret:
                exchange["response_omitted_reason"] = "unparseable_authenticated_response"
            raise TextInferenceError("Text endpoint did not return one valid UTF-8 JSON response", exchange=exchange) from None
        if secret and (secret in decoded or _contains_secret(body, secret)):
            exchange["response_omitted_reason"] = "credential_echo"
            raise TextInferenceError("Response echoed a credential; response omitted and no text returned", exchange=exchange)
        if not isinstance(body, dict) or "error" in body:
            exchange["response_omitted_reason"] = "provider_error"
            raise TextInferenceError("Text endpoint returned an invalid envelope or API error; body omitted", exchange=exchange)
        exchange["response_text"] = decoded
        usage, timing = _usage_and_timing(self.provider, body, wall)
        exchange.update(usage=usage, timing=timing)
        if usage["completion_tokens"] is not None and usage["completion_tokens"] > text_request.max_output_tokens:
            raise TextInferenceError("Provider reported completion usage above the requested limit", exchange=exchange)
        if self.provider == "ollama":
            message, finish = body.get("message"), body.get("done_reason")
            if body.get("done") is not True:
                raise TextInferenceError("Ollama response is not a completed non-streaming response", exchange=exchange)
        else:
            choices = body.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise TextInferenceError("Expected exactly one text completion choice", exchange=exchange)
            message, finish = choices[0].get("message"), choices[0].get("finish_reason")
        exchange["finish_reason"] = finish if isinstance(finish, str) and len(finish) <= 64 else None
        if finish not in ("stop", "length"):
            raise TextInferenceError("Text completion has an unsupported or missing finish reason", exchange=exchange)
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or any(message.get(key) for key in ("tool_calls", "function_call", "images"))
                or type(message.get("content")) is not str):
            raise TextInferenceError("Expected assistant content text without tool or image output", exchange=exchange)
        content = message["content"]
        try:
            content.encode("utf-8")
        except UnicodeError:
            raise TextInferenceError("Assistant text contains invalid Unicode", exchange=exchange) from None
        return {"text": content, "finish_reason": finish, "truncated": finish == "length",
                "exchange": exchange, "usage": copy.deepcopy(usage), "timing": copy.deepcopy(timing)}
