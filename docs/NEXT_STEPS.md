# Next steps

Things worth building on top of the v1 pipeline, roughly ordered by impact.

## 1. Mapper support (HIGH value, MEDIUM effort)

v1 is NROM-only. To search Punch-Out!! (UxROM), Mega Man (MMC1), SMB3
(MMC3), etc., the enumeration in `search.py:enumerate_candidates` needs
to handle banking.

**What changes:**

- `rom_byte_at(prg, cpu_addr)` currently assumes a single NROM bank. For
  banked mappers, a given CPU address can read bytes from *many* ROM
  banks depending on which one is currently mapped in.
- For 8-letter codes, the purpose of the compare byte is to disambiguate
  which bank the code applies to — the code only fires in banks where
  the compare byte matches. So enumeration should emit one candidate
  per distinct `(addr, value, compare)` where `compare` is a byte that
  actually exists at some bank's copy of `addr`.
- For 6-letter codes, banking introduces risk — the patch fires in
  every bank, which on mapped games can crash bank-switching code. Most
  real 6-letter codes on mapped games target the fixed bank at `$C000-$FFFF`
  (where applicable) for this reason. Worth emitting 6-letter candidates
  only when `addr >= $C000` and the mapper has a fixed final bank.

**Code sketch:**

```python
# In search.py
def enumerate_candidates_mapped(rom_path, ...):
    rom = parse_ines(rom_path)
    banks = split_into_prg_banks(rom)  # list of 8KB or 16KB chunks
    mapper = rom.mapper

    for cpu_addr in range(0x8000, 0x10000):
        # Get the set of possible bytes at this addr across all banks
        compares = set()
        for bank in banks_mapped_to(mapper, cpu_addr, banks):
            compares.add(bank[bank_offset(cpu_addr)])
        for cmp in compares:
            for val in value_range:
                if val != cmp:
                    yield GenieCode(cpu_addr & 0x7FFF, val, cmp)
```

Reference: https://www.nesdev.org/wiki/Mapper

Per-mapper logic lives in `nes_py/nes/src/mappers/mapper_*.cpp` in the
fork — good reference for which banks map where at a given CPU address.

## 2. RAM-watch scoring as a complementary signal (HIGH value, LOW effort)

The visual scorer catches "something visible changed." But for known-layout
games, "the lives counter went up" is a cleaner signal. For each candidate,
compare `env.ram[known_addrs]` after the rollout to baseline.

**Interface sketch:**

```python
# scorer.py extension
@dataclass
class RamScorer:
    watch_addrs: list[int]  # e.g., [0x075A] for SMB1 lives
    expected_directions: dict[int, str]  # 'up', 'down', 'change'

    def score(self, baseline_ram, candidate_ram) -> dict:
        for addr in self.watch_addrs:
            b, c = baseline_ram[addr], candidate_ram[addr]
            ...
```

Then `search.py` runs both scorers and ranks on combined signal. Great for
surfacing "infinite lives," "always have item X," "start with max health"
codes that may look visually subtle but have obvious RAM impact.

Alex's existing Punch-Out!! RAM map is literally already the input data
for this.

## 3. Input-sequence recording from human play (MEDIUM value, LOW effort)

Hardcoded `idle` / `press_start` / `walk_right` sequences miss codes that
only fire under specific game states. A recorded replay of 30 seconds of
human play as the input sequence reveals *way* more.

**Implementation:** use nes-py's `play_human.py` with controller capture
enabled, serialize the action-per-frame list to JSON, load as an input
sequence. Takes 2 hours to build and makes the search dramatically more
useful per candidate.

## 4. Disassembly-guided candidate prioritization (MEDIUM-HIGH value, HIGH effort)

Most interesting Game Genie codes target immediate-value loads in the
6502 disassembly — e.g., `LDA #$03` loading a constant 3 (starting lives),
where patching the `#$03` operand changes game behavior cleanly. Addresses
that are NOT immediate-value operand bytes are much more likely to cause
crashes.

A coarse disassembly pass over the ROM (even just "which bytes are operands
of `LDA #`, `LDX #`, `LDY #`, `CMP #`?") lets you prioritize high-signal
addresses first. Cuts effective search space 10-50x.

Libraries: `py65` for 6502 disassembly; `nesrom` on PyPI; FCEUX's trace
dumps.

## 5. Checkpoint / resume (LOW-MEDIUM value, LOW effort)

Long searches should be resumable. Add to `search.py`:

```python
# Append each CandidateResult as a JSON line to results.jsonl, not pickle
# at the end. On resume, read the file, skip candidates whose code_str
# is already present.
```

`results.pkl` → `results.jsonl`, main loop checks a set of completed
codes before dispatching to workers. Enables "run overnight, stop, resume
tomorrow with more workers" workflows.

## 6. Emulator speed optimizations (VARIABLE value, MEDIUM effort)

If your speed check in Milestone 4 shows <1000 fps, the bottleneck is
likely the Python-C ctypes boundary being called once per frame. Options:

- **Batch `step`**: add a C function `StepN(emu, n_frames)` to the fork
  that runs N frames in one ctypes call. 10-50x speedup for pure noop
  rollouts.
- **Drop the gym wrapper**: `nes_py.NESEnv.step()` has gym-compatibility
  overhead. Call `_LIB.Step(self._env)` directly for hot paths.
- **Pin the emulator to a CPU**: `os.sched_setaffinity` in the worker
  initializer, one core per worker. Reduces cache thrashing on many-core
  boxes.

`speedtest.py` in the nes-py repo gives you a clean benchmark to iterate
against.

## 7. Smarter distance metrics (LOW value, HIGH effort)

- **SSIM** for structural similarity — more robust than dHash to
  illumination changes, ignored by pixel-shift. Slow.
- **Edge-aware perceptual distance** — compute Sobel edges first, then
  compare. Catches UI changes (HUD, menus) that dHash/histogram miss.
- **Learned embeddings** — train a tiny autoencoder on random NES frames,
  use latent distance. Overkill unless you're searching thousands of ROMs.

## 8. Web UI (LOW value, MEDIUM effort)

The HTML report is static. A Flask/FastAPI server that streams results as
they come in, lets you filter by bucket, and shows a video clip per
candidate (not just a thumbnail) would be a much nicer exploration
experience — especially on large searches where you want to see 200+
candidates and compare.

## 9. Multi-ROM search (LOW value, LOW effort)

`genie search --rom-dir ./roms/ --code-dir ./results/` — run the same
search config over a directory of ROMs, write one subdirectory of results
per ROM. Useful if you want to find codes for the whole library of
NROM games overnight.

## 10. Genetic search instead of brute force (SPECULATIVE, HIGH effort)

Most brute-force time is wasted on adjacent candidates that do the same
thing. A GA over `(addr, value, compare)` tuples with the visual score as
fitness might surface interesting codes faster. Not clear this beats
random sampling for this problem, but worth experimenting with once the
baseline brute-force is too slow to iterate on.

---

## Order of operations I'd recommend

1. Get v1 running on a real NROM game (SMB1) — a few hours
2. Add RAM-watch scoring with a single known address (SMB1 lives: `$075A`) — an afternoon
3. Record a human play as input sequence — an afternoon
4. Add checkpoint/resume — a couple hours
5. Add UxROM support, test on Punch-Out!! — a weekend

After that the bang-per-buck drops sharply and it's more about where your
curiosity takes you.
