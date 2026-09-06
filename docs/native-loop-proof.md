# Native model loop: brewing proof

Validation began on 2026-09-05 using real Dwarf Fortress 53.16 and DFHack
53.16-r1.1 on Windows. A local Qwen2.5-1.5B model made three brewing decisions;
the runner queued the corresponding native jobs; a dwarf produced two drink
stacks containing 50 units in total. The bounded run ended with the game paused
and its original simulation FPS cap restored.

**Both trials are complete.** Model A produced native drinks. Model B restored
the checkpoint and passed the starting-state check, then repeated text until
its 256-token allowance ended. The runner accepted no incomplete action and
exported both outcomes in a paired report.

This establishes the bounded model/action/observation loop and a checked reset
for evaluation with another model. It
does not establish that the model is a good fortress manager, that it improved
wellbeing, or that one model is better than another.

## Declared starting fixture

The original fortress was preserved before operator preparation. At paused
absolute tick `20178138`, the operator created the `brewing-v1` fixture described
in [fixture setup](fixture-setup.md): one completed granite-block still, twenty
plump helmets, and four empty oak barrels. The still had native ID `1`; an
existing citizen, ID `662`, already had brewing labor enabled.

The preparation receipt records no brewing jobs queued, no labor changes, and
no changes to citizen care values. This preparation is outside the evaluated
model phase. The still and supplies are supplied test assets, not achievements
by the model. During evaluation, worker selection, hauling, and reaction
execution proceed through ordinary native game jobs.

The prepared fortress was saved through the game menus, the game was fully
closed, and the saved bytes were captured as a checkpoint. The first run
declares checkpoint manifest SHA256:

```text
182c40d4d913fff2c79a0084400efb4b5dfc8f88c0f8f58e3c09bb5c10a92740
```

