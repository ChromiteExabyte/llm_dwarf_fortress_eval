# Native care pilot with Ollama

On **2026-09-05 (America/Toronto)**, an actual Ollama 0.33.3 runtime on Windows
passed the synthetic connection check with an imported Qwen2.5-1.5B-Instruct
Q4_K_M model. Its subsequent native fortress trial failed on the first response:
generation reached the declared 512-token limit before completing a decision.
The host accepted no action and advanced no game ticks. Final pause and
restoration of the original simulation FPS cap were confirmed.

**This is an engineering pilot.** The model did not complete the planned
one-month horizon. Both controls completed all 33,600 ticks; the rule control
produced 100 new drink units through four native brewing jobs. All three
recordings were audited and included in the comparison report, with the model's
failed horizon explicitly retained. This establishes neither an LLM care
comparison over a full month nor drink consumption, wellbeing improvement, or
a model ranking. The earlier
[llama.cpp brewing proof](native-loop-proof.md) remains
separate evidence with different runtime settings and model inputs.

## Plan declared before the trial

The operator recorded `runs/care-study/study-plan.json` locally before the native
trial. This is a local declaration, not an independently timestamped study
registration. The planned order was Qwen through Ollama, the always-waiting
`idle` control, then the `rule` control that requests one brew job at an idle
completed still. There is one site and one trial per policy.

All trials use the existing `brewing-v1` checkpoint described in
[fixture setup](fixture-setup.md). The prepared still and materials are supplied
test assets. Seven citizens and 60 drink stack units were present in the native
starting observation. Preparation and save/reload operations occur outside the
evaluated phase. No hidden repair, edited care timers, forced scarcity,
resampling, or replacement model is allowed after observing trial outcomes.

| Predeclared setting | Value |
| --- | --- |
| Native game / bridge | Dwarf Fortress 53.16 / DFHack 53.16-r1.1 |
| Model observation contract | `native-care-v1` |
| Decisions / policy-call ceiling | 4 / 4 |
| Ticks after each accepted wait or brew | 8,400 |
| Total tick ceiling | 33,600 |
| Bridge-call ceiling | 64 |
| Reserved model-output token ceiling | 8,192 |
| Recorded policy-output byte ceiling | 262,144 |
| Episode wall-time ceiling | 3,600 seconds |
| Request timeout | 600 seconds |
| Temporary simulation FPS cap | 1,000 |
| Model context / output per call | 16,384 / 512 tokens |
| CPU settings | `num_gpu=0`, `num_thread=4` |
| Sampling | Temperature 0, seed 17, `think=false` |

