# For a Claude Code session

Drop this prompt (or a variation) into Claude Code to hand off the port:

---

I'm porting a Game Genie brute-force search tool. It consists of:

1. A 6-file C++ patch to `nes-py` (at `nes-py-fork/nes-py-cheats.patch`)
   that adds cheat injection.
2. A stand-alone Python package (in `genie_search/`) that uses the patched
   library to enumerate and evaluate candidates.

Please work through `docs/PORTING.md` milestone by milestone. Do NOT skip
the verification step at each milestone — each test catches a different
failure mode, and skipping makes later failures ambiguous.

Specifically:

- **Milestone 0–1**: get nes-py building and the patch applied. Verify with
  the four-symbol check.
- **Milestone 2–3**: drop `genie_search/` in, run `test_genie.py`,
  `test_canonical.py`, `test_cheat_env.py`. All should print "ALL PASS".
- **Milestone 4**: run the speed check. Report the fps to me before
  continuing — if it's below 1000, we need to think about optimization
  before real searches.
- **Milestone 5**: run the synthetic-ROM CLI smoke test. Expect 0 passers
  (correct — the ROM doesn't render to screen).
- **Milestone 6**: STOP here and ask me for a real ROM path. Don't run
  against anything you find on disk without asking.

When debugging, reach for `docs/VERIFICATION.md` first — it has
failure-mode-specific diagnosis for each layer. `docs/ARCHITECTURE.md`
explains the design if you need to modify anything.

For extensions, see `docs/NEXT_STEPS.md`. Don't start on those until v1 is
fully running on a real ROM and I've given the go-ahead.

One known environment-specific issue: on numpy 2.x, nes-py's `_rom.py`
needs two small `int()` casts. This is documented in `docs/PORTING.md`
Milestone 0. If `from nes_py import NESEnv` throws `OverflowError`, that's
the fix.

---

## Notes on the style of this codebase

- **Flat module layout.** No package, no `__init__.py`, no `setup.py`.
  Just files that `import` each other. Suits the exploratory phase.
- **Tests are standalone `python test_*.py` scripts**, not pytest suites.
  Each one prints a human-readable pass/fail and exits cleanly. Keep it
  this way unless there's a compelling reason; pytest noise would bury
  the actual signal.
- **Comments explain why, not what.** See the long rationale comments in
  `scorer.py` (why dHash + histogram) and the nes-py patch (why
  `shared_ptr` on the cheat table). Maintain this style.
- **No AI-slop buzzwords.** Alex's codebase voice is conversational,
  lightly technical, low-fluff. Docstrings should explain what a function
  does, not what "powerful AI-driven capabilities" it provides.

## If things break badly

The quickest diagnostic is to `rm -rf` the forked nes-py directory, `pip
install --force-reinstall nes-py`, and re-apply the patch from scratch. The
build is idempotent and takes ~30 seconds. Trying to debug a partial
rebuild is rarely worth it.
