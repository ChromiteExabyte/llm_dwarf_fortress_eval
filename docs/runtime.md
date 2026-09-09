# The new runtime foundation

The direction is a fresh undeveloped embark, a sparse objective such as
`Care for the dwarves`, and a model that chooses its strategy, resource use,
memory, records and tools. An external witness preserves what happened.
There is no prescribed care score, mandatory journal, supplied build order or
prebuilt team. The [project direction](../PROJECT_DIRECTION.md) distinguishes
this target from the current native brewing experiments.

This release adds a **development inference broker**. It serves one fixed model
to independently constructed text contexts, shares a finite resource account,
and records exchanges before delivering responses. It does not connect to the
game, run generated code, establish an OS sandbox, or start an autonomous agent.
The existing `benchmark` and native recordings retain their original interface.

## Start the development broker

Use an already installed local Ollama model:

```sh
python start.py runtime-serve --out runs/runtime-first --model YOUR_INSTALLED_MODEL --num-ctx 32768
```

The command creates a new session directory and refuses an existing one. It
starts no model calls by itself. Stop it with Ctrl+C; its wall allowance also
expires. It binds only to `127.0.0.1` on an automatically selected port.

Compatible local servers, including llama.cpp and LM Studio, use their full
Chat Completions endpoint:

```sh
python start.py runtime-serve --out runs/runtime-compatible --provider chat_completions --model YOUR_MODEL_ID --endpoint http://127.0.0.1:8080/v1/chat/completions
```

For a cloud connection select `--provider chat_completions --mode cloud`, an
explicit HTTPS `--endpoint`, and `--api-key-env YOUR_KEY_VARIABLE`. Set the key
outside the command and source files. The client does not discover proxies,
follow redirects, retry, change models or fall back to local inference.
Cloud mode defaults to `max_completion_tokens`; `--token-limit-field max_tokens`
is available for endpoints requiring that field. Context capacity for compatible
servers is configured on the server and must be retained with the experiment's
environment records; it is not measured or set by this broker.

Files in the new directory:

| Path | Purpose |
| --- | --- |
| `workspace/` | Initially empty space reserved for future agent-owned files |
| `evidence/manifest.json` | Objective, optional episode declarations, fixed provider settings and limits |
| `evidence/events.jsonl` | Ordered request reservations and provider outcomes, with hashes |
| `runtime-connection.json` | Local broker URL and bearer token for development clients |

The bearer token grants access to the shared inference allowance. It is separate
from the provider credential, excluded by `.gitignore`, and never printed by
the server. This directory is local evidence, not automatically sanitized
publication material. The API exposes no evidence paths or provider credential.

## Mechanical interface

Every request requires `Authorization: Bearer TOKEN` and an exact numeric
loopback Host header, which ordinary URL clients supply. Browsers with an
Origin header, chunked requests, redirects and ambiguous framing are rejected.

- `GET /v1/runtime` returns the exact objective, model ID, limits and current
  resource account, plus the minimal inference interface description.
- `POST /v1/infer` accepts `messages`, `max_output_tokens`, and optional
  `temperature` in a JSON body. Each message has only a `role` (`system`, `user`,
  or `assistant`) and text `content`.

For example, this **operator-side development client** makes one request:

```python
import json
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

connection = json.loads(Path("runs/runtime-first/runtime-connection.json").read_text())
opener = build_opener(ProxyHandler({}))
headers = {"Authorization": "Bearer " + connection["token"],
           "Content-Type": "application/json"}
with opener.open(Request(connection["url"] + "/v1/runtime", headers=headers)) as response:
    runtime = json.load(response)
payload = {"messages": [{"role": "user", "content": runtime["objective"]}],
           "max_output_tokens": 512}
with opener.open(Request(connection["url"] + "/v1/infer", headers=headers,
                         data=json.dumps(payload).encode()), timeout=310) as response:
    print(json.load(response))
```

The broker adds no system message, prior transcript, care explanation, memory
summary, action schema or helper identity. Callers choose their entire context
each time. It has no agent registration or spawn operation. Code and a general
inference interface are sufficient ingredients for a later isolated runtime
to construct its own organization, if the model chooses to do so. This example
is documentation for developers; it is not copied into an evaluated workspace.

Responses contain a call ID, text, finish reason, reported usage, timing and
remaining resources. A token-limited completion (`length`) remains available
as partial text. No action is inferred or executed from it.

## Resource accounting and failure evidence

Use `runtime-serve --help` for configurable ceilings. Defaults allow 64 calls,
131,072 output tokens, 16 MB cumulative canonical request bytes, four concurrent
calls and one hour of wall time. A call defaults to at most 32,768 output tokens,
262,144 request bytes and 300 seconds. Ollama context capacity is separately
configured by `--num-ctx`; capacity is not processed tokens.

Admission is atomic across all clients. The broker reserves each requested
output cap before dispatch and refunds unused tokens only after a successful
response with a valid reported completion count. Failures or missing usage
consume the reserved cap. Calls and canonical input bytes remain charged.
Reported prompt, output and cached prompt counts are retained separately, with
explicit counts of exchanges where those measurements are unknown. The byte
allowance covers canonical request JSON; transport framing and provider envelope
sizes have their own bounds and are not treated as prompt-token measurements.

These limits bound requests made through the broker. They do not make output
tokens an equal-compute budget, cap a cloud bill, measure hardware compute, or
force a provider to stop when its connection is abandoned. Tool compute, game
simulation and disk used by future agent programs still require an isolated
executor and separate accounting. Witness time is recorded separately from
provider wall time; session wall time includes both.

A request reservation must be durably recorded before dispatch. A provider
response or failure must be durably recorded before output is returned. A
witness failure closes admission. A timeout or uncertain transport failure
also closes admission, retains its reservation and records that provider work
may continue. Late output is not delivered. A disconnected client causes no
retry or refund. Records establish that a response was preserved, not that
the client read it. Rejected HTTP requests do not constitute model calls.

Verify a journal with:

```sh
python start.py runtime-audit runs/runtime-first/evidence/events.jsonl
```

The closing command prints the final witness head. Retain that head separately
and pass it using `--head HEX_DIGEST` to detect truncation or rewriting relative
to that anchor. Hash chaining without an external anchor does not establish
origin or prevent someone replacing an entire journal. A missing close record
marks an incomplete journal. The verifier checks the chain, not native game
outcomes or the truth of a provider's measurements.

## Reusing starting conditions

`EpisodeSpec` describes the objective, starting-checkpoint SHA-256, declared
undeveloped embark, information condition, control disclosure, memory-write
choice and model-controlled pause. It is independent of model selection and
runtime limits. The exact objective is part of its canonical identity. It can
be reused for the same model or another model without importing prior contexts,
tools or journals. `--spec FILE` records a complete serialized specification
instead of `--objective TEXT`.

These are declarations, not evidence that a save was restored, a site is
undeveloped, or an information boundary exists. Memory-write permissions may
remain explicitly unresolved in these development records because no game
memory access exists here. A future live launcher must resolve and enforce its
actual permissions. Silence about memory modification in an objective does
not choose those permissions.

## Remaining boundary

All development client code still runs with the caller's host permissions.
Sibling evidence and workspace directories do **not** protect the witness from
another process running as the same user. Do not execute evaluated generated
code on the host through this component. The next implementation must place
code execution behind a real isolation boundary, keep provider secrets and
evidence outside it, and connect a declared game-information/control interface.
Player-information-only and raw-memory-capable runs need different enforced
conditions. Native from-embark autonomy and model-controlled game pause have
not yet been implemented or demonstrated.
