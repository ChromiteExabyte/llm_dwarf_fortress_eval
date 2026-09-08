# Run a local care benchmark

The benchmark connects a selected local model to a loaded Dwarf Fortress save,
records its bounded decisions, and exports care outcomes alongside execution
speed. It does not assign a combined score or rank models automatically.

For a no-setup preview, run `python start.py demo --open` (or `dfeval demo` after
installation). The default is an actual model brewing episode with original
decisions and native product receipts. `--recording probe` selects the earlier
native probe. Both are curated offline recordings with source hashes and
sanitization notes; neither contacts a game or model.

## Set up once, then run

Install Python 3.11+, your chosen local model runtime and weights, and matching
Dwarf Fortress/DFHack separately. Start the model server. From the checkout:

```sh
python start.py models
python start.py setup
```

The launcher runs directly from source using Python's standard library; no pip
installation or virtual environment is required. Setup offers Ollama first,
then LM Studio, llama.cpp, or another compatible server.
It lists the server's installed/served models and saves your selection and game
folder in `dfeval.local.toml`, excluded from Git. No model is downloaded or
silently substituted. See [model connections](model-connections.md).

Load a prepared fortress with a completed still, brewable plants, containers,
and workers. Then run:

```sh
python start.py benchmark --out runs/my-model-a
```

With an already prepared game and running server, `python start.py` alone does
setup when needed and then benchmarks. An installed package exposes equivalent
`dfeval setup` and `dfeval benchmark` commands. `--config PATH` selects another
local TOML configuration; explicit benchmark flags override saved settings.

The model can wait, queue brewing, or finish. A queued job is not proof of
production. `--watch` opens the read-only spectator for live observations and
later replay; it stays available until Ctrl+C. The [evaluation guide](../EVALUATION.md)
explains the action boundary, stop files, final pause, and native care evidence.

## What the policy observes

New runs use the versioned `native-care-v1` projection for model and scripted
policies. It mechanically selects native care fields and serializes them as
compact UTF-8 JSON with sorted object keys and unchanged array order. Complete
need records use the columns `id`, `type`, `focus_level`, `need_level`,
`deity_id`; complete stock-item records use `id`, `item_type`, `stack_size`,
`candidate`, `in_job`, `excluded_by`. The input declares these columns.
Incomplete or extended records remain objects, preserving absent fields
separately from explicit `null` values.

The projection retains native time, citizens and former citizens, jobs,
workshops, stocks, brewing products, and errors. Declared metadata omissions
include versions, save identity, pause/UI state, FPS, explanatory measurement
notes, stock definitions, and brewing session/epoch IDs. Those values remain
in the raw snapshots and host validation evidence. No action advice, inferred
values, normalized welfare scores, or external summaries are added.

The manifest records the exact projection contract. Each `policy_input` records
the projected observation, retained public decisions, and the exact serialized
user-message byte count and SHA256. The native `snapshot` events remain the
source for per-dwarf inspection and care reports. The 256 KiB observation cap
rejects excessive input before policy execution; it never truncates citizen,
need, item, or product rows. Overall input/history budgets also apply.

Use the same projection version for a comparison. The packaged historical
proof retains its earlier raw model inputs; it has not been rewritten to look
like a projected run. Neither that recording nor the new representation alone
establishes better model performance or care.

## Set a game speed cap and finite budgets

```sh
python start.py benchmark --simulation-fps 1000 --watch --out runs/my-model-fast
```

`--simulation-fps` accepts 1–10,000 and temporarily changes the game's simulation
cap. Without it, the existing cap is unchanged. The runner attempts to restore
the original cap on exit and records `speed_restored`; inspect that value and
`pause_confirmed`, especially after errors. This setting does not change CPU/GPU
clock rates. Hardware, fortress complexity, and bridge overhead determine the
measured throughput; a requested 1,000 cap is not a promise of 1,000 ticks/second.

