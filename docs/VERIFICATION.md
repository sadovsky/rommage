# Verification checklist

One-pager of what "working" looks like at each layer. Use this to debug
a broken port without reading the full porting guide.

## 1. Codec — pure Python, no deps

```bash
cd genie_search
python test_genie.py && python test_canonical.py
```

**Pass signal:**
```
PASS: 47 round-trips OK
ALL PASS

OK   GOSSIP    -> cpu=$D1DD val=$14 cmp=None
OK   ZEXPYGLA  -> cpu=$94A7 val=$02 cmp=$03
OK   SXIOPO    -> cpu=$91D9 val=$AD cmp=None
Round-trip: 120/120
ALL PASS
```

**If this fails:** bundle is corrupt. Re-copy `genie.py`.

## 2. Stock nes-py imports

```bash
python -c "from nes_py import NESEnv; print('ok')"
```

**Pass signal:** `ok`

**If it fails with `OverflowError`:** apply the two-line `_rom.py` numpy-2
fix from `PORTING.md` Milestone 0.

## 3. Patched symbols exported

```bash
python -c "
from nes_py.nes_env import _LIB
for s in ['AddCheat','RemoveCheat','ClearCheats','CheatCount']:
    print(s, hasattr(_LIB, s))
"
```

**Pass signal:** all four print `True`.

**If any print `False`:**
- Rebuild didn't happen — `cd nes-py/nes_py/nes && scons`.
- Rebuild happened but the `.so` didn't replace the installed one —
  find the real path with
  `python -c "import nes_py, os; print(os.path.dirname(nes_py.__file__))"`
  and `cp` manually. On Python 3.12 the installed name is
  `lib_nes_env.cpython-312-x86_64-linux-gnu.so` (or similar), NOT
  `lib_nes_env.so`.

## 4. End-to-end cheat interception

```bash
cd genie_search
python test_cheat_env.py
```

**Pass signal:**
```
Test 1: baseline, no cheats          OK
Test 2: 6-letter cheat ($8010 -> 0x99) OK
Test 3: 8-letter cheat with matching compare OK
Test 4: 8-letter cheat with wrong compare (should NOT fire) OK
Test 5: add + remove + verify baseline is restored OK
ALL TESTS PASS
```

**If test 1 fails:** stock nes-py isn't loading — Milestone 0 broken.

**If test 2 fails:** cheat symbols exist but aren't reaching the bus.
Likely: the `.so` on disk is the newly built one, but Python is loading a
cached pyc that imports the old path. Try `find <site-packages>/nes_py
-name '*.pyc' -delete`.

**If tests 3 or 4 fail:** compare semantics broke. Open
`nes_py/nes/include/cheat.hpp`, find `CheatTable::lookup`, verify the
condition is `e.compare < 0 || e.compare == original_byte`.

## 5. Speed sanity check

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
print(f'{N/dt:,.0f} fps')
"
```

**Pass signal:** 1000+ fps on a developer laptop; 3000+ on a decent desktop.

**If under 500 fps:** you're in a constrained environment (VM, container
with limited CPU allocation, Rosetta emulation on Apple Silicon running
x86 Python, etc.). The search still works, just budget more time.

## 6. Full pipeline

```bash
python rommage.py search test_rom.nes \
    --input-sequence idle --addr-range 0x8000-0x801F \
    --value-stride 32 --stage1-frames 20 --stage2-frames 60 \
    --no-8letter --workers 1 --out ./smoke
```

**Pass signal:**
```
searching test_rom.nes...
Enumerated 236 candidates
  progress: 50/236 (21.2%)
  ...
Searched 236 candidates in Ns (Xk/s); 0 passed stage-1 filter
ranked 0 interesting candidates
wrote report: smoke/index.html
```

Zero passers is **correct** — the synthetic ROM doesn't render RAM to
screen, so cheats can't change the framebuffer.

**If `Enumerated 0 candidates`:** the addr-range is wrong for this ROM.
**If the pipeline crashes mid-search:** check the per-candidate thumbnail
directory permissions — it defaults to `./thumbs/` relative to the CWD.

Open `smoke/index.html` in a browser. You should see a dark page with the
baseline frame and "0 ranked as interesting."

## 7. Real-ROM smoke test

Run against a real NROM ROM you own. Expected signal: some codes in the
`interesting` bucket appear in the top-20 with nonzero `hist` and `ham`
values.

```bash
python rommage.py search mygame.nes \
    --input-sequence press_start \
    --sample 500 --workers 8 \
    --out ./real_test
```

**Pass signal:**
```
searching mygame.nes...
Enumerated 500 candidates
...
ranked N interesting candidates
  XXXXXX  $ABCD:=42  bucket=interesting    hist=0.123  ham=12.5
  ...
```

The top candidates should have `bucket=interesting` (not `null` or
`likely_crash`) and visibly different thumbnails from the baseline in
the HTML report.

**If everything is `null`:** the input sequence isn't getting past the
title screen. Try `--warmup-frames 240 --input-sequence press_start`.

**If everything is `likely_crash`:** the game's baseline is very dynamic
(lots of motion) so even no-op changes produce high distances, crossing
the crash threshold. Edit `scorer.py` — bump `CRASH_HAMMING_MIN` from 40
to 50 and `CRASH_HIST_MIN` from 1.2 to 1.5.
