# A Dwarf Fortress benchmark people can watch and audit

**Give an LLM a real fortress, ask it to care for the dwarves, and let people
watch what it makes of that responsibility.**

The project owner's clarification on 2026-09-05 defines accessibility as easy
evaluation: people should be able to understand the experiment, run it, watch
it, and inspect its evidence.

The owner's clarification on **2026-09-08** is that **the AI defines care within
the sandbox**. It states its priorities, chooses how to act, and can revise its
interpretation in its public notes. People inspect those choices and their
consequences. The project does not supply a prescribed or hidden care rubric.
Defining care does not make the model its own grader or authorize it to rewrite
evidence, change the interface rules, or leave the sandbox.

**Current status: a working native brewing harness; broader game agency is
unfinished.** The model-to-game loop and inspectable evidence are demonstrated.
The three-action interface leaves limited room for different interpretations of
care to lead to meaningfully different choices.

## The experience

A spectator should be able to answer:

- What did the model see, and which actions were available?
- What did it say care meant, and how did its priorities develop?
- What did it choose, and did the game accept and complete the work?
- How did the individual dwarves, their needs, and their resources change?
- Which limits, failures, human interventions, and unknown measurements qualify
  the result?

The local viewer now displays recorded native observations, actions, citizen
histories, and evidence downloads. Its public journal puts each original
notebook entry and action reason in the timeline. The experiment briefing
shows the exact saved system prompt and budgets; absent or conflicting records
are reported instead of replaced with today's defaults. It can follow an active log or replay a
finished recording. Optional user-selected game-window video stays in the
browser and is separate from the telemetry timeline. The default packaged demo
contains an actual local model's public brewing decisions, raw citizen
measurements, and native product receipts. It includes source hashes and
sanitization notes, with no invented game footage. The earlier native probe
remains available through `demo --recording probe`.

A public explanation is evidence of what the model said. It is not access to
hidden reasoning. A nearby action and measurement change do not by themselves
establish cause and effect.

## A portable application people can inspect

The application has two entry points: the local launcher runs an experiment,
and the browser spectator lets someone watch or replay it. A completed or
partial native recording can also be exported as one standalone HTML file.
That file carries the viewer, public journal, native observations, recorded
briefing, and original evidence downloads. It works without an installation
or a running server on the viewing computer. Video is separate and omitted.

This makes the portfolio artifact an inspectable experiment, rather than only
a screenshot of a final number. A reader can stop at a decision, read what the
model said, inspect what it had observed, and follow the later game records.
Earlier replay positions do not automatically reveal later notes. The viewer
does not infer a philosophy for the model or turn its prose into a care grade.

The next app work should reduce setup friction while keeping the run's start,
model, settings, and evidence visible. A desktop wrapper is optional packaging;
it does not replace game agency, reliable measurement, or repeatable starts.
Game binaries, DFHack, and model weights keep their own licenses and remain
separate from this GPL project. Exporting raw evidence is a local operation,
not an automatic publication or privacy scrub.

## The bounded experiment

The native runner gives a policy a recorded briefing, the deterministic
`native-care-v1` projection of fortress observations, public decision history,
and finite budgets. For new runs, the default care briefing asks the model to
define its interpretation in the existing public notebook and act within the
interface. Recorded repeats retain their original briefing; historical episodes
are not relabeled as runs of a new prompt. Full raw snapshots remain in the audit
logs. Model-facing choices are wait, brew, and finish. The host validates
arguments, controls tick advancement, records requests/results, and attempts
pause on exit. Ordinary dwarf labor and resource constraints determine whether
brewing succeeds.

The software supports explicitly chosen local/cloud compatible model endpoints
and labeled idle/rule baselines. It does not fall back to a mock or another
model after an error. The host owns snapshots, provider connections, and logs;
the model's decision schema accepts no arbitrary commands, code, host paths,
or browsing. Operating-system isolation of the host and game remains unbuilt,
so this is not yet a secure black-box sandbox.

Controls test mechanics and show alternative behavior under recorded conditions.
They do not establish what care should mean or prescribe a correct model choice.

This first interface concerns an existing prepared fortress. It does not claim
autonomous world generation, embark, construction, or full-game mastery.
The next interface work expands ordinary game agency while preserving bounded
actions and inspectable consequences.

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