The installation used an external save root. Save capture and restoration must
use the directory that actually contains the chosen save, as explained in
[save locations](benchmarking.md#choose-the-save-root-explicitly). A checkpoint
hash records the captured save identity; the repeat runner separately checks
the loaded starting observation before sending anything to the next model.

During operator save/reload work, a Windows window-control attempt to restore
the minimized game was blocked. The operator used DFHack's supported native UI
input route and inspected the observed UI text buffers to navigate the game
menus. These setup and reset actions happened outside the scored phase. No
extra operator game actions were issued during the scored model run.

## Models and fixed conditions

The pair uses the official Qwen GGUF releases below. These are
different model sizes from the same family, evaluated sequentially on one
machine. Model files and the game are not bundled with this repository.

| Run | Model | Quantization | Weight bytes |
| --- | --- | --- | ---: |
| A | Qwen2.5-1.5B-Instruct | Q4_K_M | 1,117,320,736 |
| B | Qwen2.5-0.5B-Instruct | Q4_K_M | 491,400,032 |

Pinned model A:

- [Official weights at revision `91cad51170dc346986eccefdc2dd33a9da36ead9`](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf)
- SHA256: `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`

Pinned model B:

- [Official weights at revision `9217f5db79a29953eb74d5343926648285ec7e67`](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf)
- SHA256: `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`

The runtime is [llama.cpp b10809, Windows CPU x64](https://github.com/ggml-org/llama.cpp/releases/download/b10809/llama-b10809-bin-win-cpu-x64.zip),
archive SHA256 `9df3158ed228a641a4b127942d7f459f24c9e13f04682659d05c00c80099b6b5`.
The validation machine has an Intel Core i5-1335U, with 12 logical processors;
inference uses CPU execution. Runtime launches record the command, weight
provenance, and hashes of the actual runtime binaries.

| Setting | Recorded value for A; reused for B |
| --- | --- |
| Allowed actions | `wait`, `brew`, `finish` |
| Maximum decisions and model calls | 3 each |
| Simulation interval after `wait` or `brew` | 1,200 ticks |
| Maximum total requested simulation ticks | 3,600 |
| Requested simulation FPS cap | 1,000 |
| Maximum response tokens per model call | 256 |
| Per-call timeout | 600 seconds |
| Total experiment wall-time allowance | 1,800 seconds |
| Context | 16,384 tokens |
| Generation / prompt-processing threads | 4 / 8 |
| Parallel model slots / GPU layers | 1 / 0 |
| Seed / temperature | 17 / 0 |
| Template / reasoning parser | ChatML with Jinja disabled / off |
| Flash attention | On |
| Response format | Closed action-specific JSON Schema |

The run manifest records the remaining limits, prompt, schema, installation
fingerprint, and bridge identity. The repeat recipe freezes those recorded
settings. A requested FPS cap is not measured game throughput; generation token
rates and simulation advance rates must be derived from their own counters.

## Confirmed native evidence from model A

All three accepted model decisions requested `brew`, `workshop_id: 1`,
`quantity: 1`. They queued native jobs `5`, `6`, and `9`. There were three
model calls, three accepted decisions, zero failed calls, and zero invalid
responses. Native product evidence confirms drink creation for jobs `5` and
`6`; no drink production from job `9` was observed before the run ended.

The following receipt was observed after native execution:

| Evidence field | Observed value |
| --- | --- |
| Source | `dfhack.eventful.onReactionComplete` |
| Reaction | `BREW_DRINK_FROM_PLANT` |
| Native absolute tick | `20179504` |
| Job / workshop / worker IDs | `5` / `1` / `662` |
| Callback event ID | `1` |
| Native item allocation interval | `[797, 798)` |
| Output item | ID `797`, type `DRINK`, newly created, stack size `25` |
| Receipt errors / dropped outputs | None / 0 |

A second callback for job `5` recorded its seed products. Callback `3` then
recorded another newly created `DRINK` item, ID `806`, with stack size `25`,
from job `6` at tick `20180813`, with the same workshop and worker. Its native
allocation interval was `[806, 807)`. A fourth callback recorded that job's
seed products.

The collector reported zero dropped events, dropped outputs, hook errors,
invalid evidence records, measurement errors, and unknown product quantities.
Both drink receipts link to accepted model decisions and their queued jobs.
Production is established by these native receipts, not by a disappearing job
or a change in stock totals.

The model's public explanation called the intended drink "mead", while the
recorded native action is plant brewing. A valid action and completed job do
not make every sentence in the model's explanation accurate.

## Model A outcome and care measurements

Model A ended at its three-decision limit: `outcome: budget_exhausted`,
`detail: max_decisions`, with a complete evidence stream. It advanced exactly
3,600 native ticks, from `20178138` to `20181738`. Final pause and restoration
of the original FPS cap were both confirmed. The complete episode took
701.328 seconds of wall time.

| Measurement | Initial | Final |
| --- | ---: | ---: |
| Living citizen population | 7 | 7 |
| Drink stack units | 60 | 110 |
| Median native stress | 220 | 640 |
| Median native thirst timer | 1,338 | 4,938 |
| Median `DrinkAlcohol` need focus | -190 | -700 |

There were zero observed confirmed citizen deaths. The increase in drink
inventory agrees with the two native production receipts. It does not prove
that a dwarf consumed the new drink, that needs were satisfied, or that care
improved. Stress and thirst timers increased while alcohol need focus fell;
these are raw native measurements, not a normalized care score. This run
provides no evidence that overall care improved.

| Timing measure | Model A result | Denominator |
| --- | ---: | --- |
| Native simulation advance | 370.981 ticks/second | Time inside measured advance operations |
| Native model decoding | 5.868 tokens/second | Provider-reported decode time |
| Completion throughput across model calls | 0.598 tokens/second | Whole model-call time, including prompt processing |

The different rates describe different work. The 1,000 FPS setting is a
requested simulation cap, not an observed rate. Model A reported 24,869 input
tokens and 412 completion tokens over its three calls; the runner reserved
768 output tokens in total. Prompt processing contributes to model-call time,
which explains why decoding throughput exceeds complete-call throughput.

## Model B restored-start check

The repeat workflow restored the captured starting save, retained the frozen
experiment settings, and selected `Qwen2.5-0.5B-Instruct-Q4_K_M`. The runner
recorded `matched: true` before B's first `policy_input` event. Expected and
actual canonical initial observation fingerprints were both:

```text
b71023d7368875e7afdd7f7b50a20e677775debc7a0db15d053f5139af0d0a1c
```

The checked installation environment fingerprint was:

```text
5472e8270186ef70d3926266e47ad7dab2d9e6a071dbdf05fdec3a34f7265a49
```

This confirms the recorded start and environment checks passed. Matching
recorded observations does not prove identical unobserved state or
deterministic simulation. The two raw initial observations differ only in the
bridge's per-session identifier, which the canonical check excludes. That
bookkeeping identifier is still present in model input; the prompts are not
byte-identical.

Model B returned `finish_reason: length` after 256 completion tokens, repeating
text inside an unfinished JSON string. The runner recorded an error, accepted
zero decisions, queued no jobs, and advanced zero ticks. The fortress retained
its initial seven citizens, 60 drink units, and tick `20178138`. Final pause and
FPS restoration were confirmed. No response repair, model fallback, or rerun
replaced this result.

| Recorded result | Model A, 1.5B | Model B, 0.5B |
| --- | ---: | ---: |
| Accepted decisions | 3 | 0 |
| Native ticks advanced | 3,600 | 0 |
| Confirmed new drink units | 50 | 0 |
| Total recording wall seconds | 701.328 | 123.484 |
| Native decoding tokens/second | 5.868 | 10.805 |
| Completion tokens / whole model-call second | 0.598 | 2.110 |
| Final pause / FPS restoration | Confirmed | Confirmed |

The paired export's conservative `recorded_setup_matches` flag is false because
B has a terminal model error and its corresponding error event. These are the
two comparison qualifications; the start and recorded settings did match. The
report preserves the failed trial instead of presenting a clean model ranking.
B's zero tick span is not evidence of superior care or faster successful play.

The environment fingerprint covers its declared installation-file inventory.
It does not cover external AppData preferences or model-server runtime options.
The same server flags were used for both launches, with only weights and alias
changed. Both models received a synthetic connection check before evaluation;
reported episode time excludes downloads and server startup.

The public evidence document is a reviewed extract. Raw local recordings remain
inspectable through the spectator and paired JSON/CSV/Markdown report. Their
source hashes are:

| Source | SHA256 |
| --- | --- |
| A events | `8c24314a7479babada9d71007abc530cc9f8d113bf4a06aef43ecaa0e297ad48` |
| A manifest | `9c1799e7f886773d97f53373cd26cf1af00e5902fb9640d1e0811b416aac3efe` |
| B events | `acce9b96e2e82fe555ab6364a3c0b055268dcd51fee6c2b59e6e230e93f28c64` |
| B manifest | `d09713d08bbb4a514143c006d43420bcdcf9e0e7d676b9d21401c9e2acab2010` |

## Reproduce the workflow

These commands reproduce the configuration on a separately prepared fortress.
The privately held checkpoint is not included. Another starting site will
produce a different trial; replacing paths does not recreate this exact world.
Use a preserved disposable fortress and follow [fixture setup](fixture-setup.md).

Save the prepared fortress and fully close the game, then capture its start:

```sh
python start.py scenario capture --df-path "YOUR_GAME_FOLDER" --save-root "YOUR_SAVE_ROOT" --save-name region1 --out saves/loop-baseline --game-stopped
```

Start the pinned model A on the local server. Use the appropriate executable
name on your platform; the flags below describe the tested Windows CPU setup:

```sh
llama-server --model qwen2.5-1.5b-instruct-q4_k_m.gguf --alias Qwen2.5-1.5B-Instruct-Q4_K_M --host 127.0.0.1 --port 18080 --threads 4 --threads-batch 8 --ctx-size 16384 --parallel 1 --n-gpu-layers 0 --flash-attn on --reasoning off --offline --no-webui --no-webui-mcp-proxy --cors-origins http://127.0.0.1:18080 --no-jinja --chat-template chatml --seed 17 --temp 0 --n-predict 256
```

Load the captured fortress and leave it paused. Run A:

```sh
python start.py experiment --df-path "YOUR_GAME_FOLDER" --policy local --model Qwen2.5-1.5B-Instruct-Q4_K_M --endpoint http://127.0.0.1:18080/v1/chat/completions --response-format json_schema --response-tokens 256 --timeout 600 --max-seconds 1800 --decisions 3 --ticks-per-decision 1200 --max-ticks 3600 --max-calls 3 --simulation-fps 1000 --starting-snapshot saves/loop-baseline --out runs/model-a --watch
```

When A ends, save and fully close the game. Stop model A's server. Restore the
checkpoint through a repeat recipe; this preserves the finished fortress in a
separate backup:

```sh
python start.py repeat prepare --from runs/model-a --model Qwen2.5-0.5B-Instruct-Q4_K_M --df-path "YOUR_GAME_FOLDER" --save-root "YOUR_SAVE_ROOT" --out runs/model-b --game-stopped
```

Start the same server command with only the model file and alias changed to
`qwen2.5-0.5b-instruct-q4_k_m.gguf` and `Qwen2.5-0.5B-Instruct-Q4_K_M`.
Load the restored fortress, leave it paused, and run the frozen recipe:

```sh
python start.py repeat run runs/model-b --watch
```

Export the pair from its recorded events:

```sh
python start.py benchmark-report runs/model-a runs/model-b/run --out reports/loop-comparison
```

The output provides JSON, CSV, and a readable report. Inspect starting-state
checks, evidence warnings, accepted actions, native product receipts, care
measurements, and timing coverage. One short pair is a reproducibility and
integration check, not a statistical ranking or proof of long-term wellbeing.
