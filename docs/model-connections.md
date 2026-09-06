# Connect a model before starting a game

`model-check` sends one **synthetic connection check** to your chosen model.
Every native game measurement is unknown. It validates the returned decision
and prints a public JSON report; it never opens Dwarf Fortress, queues a job,
or advances time. No game installation is needed.

Start from the [README](../README.md). The commands below use `dfeval`;
`python start.py` followed by the same arguments is equivalent from a checkout.
`setup` lets you choose a running local server and installed model, then records
the selection. If the server has only one local model, setup explicitly names
that choice. The package does not download models, start servers, choose a
replacement after failure, or fall back to a scripted policy.

To inspect an existing real model episode without a server or game, run
`dfeval demo --open`. It replays the curated Qwen2.5 brewing recording, including
public decisions, native product receipts, and raw citizen measurements.
`dfeval demo --recording probe --open` selects the earlier connection probe.
These are offline recordings, not new model calls.

## Model observation contract

Current adapters use `native-care-v1`, a deterministic projection of the native
care observation. Complete need and stock-item records become rows with
declared columns; native values, array order, and unknowns remain intact.
Incomplete or extended records remain objects. The projection omits declared
metadata such as bridge session IDs and UI state, without choosing a suggested
action or adding a care score. Its version is recorded in policy configuration.

