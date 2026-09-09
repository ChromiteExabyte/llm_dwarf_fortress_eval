# Run and inspect a native care evaluation

The model decides what **care** means within the sandbox and how to act on that
understanding. The evaluator supplies a game, observations, actions and finite
budgets. Native measurements are evidence for inspection, not a prescribed or
hidden definition of good care. Readers can examine the model's stated
priorities, its decisions and their consequences without accepting its own
account as a verdict on the run.

An experiment gives a policy native fortress observations and a limited action
interface, then records what happens. The software supports local/cloud models
and two scripted baselines. A real local model episode has completed three
brewing actions, with two native jobs producing 50 new drink stack units. A
second model passed the restored starting-state guard but exhausted its
256-token response limit and ended with an error, without accepted actions or
elapsed ticks. The exported paired report retains this failure. See the
[native loop proof](docs/native-loop-proof.md). The default `dfeval demo` replays
a reviewed excerpt of model A's actual trial, including public decisions, raw
citizen measurements, and native product receipts. Use `--recording probe` for
the earlier probe without model decisions. Neither recording contains game video.

For the everyday local workflow, run `python start.py` from the checkout, or
`dfeval setup` followed by `dfeval benchmark` after package installation.
Setup selects a running local runtime, installed model, and game folder.
The [benchmark guide](docs/benchmarking.md) covers native Ollama, performance
settings, and the JSON/CSV/Markdown report written for each recorded benchmark.
The sections below explain the shared evaluation boundary and the advanced
`experiment` interface for explicit compatible endpoints and scripted baselines.

Install the package and prepare a loaded fortress using [LIVE_GAME.md](LIVE_GAME.md).
For a meaningful brewing scenario, provide an existing completed still,
brewable plants, containers, and workers. The public
[drink-maintenance specification](scenarios/live_care.json) records the original
brewing fixture and limits. Its drink-supply objective is historical; the current
model briefing leaves the meaning of care to the model. The fixture identifier
still names the same limited mechanics. It contains no save assets and is not an input to the
mock simulator's `run --scenario` command.

## Choose a policy explicitly

First use [`model-check`](docs/model-connections.md) to verify the selected
connection with synthetic input and no game access. That guide includes local
Ollama/llama.cpp setup and cloud credentials. A successful connection check is
not a native evaluation result.

| `--policy` | Required setup | Behavior |
| --- | --- | --- |
| `local` | A running loopback model server and explicit `--model` | Calls a compatible Chat Completions endpoint; default `http://127.0.0.1:11434/v1/chat/completions` |
| `cloud` | Explicit `--model` and a key environment variable | Sends observations and public decision history to the selected HTTPS endpoint |
| `idle` | No model service or key | Waits each turn without queuing work |
| `rule` | No model service or key | Queues one brewing job at an idle completed still, otherwise waits |

The baselines operate on the real game and are labeled as non-model policies.
Model mode never silently falls back to a baseline, simulator, or other provider.
The package does not launch model servers or download models.

Replace the model placeholder with the exact ID served by your local endpoint:

```sh
dfeval experiment --df-path "/path/to/Dwarf Fortress" --policy local --model "YOUR_LOCAL_MODEL" --out runs/local-care --watch
```

Use `--endpoint` for another compatible loopback URL. Remote endpoints use cloud
mode and HTTPS. The client requests JSON responses and defaults to `max_tokens`
for local mode or `max_completion_tokens` for cloud mode; use
`--token-limit-field max_tokens` if your cloud endpoint requires that field.
Endpoint/model compatibility has not been established by a paid model run.

Cloud mode defaults to `https://api.openai.com/v1/chat/completions` and reads
`OPENAI_API_KEY`. Choose another endpoint and key variable with `--endpoint`
and `--api-key-env VARIABLE_NAME`. Pass the variable's **name**, never the key
itself. The configured remote service receives the native observations and
public history; the local spectator does not upload them.

You can populate the environment without typing a literal secret into a command:

```powershell
# PowerShell
$secret = Read-Host "API key" -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
Remove-Variable secret
```