DFHack documents 8,400 ticks per week and 33,600 ticks per month. The pilot uses
that conversion to declare a fixed observation horizon; it does not guarantee
that a dwarf drinks or experiences a wellbeing change in that interval.
[DFHack time conversions](https://docs.dfhack.org/en/53.16-r1/docs/tools/set-timeskip-duration.html).

The recorded experiment additionally retains input, snapshot, log, and history
limits: 2,000,000 input bytes, 4,000,000 snapshot bytes, 64,000,000 log bytes, and
eight previous public decisions. Controls inherit the source experiment's
budgets and temporary speed cap through
[repeat preparation](benchmarking.md#compare-with-an-idle-or-rule-control).
They restore the same verified checkpoint and must match the source's native
starting-state guard before their first policy call. A failed model horizon is
reported as incomplete; a control does not fill in missing model performance.

The planned outcomes are accepted decisions, native ticks advanced, confirmed
brewing products, per-citizen thirst/hunger/sleep timers, needs and stress,
observed deaths, drink stocks, native and inference throughput, errors, and
cleanup. These are separate measurements. Stock changes and timer resets alone
are not direct consumption receipts or causal wellbeing estimates.

## Verified runtime and model provenance

The operator downloaded and verified the official standalone Windows CLI ZIP,
then ran `ollama serve` as an owned local process. The native `/api/version`
response reported `0.33.3`. This exercised Ollama's native API, including
`/api/chat`, `/api/tags`, `/api/show`, and `/api/ps`.
[Official v0.33.3 release](https://github.com/ollama/ollama/releases/tag/v0.33.3),
[standalone Windows CLI documentation](https://docs.ollama.com/windows#standalone-cli).

| Verified asset | Bytes | SHA256 |
| --- | ---: | --- |
| `ollama-windows-amd64.zip`, v0.33.3 | 1,469,175,900 | `52cb36a62e7e501f61514f60212dec7117b6c098811357585e02fffe32d2fcd7` |
| `qwen2.5-1.5b-instruct-q4_k_m.gguf` | 1,117,320,736 | `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e` |

The archive hash matches the
[official release asset digest](https://github.com/ollama/ollama/releases/expanded_assets/v0.33.3).
The GGUF came from Qwen's own repository at revision
`91cad51170dc346986eccefdc2dd33a9da36ead9`; its hash matches the
[exact published file](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/blob/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf).
It was imported locally using a Modelfile `FROM` entry and `ollama create`, with
the CPU, context, sampling, and output parameters above. No new quantization was
performed. [Official GGUF import procedure](https://docs.ollama.com/import#importing-a-gguf-based-model-or-adapter).

The server used loopback, `OLLAMA_NO_CLOUD=1`, one parallel request, and one
loaded model. Model storage and the Ollama process's `USERPROFILE` were scoped
to ignored workspace directories; generated local key material was kept out of
logs. No installer, machine-wide environment change, registry entry, startup
entry, or service was used. The runtime and model weights are not distributed
with this project. This process configuration is not an operating-system
security sandbox or an automatic public launcher.

The recorded `/api/show` response confirmed `num_gpu=0` and four CPU threads.
After the synthetic check, `/api/ps` reported `size_vram: 0` and
`context_length: 16384`, supporting CPU residency for the loaded model. Ollama
reported the imported model's parameter-size metadata as `1.8B`; the published
model name remains Qwen2.5-1.5B-Instruct. Hardware and runtime residency are
observations from this host, not a speed guarantee for other machines.
[Ollama running-model API](https://docs.ollama.com/api/ps).

## Observed model outcome

The synthetic `model-check` returned a valid `wait` decision in 18.297 seconds,
with 550 reported prompt tokens and 48 completion tokens. Its report records
`game_access: false` and `actions_executed: 0`. This established a connection
and response contract before the native trial.

The native trial passed its initial-state guard and sent the complete declared
projection of the starting observation. Ollama reported `done: true` with
`done_reason: "length"`. The unfinished response proposed brewing and repeated
its public explanation until the 512-token cap. Its text also claimed a job
had already been queued; that was model-generated text, not an executed action.
The host rejected the response with `PolicyError` and performed cleanup.

| Recorded model-trial measurement | Result |
| --- | --- |
| Terminal outcome | `error`, `ok: false` |
| Model calls / accepted decisions | 1 / 0 |
| Queued brewing jobs / requested ticks / reported elapsed ticks | 0 / 0 / 0 |
| Confirmed new drink units | 0 |
| Model-reported prompt / completion tokens | 4,534 / 512 |
| Model-reported prompt processing / generation | 107.334 / 57.310 seconds |
| Native decoding rate | 8.934 tokens/second |
| Host policy-call / total trial duration | 164.703 / 166.781 seconds |
| Final pause / original FPS cap restored | Confirmed / confirmed, to 100 |

The decoding rate divides the reported completion count by generation time;
it excludes prompt processing and host overhead. These one-call measurements
cannot establish a stable hardware comparison. Native timing fields and their
units follow the [Ollama chat API](https://docs.ollama.com/api/chat).

The run contains one native snapshot and zero simulated elapsed ticks. Its
seven observed citizens and 60 drink units are starting-state evidence, not a
longitudinal care result. No invalid response was repaired, retried, or replaced
with a scripted action.

## Completed controls and comparison

| Policy | Accepted decisions | Native ticks | Confirmed new drink units | Wall seconds | Advance ticks/second |
| --- | ---: | ---: | ---: | ---: | ---: |
| Qwen through Ollama: first response failed | 0 | 0 | 0 | 166.781 | Unknown; no advance call |
| Idle control | 4 waits | 33,600 | 0 | 77.125 | 447.064 |
| Rule control | 4 brews | 33,600 | 100 | 69.125 | 501.485 |

Both controls restored the same checkpoint and passed the native starting
guard before any decision. Each advanced four intervals of exactly 8,400 ticks,
ending with `budget_exhausted`, detail `max_decisions`, and `ok: true`: the
declared limit was reached with successful cleanup. Final pause and restoration
of the original FPS cap were confirmed for all three trials. Neither control
made a model call. Advance timings include bridge and pause overhead; these
single trials do not establish that a brewing policy makes the game run faster.

| Native measurement | Shared initial | Idle final | Rule final |
| --- | ---: | ---: | ---: |
| Living population | 7 | 7 | 7 |
| Drink stack units | 60 | 53 | 153 |
| Thirst timer median | 1,338 | 13,394 | 13,744 |
| Hunger timer median | 1,338 | 34,938 | 34,938 |
| Stress median | 220 | 1,700 | 530 |
| `DrinkAlcohol` need focus median | -190 | 208 | 203 |

Each control retained five native snapshots and recorded zero observed confirmed
deaths. Native post-production receipts linked the rule control's four accepted
brew requests to four jobs producing 25 new drink units each. That evidence
establishes production independently of the final stock difference. Idle had
no queued brewing jobs and no corresponding new-product receipts.

The raw care differences do not identify consumption events, explain the changes
causally, or establish improved wellbeing. The failed model trial has no
month-end measurements: comparing its unchanged start against either control's
final state would confound elapsed game time with policy effects. One trial per
policy at one site cannot establish a general care ranking or statistical
significance, including between the controls.

The final audit's `equal_recorded_conditions` checks were all true: experiment
configuration after excluding policy configuration and normalizing each run's
local `STOP` path, starting-save hash, native starting fingerprint, recorded
installation fingerprint, bridge identity, game/DFHack versions, and observation
version matched. The recomputed SHA256 values of the first canonical policy
inputs also matched across all three policies. Every recorded input hash was
verified, every initial guard matched, and `all_planned_config_fields_match`
was true. Model connection settings and computation differ from the scripted
controls; equal experiment ceilings do not imply equal model costs.

Each first canonical policy input was 9,612 bytes, with SHA256
`d4214140b983f49d2ad4b7e8946c1a9d380c2140cb0022f400d33272d2fa34bb`.

All three recordings passed completeness, world/time-continuity, and observation
contract checks. A complete recording can document a failed trial:
`all_completed_full_horizon_with_cleanup` is false because the model advanced
zero ticks. The report's `recorded_setup_matches` is also false; its two listed
qualifications are the model's terminal error and recorded failure, with no
listed starting-condition mismatch. Matching the recorded scope does not prove
identical unobserved native state, external configuration, or deterministic
future simulation.

## Source identities and retained evidence

The audited local predeclaration, `study-plan.json`, has SHA256
`5799a565f2ef9b197d4a66f5d85fdda56418b62fe96cc67e7b3bdbb588c55028`.
The audit and report identify these original recording files:

| Trial / file | Bytes | SHA256 |
| --- | ---: | --- |
| Model / `manifest.json` | 106,276 | `665243ee4ec186c493a4f293eb7a2634e032d1d58369ca37aa920851bcf2dc4e` |
| Model / `events.jsonl` | 74,007 | `d05f35be2280a6978bf0daedbd05b6021431d8e73b071f1812efa2dad96557f9` |
| Idle / `manifest.json` | 99,971 | `f7ce32bbb5e32933b95eff73c826b0035b3a55d81caddd07ee1aeb02abb2485e` |
| Idle / `events.jsonl` | 183,486 | `0b48af2d88d557c033cc0848e4acdf84e3ccb80aa881f4f03f39cb33fde774ec` |
| Rule / `manifest.json` | 100,081 | `f930ceff32c6e197a7443f98d36fdd2b9796d14463ef73a1b1c751f09e973b1c` |
| Rule / `events.jsonl` | 209,202 | `975ef61d88c48548efdbdc53d043caea9f4f134fab98eabf8ab1aa05572eb8e2` |

These hashes identify the inspected bytes; they do not authenticate the author
or make the private recordings publicly downloadable.

Full raw evidence remains under the ignored `runs/care-study/` directory:
the predeclared plan, runtime and model provenance, synthetic check, residency
record, native manifest/event stream/result, final audit, and three-policy
JSON/CSV/Markdown report. The source
release contains this reviewed account, not those machine-specific raw files,
keys, binaries, model weights, or the game save. An auditor with the original
recordings can inspect the raw native snapshots alongside the exact projected
policy inputs and rejected response. The normal
[report and replay commands](benchmarking.md#read-care-and-speed-separately)
apply to those recordings.
