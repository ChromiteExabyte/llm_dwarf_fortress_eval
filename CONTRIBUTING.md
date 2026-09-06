# Contributing and preparing a release

The goal is an observable real-game evaluation of competence and dwarf care.
Read [PROJECT_DIRECTION.md](PROJECT_DIRECTION.md) before extending the action
space or scoring. Mock results must remain clearly identified as mock results.
Contributions to project code and documentation use GPL-3.0-or-later; preserve
any applicable third-party notices.

## Local development

Use Python 3.11 or newer in a virtual environment. The following commands use
that environment's Python; see [README.md](README.md) for creation commands.
The offline installation check also requires pip 22.3 or newer in this
development environment.

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
```

Tests use temporary fake installations, fake model responses, and local HTTP
servers. They require no game, API key, external service, or model calls. Use the source runner
`python tools/dfeval.py --help` if you only need the standard-library core.

For live changes, unit tests are necessary but insufficient. Record the tested
game/DFHack versions, native observations, requested actions, and observed
completion in a disposable fortress. Report unverified fields as unknown. Keep
the game window available and retain the raw local evidence for inspection.

## Build and verify what people will receive

```sh
python -m build
python tools/check_release.py dist --source-root .
python tools/check_install.py dist
python -m twine check --strict dist/*
```

`build` creates a source archive and then a wheel from that archive. The release
checker inspects archive paths and contents without extracting them. It rejects
unexpected package files, game/runtime binaries, saves, raw runs, environment
files, obvious credentials, and personal home paths. It also checks tracked
working-tree files when a Git repository is available. It is a guardrail, not a
complete secret detector or a review of Git history/staged blobs.

The install check creates a fresh temporary environment, installs the wheel
without dependencies or a package index, and exercises its CLI, bundled Lua
resources, recorded native demo, and spectator over loopback HTTP from outside
the source checkout. This catches source-tree imports and missing package data
that unit tests can miss. It does not execute Lua inside a game.

GitHub Actions is configured to run tests, builds, archive checks and clean installation checks
on Windows, Linux and macOS. It uses no model secrets and does not install the
game. Action revisions are pinned; Dependabot proposes updates. A successful
workflow does not establish native game compatibility on every platform.

## Put this project on GitHub

Keep Dwarf Fortress and DFHack as external runtime dependencies. Do not upload
the entire working directory as a ZIP: it may contain a game, saves, downloaded
installers and private logs. Use Git's reviewed source files or the checked
source distribution. A renamed game directory must also stay untracked.

Before the first push, inspect:

```sh
git status --short
git diff --cached --stat
git diff --cached
python tools/check_release.py dist --source-root .
```

If starting from an unpacked release without Git, initialize a repository with
`git init -b main` and add the named source, documentation and configuration
files deliberately. The `.gitignore` excludes standard local installations,
credentials, environments and run outputs. Do not use `git add -f` to include
those exclusions. Review the intended commit, then create a GitHub repository
and push it using your normal Git or GitHub workflow.

No workflow publishes packages, creates releases, or deploys a site. Publication
is a deliberate maintainer action. A release should state the version, checks
that actually ran, the supported game/DFHack pair, and unverified capabilities.
The Python distribution name is local metadata; this project does not claim a
PyPI name or publish anything there automatically.

Raw benchmark evidence needs a separate publication review because logs can
contain machine paths, prompts, responses and user-provided text. Preserve the
original local record and document any redaction in a shareable copy. Do not
replace unknown measurements with invented values to make an example complete.