```bash
# Bash
read -r -s -p "API key: " OPENAI_API_KEY
export OPENAI_API_KEY
```

Then run the explicitly selected cloud model:

```sh
dfeval experiment --df-path "/path/to/Dwarf Fortress" --policy cloud --model "YOUR_CLOUD_MODEL_ID" --out runs/cloud-care --watch
```

Keys are read from the environment and authentication headers are not recorded.
Manifests contain the key variable's name. Keep credentials out of source files
and review transcripts and local paths before sharing evidence.

## What the policy can do

The runner initially pauses the loaded fortress and sends its native snapshot.
Each response must contain one validated action, a short public `reason`, and
a public `notebook`. The current snapshot and up to eight previous validated
decisions provide context; notebook entries are part of that public history.
These outputs are not a transcript of hidden reasoning.

The default briefing asks the model to use its notebook to state its own
understanding of care and priorities, which it can revise during the run.
Those statements and subsequent actions remain inspectable; the host does not
validate a philosophical answer or turn self-description into a care score.
The current three-action interface sharply limits what the model can express
through gameplay. Broader ordinary game actions remain to be implemented.

| Action | Game effect |
| --- | --- |
| `wait` | Advance the configured tick interval, then observe again |
| `brew` | Queue 1–10 brew-from-plant jobs at a validated completed still, advance the same interval, then observe |
| `finish` | End the episode without advancing another interval |

Brewing still depends on native labor, ingredients, containers, access, and job
rules. A queued job or a disappearing job ID alone does not prove completed
brewing. No arbitrary Lua, DFHack commands, filesystem paths, shell commands,
or browser tools are accepted through the model's decision schema.

Native snapshots now include `brewing` evidence from DFHack's reaction-product
callbacks. Each event links a session-queued job to newly allocated output item
IDs. The care summary counts new drink items and their recorded stack units;
returned barrels, stock changes, and job disappearance do not count as production.
Missing hooks, dropped events or outputs, unknown quantities, and measurement
errors remain visible. Software tests cover the hooks against fake DF objects;
the [native model trial](docs/native-loop-proof.md) also verified real-game
production from model-queued jobs. The default model demo retains those product
receipts. The optional older `demo --recording probe` has no product evidence,
which remains unknown.

For disposable test saves, the separate [operator fixture script](docs/fixture-setup.md)
can prepare a still and inputs before scoring. Its setup actions are outside the
model interface and must be declared in the scenario. Preserve the original and
capture the prepared, fully saved and stopped copy before comparing policies.

The trusted Python host owns the game adapter, provider connection, and records.
This is an application-level boundary, not an operating-system sandbox against
untrusted host code or a compromised game process. The current scope does not
include autonomous embark, construction, mining, or general fortress management.

## Bound the run and stop it

Defaults are recorded in each run manifest. `benchmark` and `experiment` share
the bounds below except for their request and total wall-time defaults:

| Limit | Default | CLI option |
| --- | --- | --- |
| Decisions / policy calls | 12 / 12 | `--decisions`, `--max-calls` |
| Ticks after wait or brew | 1,200 | `--ticks-per-decision` |
| Total requested game ticks | 14,400 | `--max-ticks` |
| Bridge calls | 64 | `--max-bridge-calls` |
| Per-response token request | 512 | `--response-tokens` |
| Reserved output-token budget | 8,192 | `--max-output-tokens` |
| Total recorded response bytes | 262,144 | `--max-output-bytes` |
| Episode wall time | `benchmark`: 3,600 seconds; `experiment`: 600 seconds | `--max-seconds` |
| Request timeout | `benchmark`: 300 seconds; `experiment`: 30 seconds | `--timeout` |

The simulation FPS cap stays unchanged unless `--simulation-fps 1..10000` is
specified. Cleanup attempts to restore its original value and records
`speed_restored` as well as `pause_confirmed`; an unresponsive game can prevent
confirmation. This changes the game's simulation cap, not hardware clock rates
or the renderer's FPS. A larger cap does not promise that throughput.