These measurements describe the game; they do not define care. The native path
produces no composite wellbeing score or automatic ranking. Model statements
and native evidence remain separate so people can examine the relationship
between stated priorities, decisions, and consequences.
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
| Spectator | Local read-only viewing, chronological public journal, exact recorded briefing, replay, raw inspection, and optional browser window capture implemented |
| Standalone recording | Single HTML export with embedded original evidence and source hashes; no viewing-side installation or server; video omitted |
| Starting saves | Capture, complete inventory/hash verification, backup/restore, and process checks implemented; repeat recipes freeze run/model settings and check the loaded start |
| Comparison | Native summaries recomputed from events; recorded setup differences reported |
| Local models and performance | Native Ollama and compatible local/cloud connections; recorded care, game throughput, and model timing exported as JSON, CSV, and Markdown |
| Public demo | Actual local-model brewing episode with public decisions, native product receipts, raw citizen data, source hashes, and declared sanitization; earlier probe remains optional |
| Model-defined care | Public notes and actions are recorded; broader ordinary game agency is needed to observe more varied choices and interpretations |
| Native repeatability | Matching recorded starts and exact tick boundaries demonstrated; identical model/rule action sequences produced different care trajectories, with no reliable variability estimate yet |
| Job outcomes | Queue acceptance and native product callbacks recorded; direct cancellation causes and complete job lifecycle evidence remain unbuilt |

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

The later [native care season](docs/native-care-season.md) exercised Ollama with
Qwen2.5-1.5B and separate idle/rule controls from the verified checkpoint. All
three completed 100,800 native ticks with confirmed final pause and simulation
cap restoration. The model made 12 accepted brewing decisions; five jobs had
native drink products totaling 125 units. The rule control issued the same
actions and also produced 125 units; idle produced none. Seven citizens remained
alive in every run. Care measurements were retained, but this does not establish
better care by the model.

The model repeated an incorrect ingredient description, while the native queue
and product receipts identified plant brewing. Matching action sequences and
tick boundaries still led to different model/rule care trajectories. This is an
observed repeatability gap, not a demonstrated cause or a measured noise floor.
The 32,768-token context setting was capacity: the largest reported prompt was
9,106 tokens, and every prior accepted decision was retained. Neither successful
execution nor a larger context allocation establishes that decisions respond to
relevant changes in the game.

These remain prepared tasks with wait, brew, and finish as the only actions.
Autonomous embark, construction, resource management beyond brewing, and
full-game mastery have not been demonstrated.

Linux runner support and checks are implemented, but native Linux gameplay
has not been validated. Current native macOS integration is unsupported.
Configured cross-platform CI is not evidence that those live validations occurred.

## Next acceptance milestone

The next milestone gives the model more meaningful ordinary game choices and
makes its interpretation of care inspectable. Mechanical reliability and replay
repeatability are engineering requirements, independent of any preferred answer
to what care means. A model does not need to pass a prescribed care endpoint or
outperform a scripted care policy before its behavior can be studied.

The following work is planned, not implemented or completed:

1. **Expand ordinary game agency.** Add bounded actions beyond brewing and
   waiting, with native argument validation and evidence of their effects.
   Document the available choices and constraints without prescribing which
   interpretation of care the model should pursue.
2. **Inspect interpretations across situations.** Preserve public priorities,
   decisions, and native consequences as circumstances change. Examine whether
   actions follow the model's stated interpretation and whether its claims
   match the evidence, without specifying acceptable care answers in advance.
   Keep repetition, invalid outputs, and early failures as outcomes; do not
   replace them with hidden retries or tune away an observed failure.
3. **Measure repeatability and locate first divergence.** Replay identical
   action traces across repeated restores without model inference. Separately
   compare short and long waits while paused. Record queue insertion tick/frame
   boundaries and compare native observations at the first divergence. Existing
   exact advance boundaries do not show that actions were issued a few game
   ticks late; the mechanism remains to be tested. One divergent pair is not
   a reliable estimate of ordinary variation.
4. **Record direct job lifecycle evidence.** Preserve distinct records for a
   valid model decision, native queue acceptance, product creation, and native
   cancellation/completion where directly observable. Include job IDs, native
   timestamps and cancellation reasons when available. A vanished job cannot
   supply its own explanation, and acceptance does not guarantee completion.
5. **Measure performance improvements offline.** Replay recorded model requests
   to measure prompt processing, cache reuse and decoding separately before
   changing the prompt or cache strategy. Preserve the input/version contract
   and report changed settings and actual usage; allocated context is not
   processed tokens. Keep performance changes distinct from changes in agency
   or the care briefing.
6. **Compare models from preserved sites.** Repeat models and controls under
   recorded prompts, settings, and budgets, retaining all outcomes. Publish
   differences in interpretation, behavior, and consequences for people to
   inspect; a model's own account does not settle the evaluation.

Publish reviewed evidence, human preparation and remaining unknowns at each
milestone. The setup procedures are in [EVALUATION.md](EVALUATION.md); earlier
success and failure are retained in [the native loop record](docs/native-loop-proof.md),
and the longer controlled episode is in [the native care season](docs/native-care-season.md).

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
