# Watch what it learns

Start with [the short setup](README.md). You do not need comparisons,
scripted controls or a test suite to try the experiment.

- What does it try, and what actually happens in the game?
- Does it notice a failed choice and change what it does?
- What does it remember, forget or turn into a reusable routine?
- How does it treat individual dwarves as circumstances change?
- How far does the budget take it?

Read its workspace and recorded actions together. A note about caring is not
proof of care; a queued job is not proof it completed. Failed runs remain
useful to inspect.

Fresh runs have no inherited notes, helper agents or care rubric. Do not add
a hidden tutorial to improve results. The model's pretrained knowledge remains;
this does not measure learning from zero training data or update its weights.

Optional historical comparisons are in [the legacy evaluation guide](docs/legacy-evaluation.md).
Those commands now need the `legacy` prefix. Brewing observations do not
validate the new game controls.