The output-token budget reserves requested limits before calls; reported provider
usage is tracked separately and can be unknown. These are not monetary caps or
a guarantee about provider billing. Input, snapshot, and log byte limits also
apply. Invalid or late model responses do not authorize game actions.

Create a file named `STOP` inside the run directory, or press Ctrl+C in the
runner's terminal, to request a recorded stop. `--stop-file PATH` selects another
operator-controlled stop file. The runner checks the file between host operations
and while awaiting a policy. It cannot interrupt a blocking bridge/bootstrap
call before that call's timeout or advance horizon. Ctrl+C during a bridge wait
also publishes a cancellation request for the resident script.

Cleanup attempts a final pause, with up to one
request-timeout interval beyond the main wall deadline. Check `pause_confirmed`;
a dead or unresponsive game cannot acknowledge it. Stopping the runner does not
guarantee cancellation of an already submitted provider request.

`--watch` opens the local spectator and leaves it available after the run ends;
Ctrl+C then closes the spectator. Normal budget exhaustion is a termination
reason, not a passing care grade.

An explicitly empty citizen roster, or a roster containing only records marked
`dead: true`, ends the run with `no_citizens` and an unsuccessful outcome. An
unknown roster or death flag is not treated as extinction; malformed citizen
records produce an error.

## Preserve one starting save

Save and **fully exit the game** before capture or restore. The explicit
`--game-stopped` flag attests to this; Windows/Linux process checks also run.
They do not lock the operating system against someone launching the game later.

```sh
dfeval scenario capture --df-path "/path/to/Dwarf Fortress" --save-name region1 --out saves/drink-start --game-stopped
dfeval scenario verify saves/drink-start
```

Capture leaves the original save untouched. Its new snapshot contains a
`manifest.json` and a `save` directory. Verification checks every file's size
and SHA256 plus the complete directory inventory. The manifest has no absolute
paths; the save bytes and local operation records should remain excluded from
publication. Verification establishes byte identity, not a compatible or
well-prepared fortress.

To restore over an existing save, supply a **new exact backup directory on the
same filesystem**. Existing backup paths are refused. The original save is
retained at that path; promotion failures attempt to put it back.

```sh
dfeval scenario restore saves/drink-start --df-path "/path/to/Dwarf Fortress" --save-name region1 --backup "/path/to/save-backups/before-run-a" --game-stopped
```

Omit `--backup` only when the destination save does not exist. Each restoration
over an existing save needs a fresh backup path. Restart the game and load the
restored fortress before the next experiment; restore does not launch it.

## Compare recorded runs

