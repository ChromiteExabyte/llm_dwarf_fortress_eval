# Dwarf Fortress Care Eval

**Give an LLM a real fortress, ask it to care for the dwarves, and make the experiment easy to watch and inspect.**

The project provides a bounded game interface, local/cloud model connections,
care and performance reports, save snapshots, and a spectator with replay.
The AI defines what care means within the sandbox through its stated priorities
and actions. People inspect that interpretation, its choices, and what happens
to the dwarves. Game measurements describe consequences; they are not a
prescribed or hidden care rubric. The model's public notes are evidence to
examine, not self-grading or permission to rewrite records or change the rules.

**Status: a working native brewing harness; broader game agency is unfinished.**
Models can wait, queue brewing, or finish in a prepared fortress. General
fortress management remains unfinished. These three actions leave limited room
for a model to express its interpretation of care through meaningful choices.

In the latest [three-month experiment](docs/native-care-season.md), a local
Qwen2.5-1.5B model through Ollama and two restored controls each completed
100,800 native ticks. The model and brewing rule chose identical actions on
all 12 decisions and each produced 125 drink units. All seven dwarves survived
in every run; no model care advantage was demonstrated. The report includes
a chart, downloadable measurements, runtime costs, and unresolved questions.
The [next milestone](PROJECT_DIRECTION.md#next-acceptance-milestone) is broader
ordinary game agency with inspectable choices, consequences, and repeatability.

**Inspect it first:** with Python 3.11+, run `python start.py demo --open` from
the source folder. This opens the recorded model episode locally; no game,
model server, API key, or package installation is needed.

## Run a local benchmark

Have Python **3.11 or newer**, a running [Ollama](https://ollama.com/download)
server with a downloaded local model, and Dwarf Fortress with matching DFHack
installed separately. Load a prepared fortress with a completed still, brewable
plants, containers, and workers; see [game setup](LIVE_GAME.md). World creation
and embark are outside the current model interface.

From a clone or unpacked source release:

```sh
python start.py
```

Use `py -3 start.py` on Windows or `python3 start.py` where appropriate. The
launcher runs directly from the source with Python's standard library: no pip
installation or virtual environment is required. It guides the first selection
of a local runtime, installed model, and game folder, saves those choices in
the ignored `dfeval.local.toml`, then starts the benchmark. The game, GPU drivers,
model runtime, and model weights are not bundled or installed by the launcher.

To configure first and load the game afterward, run `python start.py setup`.
Subsequent runs reuse that selection. These commands also work independently:

```sh
python start.py models
python start.py benchmark --simulation-fps 1000 --watch
```

`benchmark` lets the model **wait**, **queue brewing**, or **finish**. The host
validates each decision and advances a bounded interval after wait/brew. Its
defaults allow 12 decisions and up to 14,400 game ticks. The optional FPS flag
requests a temporary simulation cap; actual throughput is measured, and cleanup
attempts to restore the original cap. It does not overclock hardware.

Benchmarks with at least one native snapshot export three files under the run's
`benchmark/` folder. Earlier failures retain their raw records and report export
as unavailable; no care measurements are invented.

| File | Contents |
| --- | --- |
| `README.md` | Readable care outcomes, speed measurements, failures, and limitations |
| `runs.csv` | One row per run for spreadsheet analysis |
| `benchmark.json` | Full measurements, configuration, per-call telemetry, and evidence hashes |

See [benchmarking and performance](docs/benchmarking.md) for limits, measured
game ticks/second and model tokens/second, and comparison exports. The
[model connection guide](docs/model-connections.md) covers native Ollama,
LM Studio, llama.cpp, and explicit cloud connections. To check Ollama without
touching the game, use `python start.py model-check --provider ollama --model "YOUR_INSTALLED_MODEL" --timeout 300`.

The optional spectator is read-only. Its window-video picker requires your
explicit browser selection and keeps capture local; video is separate from
native telemetry. Finished runs remain inspectable without a model server.

New runs send the versioned `native-care-v1` observation: deterministic JSON
with compact tables for repeated needs and stock items. Raw native snapshots
stay in the logs for inspection and care reports. No citizen or product rows
are truncated; oversized input stops before the model call. See the
[observation contract](docs/benchmarking.md#what-the-policy-observes).

## Repeat the same site with another model

Capture a starting checkpoint **before the first run**, while the game is fully
saved and closed:

```sh
python start.py scenario capture --df-path "YOUR_GAME_FOLDER" --save-name region1 --out saves/care-start --game-stopped
```

Capture and repeat preparation default to `GAME/save` or `GAME/data/save`.
For Windows saves under AppData, add
`--save-root "$env:APPDATA/Bay 12 Games/Dwarf Fortress/save"` to both commands
in PowerShell. Use the directory that contains `region1`, not `region1` itself.
See [explicit save locations](docs/benchmarking.md#choose-the-save-root-explicitly)
for complete examples; external save folders are never selected automatically.

Load that fortress and leave it paused, then run the first model with
`python start.py benchmark --starting-snapshot saves/care-start --out runs/model-a`.
Afterward, save and close the game, then prepare the next model:

```sh
python start.py repeat prepare --from runs/model-a --model "SECOND_MODEL" --out runs/model-b --game-stopped
```

This preserves the finished fortress in a new backup, restores the original
checkpoint, and copies the recorded run and model request settings. Load the
restored fortress, leave it paused, and start the repeat:

```sh
python start.py repeat run runs/model-b --watch
```

The first benchmark needs `--starting-snapshot`; repeat execution uses the saved
recipe. Neither command needs a save-root option.

The runner checks the initial native state, game/bridge versions, and recorded
configuration fingerprint before calling the new model. Results go under
`runs/model-b/run/`; the paired care and speed report goes under
`runs/model-b/comparison/`. Model changes use the original local or cloud
connection. A checkpoint missing from the first run cannot be recreated afterward.
See [the repeat workflow](docs/benchmarking.md#repeat-a-starting-site-with-another-model)
for portability, checks, and the limits of reproducibility.

To compare with a scripted control, replace `--model` with `--control idle` or
`--control rule` during preparation. The control restores the same checkpoint,
keeps the experiment budgets and starting-state check, and makes no model calls.
`idle` always waits; `rule` requests one job at an idle completed still. Use a
new output directory for each control; see [control comparisons](docs/benchmarking.md#compare-with-an-idle-or-rule-control).
Controls test mechanics and provide alternative behavior for comparison; they
do not define the correct meaning of care.

## Inspect the recorded demo

With Python 3.11+, this source command needs no package installation, model
runtime, game, or API key:

```sh
python start.py demo --open
```

The default viewer contains the **actual Qwen2.5-1.5B model recording**: three
brewing decisions, two native drink products totaling 50 units, seven living
citizens, and exactly 3,600 elapsed ticks. Inspect each dwarf, original public
decision, and product receipt. Raw stress and needs show why production alone
does not establish improved wellbeing.

The excerpt includes source hashes and sanitization notes, with private paths,
endpoints, and identifiers removed. It makes no game or model calls and contains
no screenshots or video. Press Ctrl+C to stop the viewer. Use
`python start.py demo --recording probe --open` for the earlier 1,200-tick probe,
or `python start.py demo --out runs/offline-demo` to export the default recording.

For a normal package installation, `python -m pip install .` provides the
equivalent `dfeval` commands: `dfeval setup`, `dfeval benchmark`, and
`dfeval demo --open`. An environment's Python followed by `-m dfeval` also works.
[Development instructions](CONTRIBUTING.md) cover isolated environments and tests.

## Choose the right command

| Command | Purpose |
| --- | --- |
| `models`, `setup` | Discover a running local server's models and save your selection |
| `benchmark` | Run the selected local model and export care plus performance reports |
| `repeat prepare/run` | Restore a recorded starting site for another model or an explicit idle/rule control |
| `benchmark-report` | Recompute JSON, CSV, and Markdown reports from recorded native runs |
| `demo`, `watch` | Inspect recorded native evidence without contacting the game or a model |
| `doctor` | Inspect installation files without connecting to the game |
| `model-check` | Make one synthetic local/cloud model call without contacting the game |
| `live` | Capture native observations; optional explicit tick/brewing probes |
| `experiment` | Run a bounded local/cloud model or scripted policy on a loaded fortress |
| `scenario capture/verify/restore` | Preserve and verify local starting saves while the game is stopped |
| `compare` | Recompute native outcomes and check recorded starting conditions and limits |
| `run`, `baselines`, `sweep`, `score` | Use the earlier **custom mock simulator**, not Dwarf Fortress |

Native care reports retain stress, needs, hunger/thirst timers, injuries, deaths,
and stocks in their recorded units. Unknown measurements remain unknown. There
is no composite wellbeing score or automatic model ranking.

## What has been verified

The [native model loop validation](docs/native-loop-proof.md) used two pinned
Qwen2.5 models on the same checkpoint and settings. Model A made three brewing
decisions, advanced exactly 3,600 ticks, and produced 50 drinks through native
jobs. Model B's starting-state check matched before inference; its incomplete
response was rejected with zero game ticks. Both runs confirmed final pause
and FPS restoration. The paired report retains B's failure and A's worsening
stress/need measurements alongside its increased drink supply.

On **2026-09-05**, Dwarf Fortress **53.16** with DFHack **53.16-r1.1** on Windows
returned real citizen and stock observations. A 1,200-tick advance completed
with zero reported overshoot and the game paused. The optional
`demo --recording probe` preserves that probe's measurements and source-file
hashes, with private paths and session identifiers removed. That earlier probe
does not establish improved wellbeing or completed brewing.

A separate idle-baseline probe requested a simulation cap of 1,000, advanced
exactly 137 ticks, and confirmed both final pause and restoration of the original
cap of 100. Its advance operation measured about 204 ticks/second including
bridge overhead. This short conformance check is not a sustained hardware or
model benchmark; see [the recorded performance check](docs/benchmarking.md#current-validation).

Also on **2026-09-05**, a real local **Qwen3-0.6B-Q8_0** connection check passed
on Windows CPU with **llama.cpp b10809**, an explicit ChatML template override,
and JSON Schema output. It returned one valid decision from synthetic input;
no game was accessed and no action was executed. The
[verified connection recipe](docs/model-connections.md#verified-recipe-qwen3-06b-on-windows-cpu)
records the exact settings, runtime/model hashes, and preceding failed checks.
This is connection evidence, not a model gameplay or care result.

Two subsequent native-observation checks reached a policy timeout and rejected
an invalid model response, respectively. Both observed seven citizens and
60 drink stack units, executed no accepted model action, advanced zero ticks,
and confirmed the final pause. These earlier checks demonstrate stopping and
rejection paths; the later successful brewing loop is linked above. See
the [native check evidence and timing limits](docs/model-connections.md#native-observation-checks-no-accepted-model-action).

| Platform | Current evidence |
| --- | --- |
| Windows | Offline CI passed; autonomous native brewing, restored controls, and recorded model failures exercised locally |
| Linux | Offline CI passed; native runner discovery/process checks tested with fixtures; live game validation pending |
| macOS | Offline CI passed; current native game integration unsupported |

All seven jobs in the [verified cross-platform CI run](https://github.com/ChromiteExabyte/llm_dwarf_fortress_eval/actions/runs/34004152116)
passed tests, builds, distribution inspection, and clean-install checks.
These offline checks do not exercise native gameplay on those systems.
The bounded interface is an application boundary; operating-system isolation
of the game and host has not been implemented.

An installed **Ollama 0.33.3** runtime passed the synthetic connection check.
In the earlier native care pilot, the model hit its 512-token response limit;
the runner rejected it with zero accepted actions. Both restored controls
completed 33,600 ticks, and the brewing rule produced 100 drink units. All three
initial native guards and first projected input hashes matched. See the
[Ollama care pilot](docs/native-care-pilot.md) for outcomes and limitations.
The historical proof and packaged recording predate `native-care-v1`; they keep
their original inputs. That pilot's incomplete model horizon prevented a full
comparison. The later [three-month experiment](docs/native-care-season.md)
completed every policy's horizon with larger declared limits, while exposing
identical model/rule actions, unexplained job outcomes, and unresolved native
repeatability. The narrow action set limits what these runs reveal about a
model's interpretation of care. Historical recordings and repeat recipes retain
their original briefing; they are not retroactively new experiments.

## Read further

- [Latest experiment: three months, a local model, and two controls](docs/native-care-season.md)
- [Evaluation: model choices, budgets, starting saves, and comparisons](EVALUATION.md)
- [Benchmark reports and performance settings](docs/benchmarking.md)
- [Live game installation, probes, and evidence](LIVE_GAME.md)
- [Project direction and remaining validation](PROJECT_DIRECTION.md)
- [Development and publication checks](CONTRIBUTING.md)
- [Earlier mock research notes](PHILOSOPHY.md)

## License and local data

Code and documentation are **GPL-3.0-or-later**; see [LICENSE](LICENSE) and
[third-party notices](THIRD_PARTY_NOTICES.md). Dwarf Fortress and DFHack retain
their own licenses and are not bundled. This is an independent project.

Keep game installations, saves, credentials, and raw run records out of source
releases. The included demo is a deliberately reviewed excerpt. Raw local
evidence can contain machine paths, prompts, and model responses; review it
before sharing. The test suite requires no game, model API, or paid calls.
