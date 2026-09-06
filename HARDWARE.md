# Hardware and run budgets

The Python runner, spectator, and mock harness need Python 3.11 or newer and no
third-party runtime packages.
The real-game path also needs a supported Dwarf Fortress installation and
matching DFHack. See [LIVE_GAME.md](LIVE_GAME.md) for platform support.

There is no measured minimum hardware requirement for this harness's live
benchmark yet. Use the game's own requirements, then measure the actual
starting fortress on the machine that will run the experiment. World size,
population, pathfinding, graphics settings, and background activity can affect
elapsed wall time. Do not infer benchmark throughput from a different save.

## Measure before choosing a run horizon

Once tick advancement is verified in a loaded fortress, record requested and
actual elapsed game ticks, wall time, errors, and pause state for short trials.
Choose a horizon and timeout from those observations. A fixed wall-clock sleep
does not establish how much simulated time elapsed.

Keep the native game window available for inspection. Any setting that changes
the simulation belongs in the run configuration and must be shared across
comparison runs. Human intervention should be recorded.

## Model and storage budgets

The live probe and recorded demo make no model calls. Native `experiment` runs
enforce finite call, reserved output-token, response-byte, game-tick, and wall-time
limits. See [EVALUATION.md](EVALUATION.md) for their defaults and exact meaning.
Missing provider usage is recorded as unknown. A local model server has its own
memory and compute requirements; those depend on the selected model and have
not been measured by this project. The optional mock Claude adapter's
cost calculation is a rough, incomplete estimate; provider billing is the
source for actual charges.

Raw snapshots and event logs accumulate under `runs`. Measure storage per
trial before starting long runs. These files can contain local paths and model
transcripts; review and prepare any evidence deliberately before publishing it.

Save capture/verification/restore tools are implemented and tested with fixtures;
controlled native reset comparisons remain unvalidated. Overnight operation and
automatic crash recovery for full episodes are not validated capabilities.
