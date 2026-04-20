# Porting guide

This guide walks through getting the Game Genie search pipeline running on
a fresh machine via Claude Code. The work is broken into milestones with
explicit verification at each step, so a failure tells you which box to
open.

Intended reader: a Claude Code session (or a human shepherding one).

---

## Prerequisites

- Linux or macOS (Windows works via WSL; native Windows untested)
- Python 3.10+
- A C++ toolchain (`g++` on Linux, Xcode command-line tools on macOS)
- `git`, `pip`, `scons`

Install missing bits:

```bash
# Linux
sudo apt install build-essential git python3-pip

# macOS
xcode-select --install
brew install scons
```

---

## Milestone 0 — Baseline nes-py working

Before touching the fork, confirm stock nes-py builds and runs. Skipping
this step will make later failures ambiguous.

```bash
pip install nes-py
python -c "from nes_py import NESEnv; print('ok')"
```

If the import fails with `OverflowError: Python integer 1024 out of bounds for uint8`,
apply the numpy-2 fix:

**File to patch:** `<site-packages>/nes_py/_rom.py`

Find these two properties and wrap `self.header[4]` / `self.header[5]` with `int()`:

```python
@property
def prg_rom_size(self):
    """Return the size of the PRG ROM in KB."""
    return 16 * int(self.header[4])   # ← was: 16 * self.header[4]

@property
def chr_rom_size(self):
    """Return the size of the CHR ROM in KB."""
    return 8 * int(self.header[5])    # ← was: 8 * self.header[5]
```