For a recorded local/cloud **model** run, the [repeat commands](docs/benchmarking.md#repeat-a-starting-site-with-another-model)
automatically preserve the finished save, restore its declared checkpoint, copy
all run/request settings, and check the loaded initial state before calling a
different model. Capture the checkpoint before the first run and pass
`--starting-snapshot` to that run. `repeat prepare` requires the game to be fully
saved and closed; `repeat run` follows after you load the restored fortress.
The following manual workflow also supports scripted baselines and explicit
changes of model provider.

The exact system briefing is part of the recorded policy settings. New runs
use the model-defined-care briefing introduced on 2026-09-08. A repeat preserves
its source briefing, including the immediately preceding `native-care-v1`
briefing; it does not silently adopt the new objective. Other unrecognized
briefings remain unsupported. Old recordings retain their original text.
For a model-only comparison, use the same briefing as well as the same start
and limits. `recorded_setup_matches` checks environment and execution conditions;
it does not assert identical policy briefings. Changed briefings are distinct
experimental conditions, shown in each run's `policy_config`.

For each policy, restore the **same snapshot**, load it, and run with the same
declared budgets. Preserve the snapshot identity in the experiment:

```sh
dfeval experiment --df-path "/path/to/Dwarf Fortress" --policy idle --starting-snapshot saves/drink-start --out runs/idle-a
```

After saving/exiting and restoring again, use another output directory and the
chosen `rule`, `local`, or `cloud` policy. Do not compare sequential runs on an
already changed fortress as though they had the same start.

**`--starting-snapshot` verifies and records the snapshot manifest's hash. It
does not restore a save or prove that the current in-memory game matches it.**
The comparison separately fingerprints the initial native observation, excluding
pause and UI-selection metadata.

```sh
dfeval watch --run runs/idle-a --open
dfeval compare runs/idle-a runs/rule-a
dfeval compare runs/idle-a runs/rule-a --json
dfeval benchmark-report runs/idle-a runs/rule-a --out reports/comparison
```

For a recording someone can inspect without Python or a local server:

```sh
dfeval export --run runs/idle-a --out runs/idle-a.html
```

The standalone HTML contains the replay, the model's original public notes,
the exact recorded briefing, and allowlisted original evidence downloads.
Later journal entries appear only when the playhead reaches them or the reader
explicitly enables full history. Missing notes or conflicting briefing records
are displayed as unknown; the viewer supplies no interpretation or care grade.
It retains incomplete/failed status, omits video/frame images, and makes no game
or model calls. Review private prompts, responses, and paths before sharing;
raw exports are not automatically sanitized. Keep original runs and checkpoints
for comparisons and repeats. See [export details](docs/benchmarking.md#export-a-standalone-recording).

`compare` accepts distinct native **experiment** directories, not mock ledgers.
Use original run bundles for comparison; packaged demo excerpts are curated
inspection material with declared omissions and substitutions. The command
recomputes outcomes from snapshot events rather than trusting stored summaries.
Setup checks cover declared save and initial
observation identities, the bridge script/protocol, scenario, game versions,
and budgets, including memory, observation, and log limits.

It also checks event ordering, start/end records, manifest/configuration
agreement, initial-snapshot integrity, and native world/time continuity.
Missing evidence, abnormal termination, or an unconfirmed final pause prevent
a setup-match result. Endpoint measurements remain inspectable; changes across
snapshots are suppressed when world/time continuity is unverified. Policy/model
identity and per-call response settings are displayed separately rather than
required to match, so inspect them when assessing a comparison. Matching recorded
setup does not establish deterministic simulation, statistical significance,
or a model ranking.

Reports retain individual measurements and distributions: population, confirmed
deaths, drink stacks, stress, needs, hunger/thirst, and wounds. Population follows
the native living-citizen roster, excluding any explicitly dead entries in older
records. A missing death flag remains unknown, and missing citizens are not
automatically counted as deaths. Candidate stocks do not prove access.
Sampled changes do not establish causation or capture every intervening event.
There is no composite wellbeing score.

`benchmark-report` exports care measurements and execution rates to
`benchmark.json`, `runs.csv`, and `README.md`, including failed or incomplete
recordings with their limitations. It contacts neither game nor model and
recomputes results from native events. It does not restore saves or run a model
sweep. See [measurement definitions](docs/benchmarking.md#read-care-and-speed-separately)
before comparing simulation throughput or token rates.

Repeat runs across multiple declared starting saves before making comparative
claims. Earlier evidence includes a Windows native probe, a synthetic local
model connection, and two native-observation model checks that stopped at a
timeout or rejected an invalid response. Those two checks had zero accepted
model actions, zero elapsed ticks, and confirmed final pauses. See the
[earlier recorded checks](docs/model-connections.md#native-observation-checks-no-accepted-model-action).

The subsequent [native loop trial](docs/native-loop-proof.md) exercised a local
Qwen2.5-1.5B-Instruct model for three autonomous brewing actions and exactly
3,600 ticks. Two queued jobs completed, with callbacks confirming 50 new drink
stack units; final pause and original FPS-cap restoration were confirmed. After
restoring the captured checkpoint, Qwen2.5-0.5B-Instruct passed the initial-state
guard before inference. It repeated its public reason until the response hit
the 256-token limit (`finish_reason: length`), ending with an error and zero
accepted decisions, actions, or elapsed ticks. Final pause and FPS-cap
restoration were confirmed. The exported paired report is qualified by the
model error, not a starting-state mismatch. This is evidence of the bounded
production loop, not a claim of improved welfare or model superiority.
