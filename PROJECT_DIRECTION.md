# A Dwarf Fortress benchmark people can watch and audit

**Give an LLM a real fortress, ask it to care for the dwarves, and let people
watch how well it does.**

The project owner's clarification on 2026-09-05 defines accessibility as easy
evaluation: people should be able to understand the experiment, run it, watch
it, and inspect its evidence. Gameplay competence and dwarf wellbeing belong
in the same account of what happened.

## The experience

A spectator should be able to answer:

- What did the model see, and which actions were available?
- What did it choose, and did the game accept and complete the work?
- How did the individual dwarves, their needs, and their resources change?
- Which limits, failures, human interventions, and unknown measurements qualify
  the result?

The local viewer now displays recorded native observations, actions, citizen
histories, and evidence downloads. It can follow an active log or replay a
finished recording. Optional user-selected game-window video stays in the
browser and is separate from the telemetry timeline. The default packaged demo
contains an actual local model's public brewing decisions, raw citizen
measurements, and native product receipts. It includes source hashes and
sanitization notes, with no invented game footage. The earlier native probe
remains available through `demo --recording probe`.

A public explanation is evidence of what the model said. It is not access to
hidden reasoning. A nearby action and measurement change do not by themselves
establish cause and effect.

## The bounded experiment

The current native runner gives a policy a fixed care briefing, the deterministic
`native-care-v1` projection of fortress observations, public decision history,
and finite budgets. Full raw snapshots remain in the audit logs. Its model-facing
choices are wait, brew, and finish. The host validates arguments, controls tick
advancement, records requests/results, and attempts pause on exit. Ordinary
dwarf labor and resource constraints determine whether brewing succeeds.

The software supports explicitly chosen local/cloud compatible model endpoints
and labeled idle/rule baselines. It does not fall back to a mock or another
model after an error. The host owns snapshots, provider connections, and logs;
the model's decision schema accepts no arbitrary commands, code, host paths,
or browsing. Operating-system isolation of the host and game remains unbuilt,
so this is not yet a secure black-box sandbox.

This first interface concerns an existing prepared fortress. It does not claim
autonomous world generation, embark, construction, or full-game mastery.
Expanding the action space should follow demonstrated, inspectable success
with the smaller experiment.

## Measurement commitments

Native care reports preserve raw stress, need levels/focus, hunger and thirst
timers, wound counts, stock units, and observed deaths. Unknowns remain unknown.
Individual histories remain available alongside aggregate distributions.
Stocks provide context; they do not prove that every dwarf can reach a drink.
A missing citizen is not automatically a death, and a queued job is not a
completed job. Population follows the native living-citizen roster; an unknown
death flag is not rewritten as false. The runner stops on an explicitly empty
roster or one containing only confirmed dead records, without interpreting
unknown data as extinction.

The native path produces no composite wellbeing score or automatic ranking.
Comparisons recompute outcomes from saved snapshots and check declared starting
identity, initial observations, bridge/protocol and scenario identities, versions,
budgets, and record consistency. Changes across snapshots are withheld when
world/time continuity is unverified. A snapshot hash is an
operator declaration, not proof that the current in-memory game was freshly
restored. Repeated runs across several starting saves are needed before making
comparative claims; a common seed is not proof of deterministic native replay.

The objective is care in a game. Broader claims about real-world benevolence
would require other evidence. Tool failure should not be relabeled as moral
indifference.

## Implementation and validation are separate

| Piece | Current state |
| --- | --- |
| Native adapter | Bounded observations, pause, tick advancement, and brewing orders implemented |
| Policy runner | Explicit local/cloud model and idle/rule policies, budgets, stop handling, and event records implemented |
| Spectator | Local read-only viewing, replay, raw inspection, and optional browser window capture implemented |
| Starting saves | Capture, complete inventory/hash verification, backup/restore, and process checks implemented; repeat recipes freeze run/model settings and check the loaded start |
| Comparison | Native summaries recomputed from events; recorded setup differences reported |
| Local models and performance | Native Ollama and compatible local/cloud connections; recorded care, game throughput, and model timing exported as JSON, CSV, and Markdown |
| Public demo | Actual local-model brewing episode with public decisions, native product receipts, raw citizen data, source hashes, and declared sanitization; earlier probe remains optional |

Local automated tests exercise these components with fixtures and fake bridges.
The [native model evidence](docs/native-loop-proof.md) on 2026-09-05 now includes
a complete autonomous episode in the bounded brewing interface:
`Qwen2.5-1.5B-Instruct` through llama.cpp made three accepted brewing decisions in Windows
Dwarf Fortress 53.16 with DFHack 53.16-r1.1. The game advanced exactly 3,600 ticks;
native product callbacks linked two model-requested jobs to 50 newly created
drink units. Final pause and restoration of the original simulation cap were
confirmed. Seven citizens remained alive and drink stocks rose from 60 to 110.
Production is established; improved wellbeing and drink consumption are not.

The checkpoint was restored for model B, Qwen2.5-0.5B-Instruct, and its
starting-state guard matched before the first model call. B then repeated an
unfinished explanation until its 256-token response limit. The runner rejected
that response, accepted no decision, queued no jobs, and advanced zero ticks.
Final pause and FPS restoration were confirmed. B's failed outcome is preserved
in the completed paired report. The report flags its terminal error and recorded
failure; it reports no changed starting state, versions, budgets, or recorded
environment. The initial observations differ only in session bookkeeping.

This is a prepared task with three allowed actions, not evidence of full-game
mastery or a general model ranking. Installed Ollama integration remains
unverified; the native model pair used llama.cpp.

Linux runner support and checks are implemented, but native Linux gameplay
has not been validated. Current native macOS integration is unsupported.
Configured cross-platform CI is not evidence that those live validations occurred.

## Next acceptance milestone

The action-to-native-production loop is demonstrated. The next milestone is to
evaluate care over longer episodes with controls and several preserved starting
saves, before expanding the action set or making comparative model claims:

1. Retain and inspect complete successful and failed trials. Model B's token-limit
   failure remains part of the initial paired evidence and its limitations.
2. Run models and labeled idle/rule controls from restored checkpoints under
   the same budgets, repeating across several starting saves.
3. Track individual needs, stress, hunger/thirst counters, resources, and deaths
   over longer horizons. Keep native production separate from evidence that
   dwarves actually consumed drinks or that their measured condition improved.
4. Publish reviewed evidence and limitations, including human preparation and
   settings outside the recorded environment, before drawing broader conclusions.

These additional care evaluations remain to be performed. The setup procedures
are in [EVALUATION.md](EVALUATION.md); the completed brewing episode and failed
repeat are described in [the native loop record](docs/native-loop-proof.md).

## Earlier prototype

The original prototype used a custom colony simulator, authored dilemmas, and
philosophical scoring. Its legacy DFHack adapter exposed unrestricted commands
and left several wellbeing fields at defaults. Those paths are not the native
experiment described here. The `run`, `baselines`, `sweep`, and `score` commands
remain explicitly labeled mock tools; their results concern that simulator.
The historical discussion remains in [PHILOSOPHY.md](PHILOSOPHY.md).

Retain useful logging and test infrastructure, but judge progress against the
owner's intended outcome: a real game, a bounded model, dwarf care, and evidence
that other people can readily inspect.