Re-run the import; it should succeed silently (you'll see a one-time gym
deprecation warning — that's fine).

**Verify:**

```bash
python -c "from nes_py import NESEnv; print('ok')"
# → ok
```

---

## Milestone 1 — Apply the C++ fork

The stock nes-py has no mechanism to intercept CPU reads from ROM, which is
what a Game Genie actually does. The fork adds a `CheatTable` to `MainBus`
and exports four new ctypes symbols: `AddCheat`, `RemoveCheat`, `ClearCheats`,
`CheatCount`.

### Steps

```bash
# 1. Clone upstream nes-py
git clone https://github.com/Kautenja/nes-py.git
cd nes-py

# 2. Apply the patch
git apply /path/to/bundle/nes-py-fork/nes-py-cheats.patch

# 3. Rebuild the native lib
cd nes_py/nes
scons
cd ../..

# 4. Copy the built .so over the pip-installed one
SITE=$(python -c "import nes_py, os; print(os.path.dirname(nes_py.__file__))")
cp nes_py/lib_nes_env.so "$SITE/"
# The installed file may be named lib_nes_env.cpython-3XX-xxx.so; copy
# overwriting that too:
cp nes_py/lib_nes_env.so "$SITE/"/lib_nes_env.cpython-*.so 2>/dev/null || true
```

**What the patch changes** (6 files, ~170 lines):

- `nes_py/nes/include/cheat.hpp` (new) — `CheatTable` class
- `nes_py/nes/include/main_bus.hpp` — holds a `shared_ptr<CheatTable>`
- `nes_py/nes/include/emulator.hpp` — adds `add_cheat`/`remove_cheat`/`clear_cheats`
- `nes_py/nes/src/main_bus.cpp` — consults the table in the `$8000+` branch of `read()`
- `nes_py/nes/src/emulator.cpp` — wires the shared table into both live and backup buses
- `nes_py/nes/src/lib_nes_env.cpp` — exports the four ctypes symbols

See `nes-py-fork/nes-py-cheats.README.md` for full architectural notes.

### Verify

```bash
python -c "
from nes_py.nes_env import _LIB
for sym in ['AddCheat','RemoveCheat','ClearCheats','CheatCount']:
    assert hasattr(_LIB, sym), f'missing {sym}'
print('all four cheat symbols present')
"
# → all four cheat symbols present
```

---

## Milestone 2 — Drop the Python package in

```bash
# Your working directory can be anywhere
cp -r /path/to/bundle/genie_search ./
cd genie_search
```

No install step — it's flat Python files with no package layout. Add it to
`PYTHONPATH` if you prefer:

```bash
export PYTHONPATH="$PWD:$PYTHONPATH"
```

### Verify the codec

```bash
python test_genie.py
# → PASS: 47 round-trips OK
# → ALL PASS

python test_canonical.py
# → OK   GOSSIP    -> cpu=$D1DD val=$14 cmp=None
# → OK   ZEXPYGLA  -> cpu=$94A7 val=$02 cmp=$03
# → OK   SXIOPO    -> cpu=$91D9 val=$AD cmp=None
# → Round-trip: 120/120
# → ALL PASS
```

If this fails, the bundle copy is incomplete — nothing downstream.

---

## Milestone 3 — Verify the cheat engine works

The synthetic ROM (`test_rom.nes`) is a 6502 program that reads from `$8010`
every frame and stores the result in RAM. By applying a cheat to `$8010` and
checking RAM, we prove end-to-end that CPU reads are being intercepted.

```bash
python test_cheat_env.py
```

Expected output:

```
Test 1: baseline, no cheats
  RAM[0x0200]=0x42  RAM[0x0201]=0xAA
  OK
Test 2: 6-letter cheat ($8010 -> 0x99)
  RAM[0x0200]=0x42  RAM[0x0201]=0x99
  OK
Test 3: 8-letter cheat with matching compare ($8010 -> 0x77 if 0xAA)
  RAM[0x0200]=0x42  RAM[0x0201]=0x77
  OK
Test 4: 8-letter cheat with wrong compare (should NOT fire)
  RAM[0x0200]=0x42  RAM[0x0201]=0xAA
  OK
Test 5: add + remove + verify baseline is restored
  OK
ALL TESTS PASS
```

**If test 1 fails** — stock nes-py isn't loading (Milestone 0 broken).
**If test 2 fails** — `AddCheat` symbol exists but isn't wired to the bus
(Milestone 1 broken; probably rebuild didn't pick up the new `.so`).
**If tests 3-4 fail** — compare semantics bug; file an issue referencing
`test_cheat_env.py` output.

---

## Milestone 4 — Speed check

Before running any real search, find out how fast rollouts actually are on
your hardware. The whole brute-force plan depends on this number.

```bash
python -c "
import contextlib, io, time
with contextlib.redirect_stderr(io.StringIO()):
    from cheat_env import CheatNESEnv

env = CheatNESEnv('test_rom.nes')
env.reset()
N = 10000
t0 = time.perf_counter()
for _ in range(N):
    env.step(0)
dt = time.perf_counter() - t0
print(f'{N} frames in {dt:.2f}s = {N/dt:,.0f} fps')
env.close()
"
```

Expected: 2000–5000 fps on modern hardware, single-threaded. If you see
<1000 fps you're in a constrained environment (VM, container, Rosetta,
shared host) and the search will still work but take longer than the
back-of-envelope estimates assume.

---

## Milestone 5 — CLI sanity check

```bash
python rommage.py decode GOSSIP
# → GOSSIP: CPU=$D1DD value=$14

python rommage.py encode --addr 0x91D9 --value 0xAD
# → SXIOPO

python rommage.py search test_rom.nes \
    --input-sequence idle \
    --addr-range 0x8000-0x801F \
    --value-stride 32 \
    --stage1-frames 20 --stage2-frames 60 \
    --no-8letter --workers 1 \
    --out ./smoke_test_out

# Expected:
# searching test_rom.nes...
# Enumerated 236 candidates
#   progress: 50/236 (21.2%)
#   ...
# Searched 236 candidates in Ns (Xk/s); 0 passed stage-1 filter
# wrote report: smoke_test_out/index.html
```

Zero passers is **correct** — the synthetic ROM doesn't render RAM to screen,
so no cheat can change the framebuffer. The purpose of this milestone is
purely to exercise the full pipeline.

Open `smoke_test_out/index.html` in a browser — you'll see a dark-themed
page with the baseline frame and "0 ranked as interesting." That confirms
the report generator works.

---

## Milestone 6 — Real ROM

Use a ROM you own (or one from the NES homebrew scene, like STREEMERZ).

```bash
python rommage.py search your_rom.nes \
    --input-sequence press_start \
    --stage1-frames 60 --stage2-frames 300 \
    --sample 2000 \
    --workers 8 \
    --out ./results/mygame
```

Tuning knobs, in rough order of impact on results quality:

- `--input-sequence` (`idle`, `press_start`, `walk_right`, `random`) —
  the action pattern during rollouts. If you're testing a platformer,
  `walk_right` reveals more codes than `idle`.
- `--stage2-frames` — longer rollouts catch codes whose effects manifest
  slowly (invincibility only shows up when you get hit).
- `--sample` — random subsample of candidates. Start with 2000 to sanity-check
  your tuning, then scale up. Full search is `~8M` candidates; omit for a
  full run.
- `--value-stride` — skip values. Stride 16 means only try values
  `0, 16, 32, ..., 240`. Good for initial sweeps; narrow down once you see
  which addresses matter.
- `--workers` — parallel processes. Set to `os.cpu_count()`.

---

## Troubleshooting

**`CheatsNotSupportedError`** — the `.so` swap in Milestone 1 didn't take.
Check: `python -c "from nes_py.nes_env import _LIB; print(hasattr(_LIB, 'AddCheat'))"`.
If `False`, your `lib_nes_env.cpython-*.so` in site-packages is still the
stock one. `find <site-packages>/nes_py -name '*.so'` to locate, then
`cp` over it.

**`OverflowError: Python integer 1024 out of bounds for uint8`** —
numpy 2 compatibility; apply the two-line `_rom.py` fix from Milestone 0.

**Results are all `null` on a real ROM** — the input sequence isn't reaching
the actual game. Try `--input-sequence press_start` with more `pre` frames,
or use `--warmup-frames 180` to run past the title screen before the
baseline snapshot is taken.

**Results are all `likely_crash`** — the crash threshold is too low for
your baseline. Look at `scorer.py` — `CRASH_HAMMING_MIN` and `CRASH_HIST_MIN`
are tuned for typical games and may need bumping if your baseline is very
static.

**Slow, even though speed-check was fast** — if you're doing a large search,
check that `--workers` is set (`--workers 8` not `--workers 1`). The default
is 0 which expands to `os.cpu_count()`, but if you forget and pass
`--workers 1` on the command line, you're single-threaded.

---

## What's next

See [`NEXT_STEPS.md`](NEXT_STEPS.md).
