# Let an LLM learn Dwarf Fortress

Install the game and DFHack. Give a model a small budget, an empty workspace,
and this objective:

> Learn Dwarf Fortress. Care for the dwarves.

Then see what it does. No supplied build order, care score, brewing assignment,
mandatory diary, or prebuilt team of agents.

**Fresh runs start blank.** Only the objective, tool instructions and resource
limits are supplied. Its pretrained knowledge remains; we cannot erase that.
No previous run's notes are imported.

## Start here

You need Python 3.11 or newer. You do **not** need to understand the source
code, install Python packages, or run the tests.

1. Install [Dwarf Fortress](https://www.bay12games.com/dwarves/) and
   [DFHack](https://docs.dfhack.org/en/stable/docs/Installing.html).
   Their game versions must match. DFHack offers Steam and manual installation.
2. Start DF with DFHack. Create a world, embark, and pause. Use an ordinary
   new embark, with **no prepared brewery or supplied build order**.
   Pick a fortress you are happy to let it experiment in.
3. Open a terminal in this project folder and run:

   ```sh
   python start.py setup
   ```

   On Windows, `py -3 start.py setup` also works. Setup asks for the game folder
   and model connection, and makes no model calls. For local inference, run your
   model server first. For cloud, you supply the provider's model, API URL,
   and current token prices. [Connection help](docs/play.md).
4. With your fortress loaded, run:

   ```sh
   python start.py play
   ```

Watch the actual game window. The terminal shows model calls and spending.
**Ctrl+C stops the run** and attempts to pause the game.

## The ten-dollar experiment

Cloud runs default to a **$10 local spending guard** at the prices you entered.
Before each call, the runner reserves a conservative input allowance and the
full output allowance, then adjusts using reported usage. It stops when the
next call cannot fit, or cloud usage is missing or unexpected. No automatic
API retries or fallback models spend extra money.

This is an estimate, not a provider-enforced billing cap. Incorrect rates,
extra provider fees and provider behavior can defeat the estimate. Use an
independent provider hard cap or limited prepaid balance too. Local inference
makes no paid API calls. Defaults also stop after one hour or 256 model calls;
`play --help` shows the other limits. Each new run has a new budget.

## What you get

Each run prints its folder under `runs/`:

| File or folder | Contents |
| --- | --- |
| `workspace/` | The model's own notes and reusable JSON routines; initially empty |
| `README.md` | Why it stopped, calls, spending and whether pause was confirmed |
| `events.jsonl` | Exact model requests, responses, tool calls and results |
| `result.json` | The final result as data |

Run `python start.py inspect runs/YOUR-RUN` to see its result and files.
The model can reset its own conversation and reread its memories. It may
request additional contexts from the same model using the same budget;
no helper identities or organization are supplied.

Learning means changes in behavior and memory during play, not retraining
model weights. Fresh runs do not inherit experience from earlier runs.

## Current limits

This is an early playable scaffold, **not demonstrated full-game autonomy**.
You still handle world creation and embark. The new exploration controls need
validation in a disposable live fortress; automated checks use fake game data.
The historical live results established only a narrow brewing loop.

The workspace is constrained through validated file and game tools. The model
can author and run bounded JSON routines. It cannot execute arbitrary shell or
Python code, access host files, or browse the web. **An OS sandbox for arbitrary
generated programs is not implemented.**

Watch the game and inspect the new log; it does not use the old spectator
format. Automatic save restoration and cross-run memory continuation remain
unimplemented.

## Keep it simple

`python start.py` shows the small menu. `setup`, `play`, and `inspect` are the
main path. `doctor` checks installation files; `models` lists local models.
Older mock simulations, brewing benchmarks, comparisons and recordings live
behind `python start.py legacy ...`.

[Setup help](docs/play.md) · [Direction](PROJECT_DIRECTION.md) ·
[Development](CONTRIBUTING.md) · [Historical work](docs/legacy-guide.md)

Code is GPL-3.0-or-later. DF and DFHack are separate installations with their
own licenses. Keep credentials, game files, saves and private run logs local.