| Benchmark setting | Default | Override |
| --- | --- | --- |
| Decisions / policy calls | 12 / 12 | `--decisions`, `--max-calls` |
| Ticks after each wait/brew | 1,200 | `--ticks-per-decision` |
| Total requested ticks | 14,400 | `--max-ticks` |
| Output tokens requested per call | 512 | `--response-tokens` |
| Reserved output-token budget | 8,192 | `--max-output-tokens` |
| Request timeout | 300 seconds | `--timeout` |
| Episode wall time | 3,600 seconds | `--max-seconds` |
| Ollama context | 16,384 tokens | `--context-size` |
| Ollama temperature / seed | 0 / 0 | `--temperature`, `--seed` |
| Ollama thinking / model retention | Off / 10 minutes | `--think`, `--keep-alive` |

Bridge, input, history, and recorded-output limits also apply. Increasing a game
cap leaves decision and tick limits intact. Token allowances are reserved before
each call; provider-reported usage is recorded separately. A controlled budget
stop does not establish successful care. Invalid or late responses execute no
action. Cleanup can extend beyond the main deadline to request a final pause.

## Keep model performance interpretable

During a call, `ollama ps` shows model residency and context. CPU or mixed
CPU/GPU residency can explain a slow request; check the runtime's
[hardware support](https://docs.ollama.com/gpu) for your device. Increasing
context uses more memory. The benchmark explicitly requests 16,384 tokens;
choose enough context for the actual observation, history, and response, and
record any change. A small synthetic check does not measure full-observation
latency. [Ollama context and residency documentation](https://docs.ollama.com/context-length).

Ollama's `keep_alive` request controls model retention between calls. The
benchmark requests ten minutes and records load, prompt-processing, and
generation durations when reported. Keep cold-load and already-loaded results
distinguishable. Configure context, sampling, and retention directly in LM
Studio or llama.cpp; Ollama-only CLI flags are rejected for those presets.
[Ollama model retention](https://docs.ollama.com/faq#how-do-i-keep-a-model-loaded-in-memory-or-make-it-unload-immediately).

For local-only inference, follow the connection guide's `OLLAMA_NO_CLOUD=1`
setup. The project does not alter global runtime settings, tune GPU drivers,
start parallel sweeps, or choose a smaller model after a failure.

## Read care and speed separately

Every recorded `benchmark` run attempts an automatic export under `benchmark/`,
including runs that fail. Export requires at least one native snapshot. Earlier
bootstrap/status failures preserve `manifest.json`, `events.jsonl`, and
`result.json`, print the original outcome/pause status, and separately report
export as unavailable. Configuration/discovery errors before a recording exists
produce an error instead. Keep the raw files alongside any generated report
for inspection and replay.

| File | Use |
| --- | --- |
| `benchmark/README.md` | Read the outcome, native care changes, execution rates, and warnings |
| `benchmark/runs.csv` | Compare one row per run in a spreadsheet |
| `benchmark/benchmark.json` | Inspect full distributions, per-call timing/usage, configuration, and source hashes |

Care includes living population, recorded deaths, drinks, stress, needs, hunger,
thirst, injuries, and native brewing-product evidence. Unknown values remain
`null` in JSON, blank in CSV, and `unknown` in Markdown. Speed is reported with
different denominators:

| Measurement | Meaning |
| --- | --- |
| Advance-operation ticks/second | Verified elapsed game ticks divided by advance-call wall time, including bridge polling and pause overhead |
| Game ticks/total wall second | Observed tick span divided by total recorded run time, including model waits and setup |
| Native decoding tokens/second | Provider-reported generation tokens divided by generation time; excludes prompt processing |
| End-to-end completion tokens/second | Reported completion tokens divided by host policy-call time, including loading, queueing, prompt processing, and transport |

Native Ollama supplies generation, prompt, load, and total durations plus token
counts. Compatible adapters may lack generation timings; those rates remain
unknown. Missing calls suppress complete aggregate totals, while known subsets
and per-call values remain in JSON. Rates use ratios of totals, not averages
of individual rates. None measures graphical FPS or establishes better care.

## Export comparisons from existing runs

```sh
python start.py benchmark-report runs/model-a runs/model-b --out reports/comparison
```

This writes `benchmark.json`, `runs.csv`, and `README.md` to a new or empty
directory without contacting the game or models. It recomputes care and speed
from recorded events, verifies the evidence, and reports setup differences or
missing identities instead of declaring every pair comparable.

For a controlled comparison, use the [save capture/restore workflow](../EVALUATION.md#preserve-one-starting-save),
fully exit before restoring the same declared snapshot for each run, then load
it and pass `--starting-snapshot PATH`. That flag records an identity; it does
not restore the game. Sequential runs on a changing fortress are different
starting conditions. The report exporter performs no automatic reset or sweep.

## Repeat a starting site with another model

With `--model`, the repeat workflow changes the model ID and preserves the original connection,
system prompt, response schema, token limits, timeouts, and all experiment
budgets. Ollama context, seed, temperature, thinking, and retention requests also
carry forward. It reads the original run's recorded settings; changes to
`dfeval.local.toml` do not affect the repeat. The default `STOP` file moves to the
new run directory; an explicitly chosen custom stop path remains unchanged.
The installed prompt, schema, and observation version must match the source;
use the matching project version for older recordings.

The capture and prepare examples below use installation-local saves. If the
game stores saves elsewhere, supply the same explicit `--save-root` to both
steps; see [save-location examples](#choose-the-save-root-explicitly).

1. Prepare a fortress, save it, and fully exit Dwarf Fortress. Capture its start:

   ```sh
   python start.py scenario capture --df-path "YOUR_GAME_FOLDER" --save-name region1 --out saves/care-start --game-stopped
   ```

2. Load the captured fortress and leave it paused. Run the first model, declaring
   the checkpoint and any desired settings:

   ```sh
   python start.py benchmark --model "FIRST_MODEL" --starting-snapshot saves/care-start --simulation-fps 1000 --out runs/model-a --watch
   ```

3. After the run, save and fully exit the game. Prepare the next model:

   ```sh
   python start.py repeat prepare --from runs/model-a --model "SECOND_MODEL" --out runs/model-b --game-stopped
   ```

4. Load the restored `region1` fortress and leave it paused. Execute the recipe:

   ```sh
   python start.py repeat run runs/model-b --watch
   ```

Replace the model placeholders with exact IDs available from the **same model
server/provider**. This also supports an original cloud `experiment` run with
its original HTTPS endpoint and credential-variable name. No credentials are
copied into the recipe. Switching providers requires an explicitly configured
new experiment from the same checkpoint, followed by a comparison export.

Preparation verifies the entire checkpoint against the original declared hash,
preserves an existing destination save in a unique directory under
`GAME/dfhack-config/dfeval-backups/`, then restores the starting bytes. Use
`--backup NEW_DIRECTORY` to choose another backup on the same filesystem, or
`--df-path GAME` to override the game folder. It prints the exact backup and
restored-save locations. Existing recipe/output/backup directories are never
overwritten. Loading the game remains an explicit game-menu step.

The new recipe contains `repeat.json` and copies of the source manifest and
initial snapshot. Its checksum detects changes to settings and references; it
does not authenticate an unknown author's files. It stores relative references
where possible. Keep the recipe, original run, checkpoint, and game in the same
relative arrangement when moving them. If the checkpoint moved before
preparation, supply `--starting-snapshot PATH`; its bytes must still match the
original identity. An old run without a declared checkpoint or the required
configuration evidence cannot establish that missing information afterward.
An explicitly selected save root is also retained in the recipe and restore
receipt, using a relative path where possible. Repeat execution checks that the
receipt's target is exactly that root plus the recorded save name.

Before inference, the runner compares the loaded native observation, including
its exact starting tick, save identity, versions, and simulation/graphics caps,
with the source. It also verifies the installed bridge and recorded configuration
fingerprint. A mismatch records an unsuccessful initial-state check, makes zero
model calls, queues no jobs, and advances no ticks. Normal final pause and speed
restoration still apply. UI focus and session-specific measurement bookkeeping
are excluded from the native fingerprint.

| Repeat output | Contents |
| --- | --- |
| `repeat.json`, `source/` | Frozen recipe, save-restore receipt, source hashes, and initial-state evidence |
| `run/` | New manifest, event stream, result, and spectator replay |
| `run/benchmark/` | New policy's care and performance measurements |
| `comparison/` | Paired JSON, CSV, and Markdown report against the original run |

The paired export verifies the original source files against their recorded
hashes. If those files were moved or changed, the individual result remains
available and the CLI reports the paired export as unavailable. Keep each run's
raw evidence to inspect or rebuild comparisons. To repeat again, prepare from
`runs/model-a` with a fresh output, or use `--from runs/model-b/run`.

The environmental fingerprint covers declared installation-local configuration,
init scripts, and selected mod/script files. The manifest lists its exact scope,
missing paths, and limitations. It does not prove the state of external settings
locations, arbitrary active plugins/timers, native settings omitted from the
observation, binary assets, or server-side model configuration. Keep those
conditions controlled and declared when comparing results. Runtime/model digests
can remain unknown; the original model's weight identity is never copied onto
the replacement. Matching saved bytes and recorded conditions does not guarantee
deterministic future simulation or establish a model ranking.

## Compare with an idle or rule control

An explicit control helps separate the model's actions from what this fortress
does while time passes. Start from a completed model recording with a declared
checkpoint. Save and fully close the game, then choose exactly one of `--model`
or `--control` during repeat preparation:

```sh
python start.py repeat prepare --from runs/model-a --control idle --out runs/idle-control --game-stopped
```

Supply the same explicit `--save-root` if the save is outside the installation.
Preparation preserves the finished save and restores the original checkpoint.
Load that restored fortress, leave it paused, and execute the recipe:

```sh
python start.py repeat run runs/idle-control --watch
```

The `idle` control always waits. For a rule control, repeat the save/exit/prepare
and load/run steps with `--control rule --out runs/rule-control`. That policy
requests one normal brewing job when it observes an idle completed still and
otherwise waits. Neither control contacts a model server or uses model tokens.

Controls preserve the source's save, experiment budgets, simulation cap, and
initial-state guard. Their policy identity and model settings naturally differ
from the model run; copied budget ceilings do not imply equal computation or
equal wall time. The report keeps those differences visible. A product receipt
still does not establish consumption or improved care, and this workflow is
not a completed care study or a claim that a model outperforms the controls.

Each recipe gets its own `run/`, `run/benchmark/`, and paired `comparison/`.
To inspect the model and both controls together:

```sh
python start.py benchmark-report runs/model-a runs/idle-control/run runs/rule-control/run --out reports/model-controls
```

Prepare each control from the original model run using a fresh destination.
Control recordings are not accepted as model-source recipes.

## Choose the save root explicitly

The save root is the directory containing save folders such as `region1`.
Without `--save-root`, capture and restore inspect only `GAME/save` and
`GAME/data/save`. They do not search AppData, a home directory, or other game
installations. `--df-path` always identifies the game installation and DFHack;
it does not select an external save directory.

The Windows DF 53.16 installation exercised here stores saves at
`%APPDATA%\Bay 12 Games\Dwarf Fortress\save`. To capture a starting checkpoint
from that location in **PowerShell**, first save and fully exit the game:

```powershell
$dfSaveRoot = Join-Path $env:APPDATA 'Bay 12 Games\Dwarf Fortress\save'
python start.py scenario capture --df-path "YOUR_GAME_FOLDER" --save-root "$dfSaveRoot" --save-name region1 --out saves/care-start --game-stopped
```

Replace `region1` with the actual save folder name. The selected root must
already exist and must be a local directory without symlinks or filesystem
reparse points. If your installation uses another location, supply that path
explicitly. The snapshot's portable manifest contains no save-root path and
keeps the existing snapshot format.

Load the saved fortress, leave it paused, and run the first model with
`--starting-snapshot saves/care-start`. After that run, save and fully exit
again. Prepare the repeat using the external save root:

```powershell
python start.py repeat prepare --from runs/model-a --model "SECOND_MODEL" --out runs/model-b --df-path "YOUR_GAME_FOLDER" --save-root "$dfSaveRoot" --game-stopped
```

The recipe retains this root for `python start.py repeat run runs/model-b`.
There is no save-root override at repeat execution. The first benchmark needs
only its verified starting snapshot; it does not read or restore a save folder.

For a separate explicit restore, the same option selects the destination root:

```powershell
python start.py scenario restore saves/care-start --df-path "YOUR_GAME_FOLDER" --save-root "$dfSaveRoot" --save-name region1 --backup "YOUR_NEW_BACKUP_DIRECTORY" --game-stopped
```

An existing destination requires preservation at a new backup directory on the
same filesystem. Repeat preparation normally chooses a backup under the game
installation; use `--backup` when the external save root is on another volume.
Capture, restore, and repeat preparation check that the game is stopped. They
do not load the fortress afterward.

Compatibility checks between the snapshot/installation and native observation
recognize `53.16` and `v0.53.16 win64 ITCH` as the same game release. They still
reject a different release such as `53.17`. The full native version string and
DFHack version remain unchanged in the initial-state guard and recorded
evidence. This formatting normalization does not establish compatibility with
other game releases or identical unobserved simulation state.

## Current validation

The latest [three-month experiment](native-care-season.md) completed 100,800
ticks under a local Ollama model and both restored controls. The model and rule
issued identical brewing actions on all 12 decisions, each producing 125 drink
units. Its report includes derived data and measured throughput, with care-task
validity and same-action replay variation still unresolved. The shorter checks
below retain their original settings and outcomes.

On 2026-09-05, a Windows DF 53.16 / DFHack 53.16-r1.1 idle-baseline probe requested
a temporary cap of 1,000 and advanced exactly 137 ticks. Final pause and
restoration of the original cap of 100 were confirmed. Its 0.672-second advance
call measured about **204 advance-operation ticks/second**; total run time was
1.359 seconds. This small conformance check includes bridge overhead and is not
a stable hardware benchmark. Seven citizens and 60 drink units remained; it
contained no model inference or brewing action.

A [completed native model run](native-loop-proof.md) now validates the bounded
observation-to-action-to-production loop. Qwen2.5-1.5B-Instruct through llama.cpp
made three accepted brewing decisions and advanced exactly 3,600 ticks. Native
product callbacks linked two of its queued jobs to 50 newly created drink units.
The run ended at its decision limit with pause and restoration of the original
FPS cap confirmed. Seven citizens remained alive; drink stocks increased from
60 to 110. These measurements establish production, not improved wellbeing or
drink consumption.

The original checkpoint was restored for model B, Qwen2.5-0.5B-Instruct, and its
native starting-state guard matched before the first model call. **B failed its
first response:** it repeated an unfinished explanation until the 256-token
limit, returning `finish_reason: length`. The runner accepted no decision,
queued no jobs, and advanced zero ticks. Its 123.469-second run ended with pause
and FPS restoration confirmed. This unsuccessful outcome is retained in the
completed paired report; no replacement trial is substituted.

The report's `recorded_setup_matches` flag is false because it includes B's
terminal error and recorded failure as qualifications. Its listed issues contain
no difference in starting native state, versions, budgets, or recorded
environment. Initial observations differ only in the bridge's session ID, which
is excluded from the native fingerprint; this does not make the model-visible
prompts byte-identical. The repeat workflow also has offline tests for save
preservation, settings reuse, start-state rejection, and paired exports using
fake saves and bridges.

The native Ollama adapter has automated tests and local HTTP transport fixtures;
an actual Ollama 0.33.3 Windows runtime has now passed the synthetic check with
an imported Qwen2.5-1.5B-Instruct Q4_K_M model. Its subsequent `native-care-v1`
trial failed at the declared 512-token response cap before any accepted action
or elapsed tick; the native starting guard matched, and pause and FPS
restoration were confirmed. The [native care pilot](native-care-pilot.md)
records verified asset provenance, CPU residency, predeclared settings, and
the control status. The earlier
[Qwen3 connection checks and failed native attempts](model-connections.md#verified-recipe-qwen3-06b-on-windows-cpu)
remain separate evidence. This small prepared brewing task does not demonstrate
full-game competence, operating-system isolation, or a model ranking.
