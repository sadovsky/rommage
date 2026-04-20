# Architecture

How the pieces fit together, for someone (human or AI) modifying the code.

## The core problem

A Game Genie code is a scrambled `(address, value, compare)` triple. It
patches CPU reads: when the CPU reads from `address` in `$8000–$FFFF`,
return `value` instead — either unconditionally (6-letter code) or only
if the real ROM byte equals `compare` (8-letter code).

Brute-force search means: for each candidate code, run the game with the
cheat applied, see what happens, score it. The challenges:

1. **Applying cheats.** Stock nes-py has no hook for this. We fork.
2. **Defining "what happens."** We capture frames and measure visual
   distance to a baseline.
3. **Scale.** 6-letter space is ~8M candidates; 8-letter collapses to ~8M
   in practice (compare byte is constrained by the ROM). At ~2 rollouts/sec
   per core, 8 cores, that's ~140 hours for full 6-letter. Sampling and
   smart pruning matter.

## Layered design

```
┌──────────────────────────────────────────────────┐
│                 rommage.py                           │
│  argparse, progress reporting, output assembly   │
└────────┬──────────────────────────┬──────────────┘
         │                          │
         ▼                          ▼
┌─────────────────┐      ┌────────────────────────┐
│   report.py     │      │      search.py         │
│ HTML gallery    │      │ enumeration + parallel │
└─────────────────┘      │ two-stage eval         │
                         └──────┬─────────┬───────┘
                                │         │
                                ▼         ▼
                         ┌──────────┐ ┌──────────┐
                         │runner.py │ │scorer.py │
                         │rollouts  │ │distances │
                         └─────┬────┘ └──────────┘
                               │
                               ▼
                      ┌───────────────────┐
                      │  cheat_env.py     │
                      │ NESEnv wrapper    │
                      └────────┬──────────┘
                               │ ctypes
                               ▼
                      ┌───────────────────┐
                      │ lib_nes_env.so    │  ← forked C++
                      │ (CheatTable in    │
                      │  MainBus)         │
                      └───────────────────┘
                               ▲
                               │
                      ┌────────┴──────────┐
                      │   genie.py        │
                      │ Game Genie codec  │
                      └───────────────────┘
```

## Module responsibilities

### `genie.py` — codec
Pure-Python, no dependencies. `decode(str)` and `encode(GenieCode)` using
the verified nesdev bit formulas. Also hosts `iter_6letter` and `iter_8letter`
generators, though the practical enumeration lives in `search.py` where it
can consult the ROM for compare constraints.

### nes-py fork — `CheatTable` in C++
A `std::vector<CheatEntry>` on `MainBus`, consulted in the `$8000–$FFFF`
branch of `MainBus::read`. Held as `shared_ptr` so backup/restore preserves
cheats across `Emulator::backup()/restore()`. Linear scan on lookup — fine
for 1-3 active cheats (our case); would need a hash map for thousands.

### `cheat_env.py` — Python ctypes wrapper
Subclasses `nes_py.NESEnv`, adds `add_cheat`, `remove_cheat`, `clear_cheats`,
`cheat_count`. Degrades gracefully if the patched `.so` isn't installed
(raises `CheatsNotSupportedError` with an actionable message).

### `runner.py` — rollouts
`RolloutRunner` owns one `CheatNESEnv` for its lifetime. Key optimization:
an optional warmup sequence is run once, then `_backup()` is called; every
subsequent `run()` does `_restore()` instead of `reset()`. This skips title
screens on every candidate. Input sequences are `[(button_bitmask, n_frames)]`
lists.

### `scorer.py` — distance
Two metrics over frame stacks:

- **dHash** — 64-bit perceptual hash per frame, Hamming distance between
  baseline and candidate. Fast. Robust to small scroll shifts.
- **Color histogram** — 64 bins (2 bits per RGB channel), L1 distance.
  Catches semantic changes invisible to dHash.

Per-frame distances are aggregated to mean and max. Bucketing into
`null` / `interesting` / `likely_crash` is threshold-based; thresholds
are module-level constants tuned empirically.

### `search.py` — the driver
Two stages to prune the space cheaply:

**Stage 1 (fast reject):** 60-frame rollout, dHash-only. Drops candidates
whose distance is near-zero (did nothing) OR very large (crashed). Typically
kills 90-95% of candidates at ~1/5 the per-candidate cost.

**Stage 2 (deep eval):** 300+ frame rollout on survivors, both metrics, save
a thumbnail. Ranked by a combined score that penalizes `likely_crash`.

Parallelism is `multiprocessing.Pool` with `imap_unordered`. Each worker
holds its own `RolloutRunner` — emulators don't pickle. The baseline
features (phash array + histogram array) are computed once in the main
process and passed to workers via `initializer`.

### `report.py` — HTML gallery
Static HTML + thumbs. Dark theme, monospace, pixelated rendering. One card
per candidate with code, address, value, bucket, and distances.

### `rommage.py` — orchestration
`decode` / `encode` / `search` subcommands. Pre-baked input sequences
(`idle`, `press_start`, `walk_right`, `random`). The `search` command
builds a `SearchConfig`, runs `run_search`, ranks, writes the report.

## Design decisions worth knowing about

**Why fork instead of patching ROMs on disk.** Per-candidate ROM patching
requires mapper-aware offset translation and an emulator reload per
candidate. The fork is a one-time C++ edit and gives instant in-emulator
cheat swapping. Rollouts become 50-100x faster.

**Why `shared_ptr<CheatTable>` instead of a plain member.** `Emulator::backup()`
copies the whole `MainBus`. If the cheat table is a plain member, backup
duplicates it — and then cheats added after the backup don't affect the
restored state. A shared table means both live and backup buses point at
the same cheats, which matches user intuition ("cheats are a property of
the session, not of the game state").

**Why dHash and not SSIM/CNN embeddings.** Pixel-L2 is wrong (one-pixel
scroll = huge distance, zero semantic change). SSIM is slow. CNN embeddings
are overkill and trained on natural images, not NES sprites. dHash +
histogram is the pragmatic sweet spot — fast, interpretable, and
complementary (dHash catches layout, histogram catches palette/content).

**Why two stages instead of one.** Stage 1's purpose is to cheaply distinguish
"cheat did something" from "cheat did nothing" (which is 90%+ of candidates
for a random address) and from "cheat crashed the emulator" (PPU garbage —
very high distance). Stage 2's purpose is to rank the remaining candidates
on both axes with a longer rollout that lets delayed effects manifest.

**Why NROM only in v1.** NROM has no banking — CPU `$8000-$FFFF` maps
linearly to PRG-ROM. Other mappers bank-swap, which makes the 8-letter
compare byte's purpose visible: it disambiguates which bank the code
applies to. Supporting mapped games means iterating over all banks' bytes
at each address. Straightforward but deferred — see `NEXT_STEPS.md`.

## File dependency graph

```
rommage.py
├── search.py
│   ├── runner.py
│   │   └── cheat_env.py
│   │       └── genie.py
│   ├── scorer.py
│   └── genie.py
└── report.py

test_*.py ← standalone, no cross-dependencies
```

All modules are flat; no package layout. `import genie` etc. works when
`genie_search/` is on `PYTHONPATH` or is the current directory.
