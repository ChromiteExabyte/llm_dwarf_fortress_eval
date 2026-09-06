# Connect and inspect the real game

The native path reads real Dwarf Fortress state and exposes a small set of
validated actions. For an offline introduction, run `dfeval demo --open`.
For model episodes and comparisons, use [EVALUATION.md](EVALUATION.md).

## Installation and launch

Install the Python package as described in [README.md](README.md). Obtain the
game and DFHack separately. The locally exercised pair is Dwarf Fortress
**53.16** and [DFHack **53.16-r1.1**](https://github.com/DFHack/dfhack/releases/tag/53.16-r1.1)
on Windows. Other version pairs have not been validated here. Follow the
[DFHack installation instructions](https://docs.dfhack.org/en/stable/docs/Installing.html)
for your game distribution.

The game may live outside this repository. Supply `--df-path`, or set
`DFEVAL_DF_PATH`. An explicit argument takes precedence; the fallback is
`./dwarfFortressItself` in the current working directory. Quote paths containing
spaces. The game's configuration directory and your output directory must be
writable.

```sh
dfeval doctor --df-path "/path/to/Dwarf Fortress"
```

`doctor` reads files only. It reports the game, DFHack support files, usable
host runner, and bridge installation. Missing prerequisites produce exit code
1. File readiness does not prove connectivity or binary compatibility. A
missing bridge script on a fresh installation is expected: `live` and
`experiment` install it before connecting.

On Windows, launch with the game directory as the working directory. The
optional source-checkout helper does this:

```powershell
.\tools\start-game.ps1 -GamePath "D:\Games\Dwarf Fortress"
```

The Windows bootstrap searches `hack/dfhack-run.exe`, then `dfhack-run.exe`.
On Linux it searches executable `dfhack-run`, then `hack/dfhack-run`; launch
DFHack according to its official instructions. Linux native gameplay has not
been validated by this project. Native integration on macOS is unsupported;
offline viewing and other Python tools do not require a native game host.

For a fresh Windows installation without DFHack, the optional
`tools/install-dfhack.ps1 -GamePath "D:\Games\Dwarf Fortress"` helper downloads
the pinned release, checks its hash, and preserves replaced files under
`runs/setup`. It refuses an existing `hack` directory and a running game.
Keep its setup records and backups; it is not a general updater.

## Observe a loaded fortress

Start DFHack, load a fortress, and keep the game window available for inspection.
Then capture a probe into a new or empty directory:

```sh
dfeval live --df-path "/path/to/Dwarf Fortress" --out runs/inspection
dfeval watch --run runs/inspection --open
```

The default `live` invocation observes without intentionally changing pause or
queuing work. An already unpaused game can advance naturally between samples.
Use one controller at a time. Starting a bridge while another advance is active
is rejected rather than taking over that advance.

The installed script is `dfhack-config/scripts/dfeval-live.lua`, with its source
bundled in the Python package. The legacy `dfeval-bridge.lua` and older Python
adapter are not used by these native commands.

## Request a bounded probe

Use a prepared, disposable fortress or preserve its saved baseline first.
This command advances 1,200 simulation ticks and requests a return to pause:

```sh
dfeval live --df-path "/path/to/Dwarf Fortress" --advance-ticks 1200 --out runs/tick-probe
```

The result records requested ticks, actual elapsed ticks, overshoot, and pause
confirmation. A blocking screen or slow/unresponsive game can cause a timeout.
The bridge watchdog attempts to pause; a completely unresponsive process cannot
acknowledge that pause. Inspect the game when the record says it is unconfirmed.

Brewing probes accept `--brew-workshop ID --brew-jobs N`. Obtain the actual ID
from a native snapshot's `workshops` list. The target must be a completed still;
1-10 jobs may be requested subject to its queue limit. Ordinary plants,
containers, workers, and access are still required. No ingredients or drinks
are created by the adapter. Returned job IDs prove queuing, not completion.
Completed native brewing has been verified in the
[local model loop trial](docs/native-loop-proof.md), using reaction callbacks
that link the model's queued jobs to newly created drink items.

For recurring policy decisions, use `experiment` instead of manually repeating
probes. [Its guide](EVALUATION.md) explains the three policy actions and limits.

## Evidence and watching

Each probe prints its output path. Important files include:

| File | Evidence |
| --- | --- |
| `status.json` | Game/DFHack versions, loaded state, clock, pause, UI focus |
| `before.json`, `after.json` | Native citizens, stocks, workshops, jobs, and collection errors |
| `events.jsonl` | Timestamped requests, results, and failures |
| `session.json`, `bridge-script.lua` | Declared session and exact bridge source/hash |
| `startup.json` | Host bootstrap command and output |
| `advance.json`, `brew.json` | Results of explicitly requested actions |
| `result.json` | Compact probe outcome |

The spectator can follow an active log or replay completed evidence. It shows
the timeline, citizen details, raw observations/actions, and evidence downloads.
An unfinished log is not proof that its producer is still running.

Optional browser window capture requires your explicit choice of a window.
It stays in the browser and is independent of the selected recorded observation.
The built-in native demo contains no game frames or video; the viewer does not
invent them.

Stock totals count native stack units and distinguish drink from prepared food.
Candidate-stock exclusions are recorded, but they do not establish path access
or that a particular dwarf drank. Stress, need values, hunger/thirst counters,
and wound counts are raw observations. Missing fields are null with collection
errors where available; they are not substituted with healthy defaults.

Raw records may contain local paths and transcripts. Keep them local by default
and review any excerpt before publication.

## Native verification records: 2026-09-05

### Earlier observation and advance probe

With `v0.53.16 win64 ITCH` and DFHack `53.16-r1.1`, a loaded fortress reported
seven living citizens and 60 drink stack units. The absolute clock advanced
from **20,176,801** to **20,178,001**: exactly **1,200 ticks**, with zero reported
overshoot. The final native snapshot and advance response confirmed pause.
The citizen roster and drink count were unchanged across these two samples.

`dfeval demo --open` exposes the reviewed recording, including native values,
original sanitized events, and source-file SHA256s. The probe establishes
observation and bounded advancement on this version pair. It does not establish
an autonomous model episode, improved wellbeing, brewing completion, or a
repeatable comparison after save restoration. Those are outside this earlier
probe's evidence.

### Autonomous brewing and restored starting state

A later local Qwen2.5-1.5B-Instruct episode selected three brewing actions and
advanced exactly **3,600 ticks** on a declared, operator-prepared fixture. Two
queued jobs completed and produced **50 new drink stack units**, confirmed by
native reaction-product callbacks. The episode confirmed final pause and
restoration of the original simulation FPS cap.

The saved checkpoint was subsequently restored for Qwen2.5-0.5B-Instruct. Its
starting-state guard matched before inference. Its response repeated the public
reason until it reached the 256-token limit (`finish_reason: length`), so the
runner ended with an error and zero accepted decisions, actions, or elapsed
ticks. Final pause and FPS-cap restoration were confirmed. The exported paired
report is qualified by that model error, not a starting-state mismatch. These
observations demonstrate a bounded model-to-game production loop; they do not
establish improved dwarf wellbeing or general fortress-management ability. See the
[native loop proof](docs/native-loop-proof.md) for configuration and evidence.