Raw native snapshots remain in experiment logs. `policy_input` records the
projected observation, public decision history, and exact user-message byte
count and SHA256. Both native Ollama and compatible transports use the same
canonical JSON serialization. The observation limit is 256 KiB; excessive input
is rejected instead of dropping citizen, item, or product rows. See the
[full contract](benchmarking.md#what-the-policy-observes).

The historical connection checks and packaged native model recording below
retain their original pre-projection inputs. They are not performance evidence
for this new input representation or proof of an installed Ollama run.

## 1. Use a local model

Run a model server on your own computer. Local connections accept loopback
addresses only. An unauthenticated local server needs no provider account;
downloading the selected model initially requires its chosen source.
Local model support does not imply native game support on the same platform;
see the [platform evidence](../README.md#what-has-been-verified).

### Ollama

Install Ollama using its official [Windows](https://ollama.com/download/windows),
[Linux](https://ollama.com/download/linux), or [macOS](https://ollama.com/download/mac)
instructions. Select and download a model that fits your machine. Use its exact
installed name in `--model`. Start the Ollama app or its server before checking:

```sh
ollama serve
```

If the app already runs its server, leave that server running instead of starting
another instance. Then use a separate terminal:

```sh
dfeval models
dfeval model-check --provider ollama --model "YOUR_INSTALLED_MODEL" --timeout 300 --out runs/ollama-check
dfeval setup --provider ollama --model "YOUR_INSTALLED_MODEL"
```

Native Ollama uses `http://127.0.0.1:11434/api/chat`; `--base-url` accepts a
different loopback server root. Discovery reads installed model metadata from
`/api/tags` without generating tokens. Before a benchmark, the selected model's
name, digest, parameter size, and quantization are recorded where reported.
See Ollama's [chat API](https://docs.ollama.com/api/chat) and
[model inventory API](https://docs.ollama.com/api/tags).

The native request uses the decision JSON schema, `stream: false`, a bounded
`num_predict`, `num_ctx: 16384`, temperature and seed zero, `think: false`, and
`keep_alive: "10m"`. It sends no tools. These settings and the response's native
timing/token counters are preserved. Host validation still rejects invalid
actions; JSON generation is not a substitute for it. See
[benchmark settings and throughput](benchmarking.md).

For a local-only setup, start Ollama with `OLLAMA_NO_CLOUD=1` in its environment;
restart an already running server for that setting to take effect. This is an
operator choice: dfeval does not edit your global Ollama configuration. Obvious
cloud model tags are rejected by the local adapter, but a loopback address alone
cannot attest how another server performs inference.
[Ollama local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features).

Native Ollama has automated transport tests, including an actual loopback HTTP
test server. An actual Ollama 0.33.3 Windows runtime with an imported
Qwen2.5-1.5B-Instruct Q4_K_M model also passed a synthetic connection check.
Its native `native-care-v1` trial reached the 512-token output cap before
completing a decision: zero actions and ticks were accepted, and final pause
and FPS restoration were confirmed. The [native care pilot](native-care-pilot.md)
records the pinned assets, CPU residency evidence, settings, and failed outcome.

### LM Studio and compatible server presets

Start your selected server and load a model using its own interface. Then list
the served IDs and use that exact ID in setup:

```sh
dfeval models --provider lmstudio
dfeval setup --provider lmstudio --model "YOUR_SERVED_MODEL"
```

| Provider | Default server root | Discovery / generation |
| --- | --- | --- |
| `ollama` | `http://127.0.0.1:11434` | Native `/api/tags` / `/api/chat` |
| `lmstudio` | `http://127.0.0.1:1234` | `/v1/models` / `/v1/chat/completions` |
| `llamacpp` | `http://127.0.0.1:8080` | `/v1/models` / `/v1/chat/completions` |
| `compatible` | Explicit `--base-url` required | `/v1/models` / `/v1/chat/completions` |

For example, `dfeval model-check --provider lmstudio --model "YOUR_SERVED_MODEL" --timeout 300`
tests the same adapter before game access. Presets use JSON Schema by default;
compatible servers can explicitly request `--response-format json_object`.
Context, temperature, seed, thinking, and keep-alive flags apply only to native
Ollama; set these in other servers and preserve their configuration yourself.
Unsupported preset-specific flags fail rather than being silently ignored.
[LM Studio's model API](https://lmstudio.ai/docs/developer/openai-compat/models)
documents discovery, including its just-in-time model-loading behavior.

### Portable llama.cpp

Use a release appropriate to your operating system, processor, and desired
backend from the official [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases).
Keep the extracted runtime and your chosen GGUF model outside the source tree,
or inside its ignored `runs` directory. Record the release, model source,
quantization, and SHA256 when preparing an experiment.

The official installation guide also lists these package-manager choices:

| Platform | Installation command, when that package manager is installed |
| --- | --- |
| Windows | `winget install llama.cpp` |
| macOS | `brew install llama.cpp` |
| Linux | `conda install -c conda-forge llama.cpp` |

[Official installation instructions](https://github.com/ggml-org/llama.cpp/blob/master/docs/install.md).

After downloading the runtime and model, start the server with the model's
actual local path and an explicit API alias. In Windows PowerShell, from the
extracted runtime directory:

```powershell
.\llama-server.exe -m "path\to\chosen-model.gguf" --alias "YOUR_MODEL_ALIAS" --host 127.0.0.1 --port 8080 --offline --no-agent --no-ui
```

On Linux/macOS, from an extracted runtime directory:

```sh
./llama-server -m "/path/to/chosen-model.gguf" --alias "YOUR_MODEL_ALIAS" --host 127.0.0.1 --port 8080 --offline --no-agent --no-ui
```

For a package-manager installation, use `llama-server` from your PATH. `--offline`
prevents model fetching after the assets are available. Keep server tools and
MCP integrations disabled: no `--tools`, `--agent`, or MCP server configuration.
The example explicitly disables agent mode; dfeval sends no tool declarations.
Check your pinned build's `--help` against the
[official server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

For a model/template that supports non-thinking responses, optionally append
`--reasoning off`. It can help a small response allowance produce final JSON,
but changes the model's configuration; record that choice. Support depends on
the chosen model/template and server version. The check rejects truncated or
invalid content rather than treating it as a decision.

Wait for the server to finish loading, then in a separate terminal:

```sh
dfeval model-check --policy local --model "YOUR_MODEL_ALIAS" --endpoint http://127.0.0.1:8080/v1/chat/completions --response-tokens 512 --timeout 120 --out runs/llama-check
```

These are setup instructions, not a claim that every runtime, quantization, or
model has passed this project's checks.

### Verified recipe: Qwen3-0.6B on Windows CPU

On **2026-09-05**, one real local `model-check` succeeded with
**llama.cpp b10809** and **Qwen3-0.6B-Q8_0**. The server reported fingerprint
`b10809-5266f24da`. It returned an unfenced, valid `wait` decision from the
synthetic input: 404 prompt tokens, 79 completion tokens, and 7.594 seconds
for that call. The report recorded `game_access: false` and
`actions_executed: 0`. This establishes a working model connection and response
contract on that configuration, not a game episode, completed brewing, or model
care ability. The timing is one observation, not a performance comparison.

The downloaded files were pinned and their SHA256 values recorded:

| Asset | Bytes | SHA256 |
| --- | ---: | --- |
| `llama-b10809-bin-win-cpu-x64.zip` | 18,407,457 | `9df3158ed228a641a4b127942d7f459f24c9e13f04682659d05c00c80099b6b5` |
| `Qwen3-0.6B-Q8_0.gguf` | 639,446,688 | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031` |

Runtime source: the official [b10809 release](https://github.com/ggml-org/llama.cpp/releases/tag/b10809),
specifically its [Windows CPU archive](https://github.com/ggml-org/llama.cpp/releases/download/b10809/llama-b10809-bin-win-cpu-x64.zip).
Model source: Qwen's repository at revision
`23749fefcc72300e3a2ad315e1317431b06b590a`, with the
[exact Q8_0 file](https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/23749fefcc72300e3a2ad315e1317431b06b590a/Qwen3-0.6B-Q8_0.gguf).
Neither binary nor model weights are included in this project.

After downloading and verifying those files, use your own relative locations
for the extracted `runtime` directory and `models` directory. The successful
server settings, with machine-specific paths replaced, were:

```powershell
.\runtime\llama-server.exe `
  --model ".\models\Qwen3-0.6B-Q8_0.gguf" --alias "Qwen3-0.6B-Q8_0" `
  --host 127.0.0.1 --port 18080 --threads 4 --threads-batch 4 `
  --ctx-size 16384 --parallel 1 --n-gpu-layers 0 --reasoning off `
  --offline --no-webui --no-webui-mcp-proxy `
  --cors-origins http://127.0.0.1:18080 `
  --no-jinja --chat-template chatml --seed 17 --temp 0 --n-predict 512
```

No server agent tools or MCP integrations were enabled. The client connected
to loopback without a key. In a second terminal:

```sh
dfeval model-check --policy local --model "Qwen3-0.6B-Q8_0" --endpoint http://127.0.0.1:18080/v1/chat/completions --response-format json_schema --response-tokens 512 --timeout 120 --out runs/qwen-chatml-check
```

On Linux/macOS, select that platform's runtime asset and adapt the executable
path and shell continuation syntax. The Windows archive hash above applies
only to Windows; this recipe has not been verified on those other platforms.

**The ChatML override is part of this recipe.** `--no-jinja --chat-template chatml`
uses the server's legacy ChatML rendering instead of the model's embedded Jinja
template. It also takes the direct JSON-grammar path. This changes the model
configuration and must be recorded when comparing experiments; it is not a
transparent parser repair. The pinned [legacy rendering and grammar code](https://github.com/ggml-org/llama.cpp/blob/b10809/common/chat.cpp#L3398-L3456)
shows that route. The successful recipe does not use `--skip-chat-parsing`.

Three earlier checks failed and were retained as failures:

| Configuration | Observed result |
| --- | --- |
| Embedded template with `json_object` | Markdown-fenced JSON; rejected by the host |
| Embedded template with the initial bounded-string `json_schema` | HTTP 400 before sampling: generated grammar exceeded the runtime's repetition limit |
| `--skip-chat-parsing` with `json_object` | Markdown-fenced JSON again; rejected by the host |

In b10809, `char{0,2000}` reaches the grammar parser's rejection threshold.
See the [pinned repetition guard](https://github.com/ggml-org/llama.cpp/blob/b10809/src/llama-grammar.cpp#L432-L463).
The current generation schema therefore omits `maxLength`. The prompt and
host validator still enforce 2,000 characters for `reason`, 4,000 for
`notebook`, the 16,384-byte decision limit, and the action-specific fields.
No fences were stripped, no invalid response was repaired, and no automatic
retry or model fallback converted a failed check into success.

Local evidence was retained as `model-check-qwen-chatml/model-check.json`,
`local-runtime/provenance.json`, and `local-runtime/server-process.json` under
the ignored `runs` directory, alongside the failed checks and server logs.
Those raw records contain local paths and are not bundled in the release.

### Native observation checks: no accepted model action

Two subsequent checks on **2026-09-05** supplied this model with actual
Dwarf Fortress **53.16** / DFHack **53.16-r1.1** observations: seven citizens,
60 drink stack units, and no reported observation errors. Brewing product hooks
reported available, but no brewing job or product was produced by these checks.

The first reached its 120-second policy timeout with zero accepted decisions.
Its result is `budget_exhausted` with `ok: true`, meaning a controlled budget
stop, not successful gameplay. A second used a 300-second request timeout,
360-second wall budget, and limits of one decision and one game tick. It returned
`finish` with forbidden `workshop_id: 0` and `quantity: 10` fields. The host
rejected it with `DecisionError`. **Both attempts recorded zero requested and
elapsed ticks and confirmed the final pause.** They validate these stopping
and rejection paths, not successful autonomous play or brewing.

The second request contained 7,732 prompt tokens and returned 56 completion
tokens, taking about 197 seconds in the server. The earlier synthetic check's
7.594-second duration did not predict full-observation latency. On this CPU,
even a 120-second allowance was insufficient. `benchmark` now defaults to a
300-second request timeout and 3,600-second episode budget; standalone
`model-check` and the advanced `experiment` command retain 30-second request
defaults. Choose explicit budgets with actual observation size in mind. The
retained local evidence directories are
`runs/native-model-integration-one-tick` and
`runs/native-model-integration-one-tick-300s`.

## 2. Use a cloud model

Choose the model and compatible HTTPS endpoint explicitly. The selected service
receives the synthetic prompt during `model-check`; a later `experiment` sends
actual native observations and public decision history. A cloud check can incur
provider charges.

Cloud mode defaults to the OpenAI Chat Completions endpoint and `OPENAI_API_KEY`.
For another service, set `--endpoint` to the complete Chat Completions URL and
`--api-key-env` to the **name** of the environment variable containing its key.
The CLI checks that variable before calling the provider. Follow the selected
provider's account and model-access instructions.

Enter a key without including its literal value in the command history. In
PowerShell:

```powershell
$connectionSecret = Read-Host "Provider API key" -AsSecureString
$env:MODEL_API_KEY = [System.Net.NetworkCredential]::new("", $connectionSecret).Password
Remove-Variable connectionSecret
```

In Bash:

```bash
read -r -s -p "Provider API key: " MODEL_API_KEY
export MODEL_API_KEY
```

Then select the endpoint and model:

```sh
dfeval model-check --policy cloud --model "YOUR_CLOUD_MODEL_ID" --endpoint "https://YOUR_PROVIDER/v1/chat/completions" --api-key-env MODEL_API_KEY --timeout 120 --out runs/cloud-check
```

Compatible `--policy local` uses the request field `max_tokens`; cloud mode defaults to
`max_completion_tokens`. Select `--token-limit-field max_tokens` if your chosen
cloud endpoint requires it. `--response-tokens` sets that request's maximum.
The provider must support the client's JSON response format and a single text
completion. Compatibility is established by the actual returned response.

The advanced `--policy local|cloud` interface defaults to
`--response-format json_object`, which requests plain JSON. For a provider
that supports JSON Schema, explicitly select `--response-format json_schema`
in both `model-check` and `experiment`. That sends a generation schema with separate
closed objects for `wait`/`finish` and `brew`, so brewing fields are required only
for `brew` and forbidden otherwise. Its `strict` flag remains false because provider
support varies; the host independently enforces every action-specific rule.
Neither mode guarantees that a provider will comply. Markdown fences, extra keys,
missing fields, and invalid values still fail without executing an action. The
selected response format and full schema are preserved in the public record.
String-length limits remain in the prompt and host validator; they are omitted
from the generation schema because some runtimes cannot compile large bounded
string repetitions.

## Read the result

Exit status `0` and `"ok": true` mean one call produced a valid fixed-schema
decision. They do not establish care, useful planning, completed brewing, or a
successful game episode. Even a returned `brew` action is only recorded as data.

`--out` writes `model-check.json` to a new or empty directory. Without it, the
report is printed without creating files. It includes the synthetic input,
selected configuration, public response, validated decision, timing, and usage
when reported. Authentication headers are excluded and configured credential
echoes are redacted and rejected. Review public responses before sharing them.

Failures return a nonzero exit status. Check `error`, `publicexchange`, and
`response_truncated` within that exchange. A missing key, wrong model name,
unsupported token field, insufficient response allowance, or timeout can require
a different explicit setting. The client does not retry automatically. Invalid
configuration can fail before a report file is created.

`--timeout` also bounds the overall wait. A provider request already in progress
may finish later; its decision is discarded. The report marks
`background_request_may_continue` when appropriate. This does not guarantee
provider-side cancellation or cancellation of billing.

After a successful connection check, use the same chosen settings with
[`experiment`](../EVALUATION.md) and a separately prepared fortress. Connection
checks have no native run ledger and are not inputs to `compare` or `watch`.
