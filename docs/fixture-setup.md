# Operator preparation of a brewing fixture

This script prepares declared test assets in a **preserved, disposable copy** of
an already embarked fortress. It is separate from the evaluated model's actions.
The implementation has been reviewed against DFHack 53.16-r1.1 source; a passing
software test does not establish native gameplay correctness.

Install `src/dfeval/bridge/lua/dfeval-prepare.lua` as
`<game>/dfhack-config/scripts/dfeval-prepare.lua`. From the DFHack console, with
the disposable fortress loaded and paused:

```text
dfeval-prepare check --worker 662 --out brewing-check-1
dfeval-prepare check --worker 662 --pos 40,50,100 --out brewing-check-2
dfeval-prepare apply --worker 662 --pos 40,50,100 --out brewing-apply-1
```

These IDs and coordinates are examples. The first command searches up to twelve
tiles around the chosen worker, on the same level, and returns up to eight
candidate positions. Use a returned position for `apply`. `--pos` is the upper
left corner of a 3×3 still. The clear area must be 4×3; supplies occupy the middle
tile of the extra eastern column. A check writes an audit record but does not
change native game state.

`--out` is a new lowercase alphanumeric/hyphen token, up to 64 characters. Each
record is written under `<game>/dfhack-config/dfeval-setup/<token>.json`; existing
records are never overwritten. Inspect its `ok`, `outcome`, native timestamp,
fixture IDs, and any error or cleanup information. Running through `dfhack-run`
uses exactly the same arguments. Neither command saves or exits the game.

The fixed `brewing-v1` fixture consists of one completed granite-block still,
twenty plump helmets in one stack, and four empty oak barrels. It uses one
existing, idle, sane adult citizen whose brewing labor is already enabled.
Setup changes no labor configuration, unit attributes, needs, stress, hunger,
thirst, skills, or simulation time. It creates no citizens and queues no brewing
jobs. The native still and its input materials are created before scoring; normal
native worker selection, hauling, and reaction execution must do the later work.

Preflight refuses hidden, wet, non-floor, designated, occupied, constructed, or
zoned tiles. It requires the worker and footprint to share native walkability
groups. That cache can be stale and does not prove burrow access or that the
worker will take the job. The script refuses an active bridge advance. The
operator must also ensure that no scored experiment is running between advances.

The site has one persistent fixture marker. Repeating the command with a new
output token checks the existing IDs and returns `existing_unchanged`; it does
not replenish supplies or create a second still. A changed, consumed, or failed
fixture must be reset from the preserved checkpoint. The marker is persisted
with the save, so save the prepared copy before capturing its starting snapshot.

Failure cleanup is deliberately limited to newly created assets. It requests
removal of newly created loose items and attempts immediate removal of a still
that has not begun construction. A finished or partially finalized still and
attached items may remain. Their IDs are recorded; this is **not an atomic game
transaction**. Preserve the pre-setup checkpoint and restore the disposable copy
after any failed application. Do not use this script to repair a valuable fort.

The construction sequence follows the pinned official
[build-now source](https://github.com/DFHack/scripts/blob/7549711a993e03bef19e90b27427096c1099853e/build-now.lua):
remove this still's construction job, attach its block, finalize its build
stage/design, call `completeBuild`, and request pathfinding reindexing. The
script does not invoke `build-now`, since that command also cycles buildingplan
and can globally unsuspend construction jobs. The public
[building and item APIs](https://docs.dfhack.org/en/53.16-r1/docs/dev/Lua%20API.html)
provide construction, item creation, placement, and persistent site data.

## Save and reload the fixture

The following **manual operator procedure** was exercised with Windows DF 53.16
and DFHack 53.16-r1.1. It uses the native game menus; it is not an automated
reset command.

1. Leave the fortress paused. Before applying the fixture, preserve the original
   with this same procedure and a separate checkpoint destination.
2. Open **Options**, choose **Save and return to title menu**, then **Save to
   this timeline**. Wait for the title screen. In the validated save, both
   `dfhack.isMapLoaded()` and `dfhack.isWorldLoaded()` were false at that point.
3. Choose **Quit** on the title screen and wait for the game process to exit.
   Capture the prepared save with `python start.py scenario capture`, using a
   new destination. Do not copy or restore a save while the game is running.
4. Start the game. Select **Continue active game**, the world row for the chosen
   save folder, and then the active fortress row. Leave the loaded game paused.
   The validated reload of `region1` / Workedsculpt retained the paused native
   absolute tick `20178138`; these names and that tick identify the test case,
   not values another fortress should have.
5. Before scoring, inspect the fixture audit record and the loaded observation.
   Use the captured prepared checkpoint as the first run's
   `--starting-snapshot`. A later repeat must restore this checkpoint and pass
   its starting-state check before the replacement model is called.

The installation exercised here stores saves in
`%APPDATA%\Bay 12 Games\Dwarf Fortress\save`, outside the game directory. Supply
that directory explicitly with `--save-root` when capturing or restoring. See
[save-location commands](benchmarking.md#choose-the-save-root-explicitly) and
the [repeat workflow](benchmarking.md#repeat-a-starting-site-with-another-model).
The installed `dfhack.getSavePath()` assumes a `save` directory inside the game
installation and does not locate this external save root.

The official
[`quicksave`](https://docs.dfhack.org/en/53.16-r1/docs/tools/quicksave.html)
command is useful for an additional autosave, but the game retains only three
autosaves. Its return neither confirms that disk writes are finished nor proves
that the named timeline save was refreshed. Use the save-and-quit procedure
above for a checkpoint and preserve the original separately.

The release's
[`load-save` documentation](https://docs.dfhack.org/en/53.16-r1/docs/tools/load-save.html)
marks that script unavailable, despite retaining command-line examples. Its
installed implementation uses old title/load-screen fields. This preparation
script therefore makes no promise of a supported programmatic reload/reset
command. The menu procedure above has been validated; automating it has not.
