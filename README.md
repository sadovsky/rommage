# rommage

> **rom·mage** /ˈrɒm.meɪdʒ/
> *v.*  to rummage through the guts of an NES ROM, hunting for byte
> perturbations that break — or improve — the game.
> *n.*  one who does this; a **rom-mage**.

Brute-force behavioural search for NES [Game Genie](https://en.wikipedia.org/wiki/Game_Genie)
codes. For each candidate, the ROM is executed with the cheat patched into
the CPU read path, frames are captured, and the resulting screens are
compared against a no-cheat baseline. Codes that meaningfully change the
game without crashing it are surfaced in an HTML report.

```
            .---.
           /     \        ,---,
          | O   O |      /     \
          |   ∆   |     | _____ |
           \  ~  /      ||  8  ||    <- ROM cartridge
            `---'       ||_____||
           ( /|\ )      |_______|
            / | \            |
           /  |  \           |
          /___|___\      [ emulator ]
         the rom-mage         |
             |                |
             `----->  patch --+  -> frames -> rank -> codes
                      bytes
```

## How it works

Four stages, each more expensive than the last:

1. **Trace** (optional, one-time per ROM+warmup): perturb each PRG byte
   once and run a short rollout. Addresses the game never reads during
   play are dropped — typically cuts the candidate universe by 8-10×.
2. **Boot check** (optional, per candidate): reset, apply cheat, step ~60
   frames. If the title screen visually diverges from baseline, the code
   breaks the cart's power-on path and is rejected.
3. **Stage 1** (fast reject, per candidate): short rollout, dHash-only
   distance, kill nulls and crashes.
4. **Stage 2** (deep eval, survivors only): full rollout, dHash + 64-bin
   colour histogram, bucket and rank.

Each stage runs a stateful multi-process pool over a persistent emulator
per worker, so there's no per-candidate boot cost. State backup/restore
inside the emulator skips the title-screen intro on every candidate.

## Quick start

One-time setup: apply the C++ cheat patch to nes-py and rebuild. Full
steps in `docs/PORTING.md`.

Then drop your ROM next to the search script. A first sanity run on
Super Mario Bros. 1, walking right on 1-1:

```bash
cd genie_search
python3 -u rommage.py search ../smb1.nes \
    --warmup-input-sequence walk_right --warmup-frames 300 \
    --input-sequence walk_right_ingame \
    --trace-live-addrs \
    --sample 2000 --workers 8 \
    --out ./results/smb1
```

On 8 cores this is ~12 minutes end-to-end: ~9 min for the live-address
trace (cached to `results/smb1/live_addrs.json` — only runs once per
ROM+warmup), ~3 min for the search, then the HTML report.

Flag cheat-sheet:

| flag                           | what it does                                                 |
|--------------------------------|--------------------------------------------------------------|
| `--warmup-input-sequence`      | inputs to run before the snapshot (skip the title screen)    |
| `--warmup-frames`              | how many frames to warm up for                               |
| `--input-sequence`             | inputs during the per-candidate rollout                      |
| `--trace-live-addrs`           | pre-stage: drop bytes the game never reads (~8–18× speedup)  |
| `--require-boot-safe`          | also reject anything that breaks the title screen            |
| `--sample N`                   | random sub-sample instead of exhausting the space            |
| `--workers N`                  | parallel processes (default: os.cpu_count())                 |
| `--resume`                     | skip candidates already evaluated (crash-safe; resumable)    |
| `--out DIR`                    | where to write `index.html`, `results.pkl`, `thumbs/`, cache |

Kill the run at any time; re-run with `--resume` and it picks up where
it left off. Open `results/smb1/index.html` to browse ranked survivors.

For a full-ROM sweep, drop `--sample` and expect several hours per 8-core
machine. Use `--value-stride 16` for a coarse pass first, then re-run
without stride on promising address ranges.

## How did we get here

If you want the writeup of *why* the pipeline looks the way it does —
perceptual hashing, cascaded filtering, differential ablation, and the
`multiprocessing.Pool` war story — see [docs/BLOG.md](docs/BLOG.md).

## Subcommands

| command  | what it does                                           |
|----------|--------------------------------------------------------|
| `search` | brute-force over an address range                      |
| `decode` | inspect a letter code, e.g. `rommage.py decode SXIOPO` |
| `encode` | build a letter code from `(addr, value[, compare])`    |

## Project layout

```
rommage/
├── README.md                    ← you are here
├── docs/
│   ├── PORTING.md               ← step-by-step port guide (start here)
│   ├── ARCHITECTURE.md          ← how the pieces fit together
│   ├── VERIFICATION.md          ← how to verify each step works
│   └── NEXT_STEPS.md            ← extension ideas
├── nes-py-fork/
│   ├── nes-py-cheats.patch      ← C++ diff to apply to nes-py
│   └── nes-py-cheats.README.md
└── genie_search/
    ├── rommage.py               ← command-line entry point
    ├── genie.py                 ← Game Genie codec
    ├── cheat_env.py             ← Python wrapper over patched nes-py
    ├── runner.py                ← rollout runner
    ├── scorer.py                ← dHash + histogram distance
    ├── search.py                ← trace + two-stage parallel search
    ├── report.py                ← HTML gallery
    ├── analyze.py               ← post-hoc clustering / boot-safety
    └── test_*.py                ← codec, cheat-env, and ROM tests
```

## Caveats

- **NROM only** (mapper 0) in v1. Covers SMB1, Donkey Kong, Balloon Fight,
  Ice Climber, Excitebike, Mario Bros, Clu Clu Land, Popeye. UxROM/MMC1/MMC3
  are on the [roadmap](docs/NEXT_STEPS.md).
- **numpy 2.x** requires a two-line patch to nes-py's `_rom.py`
  (see `docs/PORTING.md`).
- **Speed is environment-dependent.** A normal desktop core should push
  3000+ NES frames/sec through nes-py; virtualised sandboxes can drop an
  order of magnitude. Run the bundled speed test first.

## Attribution

Built on top of [nes-py](https://github.com/Kautenja/nes-py) with a small
C++ patch that exposes a Game Genie cheat table intercepting CPU reads at
$8000–$FFFF. Patch and rebuild instructions live in `nes-py-fork/`.
