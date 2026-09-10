# Your first learning session

Run `python start.py setup`, then `python start.py play`, from the project
folder. On Windows, `py -3` can replace `python`. No pip installation is needed.

## Game

Install DF and matching DFHack using [official instructions](https://docs.dfhack.org/en/stable/docs/Installing.html).
[Classic DF](https://www.bay12games.com/dwarves/) is available from Bay 12 Games.
The previous native bridge was exercised on Windows with DF 53.16 and DFHack
53.16-r1.1. This does not validate the new controls. The transport supports
native Windows and Linux; the learning loop has not been live-validated on
either platform.

Launch with DFHack, create a world, embark, and pause. Keep the game window
available and use one controller. Nothing in `play` loads or restores saves.
Do not run the old fixture preparation script: that creates a brewing task.

In setup, select the folder containing the game executable and `hack`.
Use `python start.py doctor --df-path "YOUR_GAME_FOLDER"` to check its files.

## Cloud model

Choose `cloud` in setup. Use a provider supporting text Chat Completions.
Supply the exact model ID, full HTTPS `chat/completions` URL, and current
USD prices per million input/output tokens. Use undiscounted rates that include
any context-length premium. Setup writes `play.local.toml`, which is ignored
by Git. Edit it in a text editor to change settings.

Setup asks for an **environment variable name**, not your API key.
For the default variable name, set the key in the same terminal before play:

```powershell
# Windows PowerShell: key entry without putting it in command history.
$taskSecret = Read-Host 'Provider API key' -AsSecureString
$env:DF_MODEL_KEY = [System.Net.NetworkCredential]::new('', $taskSecret).Password
python start.py play
```

On Linux (Bash):

```sh
read -rsp 'Provider API key: ' DF_MODEL_KEY
export DF_MODEL_KEY
python start.py play
```

The key stays outside settings, logs and the model workspace. Your provider
receives model messages, including game data and notes the model reads.
Setup, help, doctor and inspect make no paid calls.

## Spending

New cloud runs default to a $10 spending guard. For a shorter trial:

```sh
python start.py play --budget 1
```

Before a call, the guard reserves serialized request bytes plus 4096 as a
conservative input-token allowance, and the complete output-token cap, at
the entered prices. Complete reported usage replaces the reservation.
Missing usage retains it and stops further cloud calls; usage above the
reservation also stops further calls. Cached input uses the full rate.

Bytes are an allowance, not an exact tokenizer measurement. No extra provider
fees or currency conversion are included. Use a provider-enforced hard cap or
limited balance too: this application cannot enforce the provider's invoice.

## Local model

Start your model server first. Choose `local`, then `ollama` or
`chat_completions` (for a compatible local endpoint). The launcher does not
install or download models. There is no paid API charge; the other resource
limits remain. Ollama setup requests a 32768-token context capacity, which
may require more RAM than the server's default.

## The blank start

Every run creates an empty `workspace/`. The initial messages contain the
objective, tool mechanics and resource limits. They contain no examples,
DF guide, diary template, predefined helpers or retrieved notes.

The model can inspect the game, choose simulation waits, use native controls,
list/read/write/delete files, and execute self-authored JSON lists of up to
twelve tool calls. Routines are bounded action sequences, not arbitrary code.
It can submit its own messages to `infer` or call `context_reset` with text
it chooses to retain. All inference shares the same model and budget.

There is no host-written memory summary, API retry, helper role or fallback
model. Invalid tool syntax becomes visible feedback for a subsequent ordinary
model turn, which consumes the same budget. A truncated response executes no
tool. Transport failures stop the run.

Game observations include native dwarf stress and needs, which go deeper
than an ordinary screenshot. This is not a strict player-screen-only
experiment. Unknowns remain unknown; no care score is calculated.

## Stop and inspect

Ctrl+C stops the runner and attempts a final pause. You can also create an
empty `STOP` file in the printed run directory. The current call may take until
its timeout to return. A client cannot force a provider to cancel a request
already received. Check the game if pause was not confirmed.

```sh
python start.py inspect runs/YOUR-RUN
```

Read the model's workspace in any text editor. The run's `README.md` explains
the stop, and `events.jsonl` records exact messages and actions. The old viewer
does not read this new format. Watch the actual game.

Files persist on disk, and the model can reread them across its own context
resets. Automatic resume, save restoration and cross-run memory transfer are
not implemented; a new `play` always starts blank.

Validated tools keep the model's file access in its workspace. There is no
shell, Python executor, arbitrary DFHack command or general internet tool.
Folder separation alone is not OS isolation. An actual container or VM must
precede arbitrary generated-program execution.

## Historical tools

Add `legacy` after `start.py` for older commands:

```sh
python start.py legacy demo --open
python start.py legacy benchmark --help
```

These recordings remain historical evidence of the brewing experiment.
Their large evaluation guides are optional reference, not setup steps.
