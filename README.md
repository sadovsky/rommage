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

For a mechanics-level walkthrough aimed at reimplementing the filter
chain (exact dHash construction, histogram quantization, threshold
tuning, cluster binning), see [docs/PIPELINE.md](docs/PIPELINE.md).

## Subcommands

| command  | what it does                                           |
|----------|--------------------------------------------------------|
| `search` | brute-force over an address range                      |
| `report` | (re)render `index.html` from existing results          |
| `decode` | inspect a letter code, e.g. `rommage.py decode SXIOPO` |
| `encode` | build a letter code from `(addr, value[, compare])`    |

Both `report` and `analyze.py` work on a run that's still in progress —
they'll read `results.partial.pkl` when `results.pkl` doesn't exist yet,
so you can preview what's survived so far without waiting or re-running:

```bash
# Fresh HTML gallery from whatever's on disk so far:
python3 rommage.py report --out ./results/smb1

# Clustered percentile ranking of stage-2 survivors:
python3 analyze.py ./results/smb1
```

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

## Favourite found codes

A few Game Genie codes for **Super Mario Bros. 1** (NES) that the search
has turned up and that are weird enough to keep:

| code       | address           | effect                                                           |
|------------|-------------------|------------------------------------------------------------------|
| `KAAYPYSA` | `$F701 $85→$84`   | weird audio — STA zp → STY zp in the sound-channel init loop     |
| `VSZAPEPZ` | `$80A1 $29→$D6`   | dynamic level generation — AND #$E7 → DEC $E7,X in the NMI path  |
| `AELUNLAL` | `$BBBF $30→$00`   | a hammer falls on you after a block hit — object-timer cap → 0   |

What the disassembly says they're doing:

- **`KAAYPYSA`** — `$F701` sits inside a table-driven sound-channel setup
  that copies bytes from `$F90C,Y` into zero-page sound pointers
  (`$F0`, `$F5`, `$F6`, `$F8`, `$F9`). The flip turns `STA $F5` into
  `STY $F5`, so one pointer byte gets clobbered with the current Y-index
  instead of the table value. Music and SFX come out scrambled.
- **`VSZAPEPZ`** — `$80A1` is the `AND #$E7` that masks the PPUMASK shadow
  (`$0779`) right before `STA $2001` in the NMI prologue. Replacing it
  with `DEC $E7,X` decrements a different zero-page byte every frame
  (leaving A stale for the `STA $2001`), which perturbs whatever state
  lives at `$E7,X` — in practice that corrupts level-generator bookkeeping,
  and terrain / object spawns keep mutating as you play.
- **`AELUNLAL`** — `$BBBF` is the `#$30` immediate operand of the
  `CMP #$30` at `$BBBE`, inside the per-slot timer loop that increments
  `$2A,X` and, on match, resets it and jumps into a spawn path at
  `$BBF4`. Lowering the cap to `#$00` makes the compare fire almost
  immediately after a block bump, so the object path keeps triggering —
  which in this case materialises as a hammer raining down on Mario.

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
