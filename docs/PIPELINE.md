# The filtering pipeline, in detail

A companion to [BLOG.md](BLOG.md), which motivates the approach.
This document is a mechanics-focused walkthrough of what actually
happens to a candidate as it flows through the pipeline — enough detail
to reimplement the system from scratch on a different platform (Game
Boy, SMS, anything with a patchable memory bus).

We work through the funnel in order:

```
16M raw candidates
  ─(6/8-letter split + 8-letter compare pinning)→  ~4M
  ─(live-address trace)                         →  ~230k  (SMB1, walk-right)
  ─(enumeration over live addrs × values)       →  ~912k  (both code lengths)
  ─(boot-safety, optional)                      →  slightly fewer
  ─(stage 1: short rollout, dHash-only)         →  ~1–10%  survive
  ─(stage 2: full rollout, dHash + histograms)  →  all scored
  ─(rank + bucket)                              →  index.html
  ─(percentile cluster)                         →  clustered.html
```

## 0. What a candidate is

A Game Genie code decodes to a triple `(cpu_address, value, compare)`:

- `cpu_address`: a 15-bit address in `0x8000–0xFFFF` (the cartridge
  region on a 6502 NES).
- `value`: the replacement byte (`0x00–0xFF`).
- `compare`: optional. If present (8-letter codes), the cheat only fires
  when the CPU read at `cpu_address` would have returned `compare`.
  Without it (6-letter codes), it fires unconditionally.

Our job: for each such triple, decide whether the resulting game
behaves *interestingly* — not identical to vanilla (a "null" code) and
not a crash.

## 1. Full space and the compare-pinning trick

Raw enumeration:

- `2¹⁵` addresses × `2⁸` values × `{6-letter, 8-letter}` = **~16.8M** codes.
- The 8-letter half naively contributes 256× per 6-letter entry
  (one for each possible `compare`), bloating the count.

**Compare pinning**: for an 8-letter code to do anything on an NROM
cart, `compare` must equal the real PRG byte at `cpu_address`.
Every other value produces a silent no-op (the compare mismatch
short-circuits the cheat in hardware). So we read the PRG ROM once in
`read_prg_bytes`, and when emitting an 8-letter candidate we pin
`compare = prg[cpu_address - 0x8000]`. This collapses the 8-letter
space back down to the same size as the 6-letter space — roughly **~8M
non-degenerate candidates** total before any game-specific filtering.

A 16KB NROM ROM is mirrored into the upper 16KB; we fold with
`offset &= 0x3FFF`. Bigger mappers (MMC1, UxROM) would need a bank-aware
read here.

## 2. Live-address trace: differential ablation

This is the most impactful filter and the only one that's per-ROM +
per-play-sequence. The intuition: if the CPU never reads the byte at
`$8142` during a walk-right rollout, any patch to `$8142` is a no-op
by construction. Skip the address entirely.

Procedure, for each of the 32,768 PRG addresses:

1. Read the original PRG byte `orig = prg[addr]`.
2. Construct a perturbed byte: `probe = orig ^ 0xFF`, falling back to
   `orig ^ 0x55` in the degenerate case `orig == 0xFF`. We just need
   *some* value the CPU would notice if it read it.
3. Apply the cheat as a 6-letter code (no compare — unconditional
   substitution), run a short rollout (`--trace-probe-frames`, default
   60), capture every `capture_every` frames.
4. Compute the dHash of each captured frame; compare to the no-cheat
   baseline's dHashes for the same frame indices.
5. If `max(hamming(base, cand)) > 0` anywhere in the stack, the
   address is **live**. Otherwise it's dead.

Implementation: `trace_live_addresses` in `search.py:494`. Workers
are the same raw-Process + Queue pool as the main search. Results are
cached to `live_addrs.json` (keyed by ROM + warmup — delete the file
to re-run).

Conservative safety net: if the watchdog has to SIGKILL workers
mid-trace, any addresses that didn't produce a result are assumed
live. We'd rather re-evaluate a dead address than miss a real code.

**SMB1 numbers**: with the `walk_right` warmup + `walk_right_ingame`
rollout, 1,789 of 32,768 addresses are live (~5.5%). The trace itself
takes ~9 minutes on 8 cores at ~60 probes/sec/core. Amortized over the
subsequent search it's a rounding error.

**Why this works at all**: on an NROM cartridge, the PRG ROM is mostly
code + static tables + unused space. In a given ~300-frame window, only
a narrow execution path actually runs, so the vast majority of bytes
are never fetched. Changing them is physically undetectable.

## 3. Candidate enumeration

`enumerate_candidates` walks `effective_addr_range × value_range`, emits
both a 6-letter code and an 8-letter code (with pinned `compare`) per
(addr, value) pair, optionally sub-samples, and returns the list.

At this point we have a fixed, ordered list of `GenieCode` triples.
Each candidate pickle is ~80 bytes. The list itself fits comfortably in
memory on the main process.

## 4. Stage 0: boot-safety (optional)

Problem: some codes look fine in the middle of a rollout but kill
the title screen on power-on. If the code only "works" from a
post-warmup save-state, it isn't a usable cheat on real hardware.

Check, per candidate (opt-in via `--require-boot-safe`):

1. Hard-reset the emulator (bypass the shared warmup backup).
2. Clear cheats, add the candidate.
3. Step through `--boot-check-frames` frames (default 60) of the
   warmup input sequence.
4. Capture the last frame; compute its dHash and 64-bin histogram.
5. Accept if `hamming ≤ 8` AND `hist_L1 ≤ 0.12` vs the no-cheat
   title-screen baseline.

A failure short-circuits: `boot_safe = False`, stage 1 and 2 are
skipped, we move on. Thresholds are generous because some legitimate
codes *do* alter the HUD or palette on boot without being "broken."

## 5. Stage 1: the fast-reject filter

Every surviving candidate gets a short rollout here. This is the main
throughput bottleneck, so every per-candidate op matters.

Per-candidate work:

1. `runner._restart()` — if a warmup backup exists, restore from it
   (a few memcpys of emulator state). Else cold-reset. No full
   re-emulation of the title screen.
2. `env.clear_cheats()` + `env.add_cheat(candidate)`. The C++ patch
   exposes a cheat table that intercepts reads in `$8000–$FFFF`.
3. Step `stage1_frames` frames of the input sequence, capturing
   every `capture_every` frames into a uint8 `(N, 240, 256, 3)`
   array. Typical defaults produce `N ≈ 4–20` captures.
4. `dhash_stack(frames)` → `(N,) uint64`.
5. `hamming_stack(s1_base_hashes[:n], cand_hashes[:n])` → `(N,) int`.
6. Record `hamming_max` (worst-case bit flip) and `hamming_mean`.

Accept into stage 2 iff:

```
hamming_max  > NULL_HAMMING_MAX   (default 3)
hamming_mean < CRASH_HAMMING_MIN  (default 40, out of 64)
```

The first condition kills nulls: no single frame ever drifted beyond a
few bits, so the code did nothing observable. The second kills
crashes: the screen averaged ≥40 of 64 bits different from baseline
across captures, which is the signature of PPU garbage or a solid-color
lockup.

On SMB1 the stage-1 filter typically passes **1–10%** of candidates,
depending on the input sequence. Lower passes = more crashes, more
nulls, or both. This is the single biggest speedup in the pipeline
after the live-address trace.

### dHash, in detail

`_dhash_frame` in `scorer.py:32`. For each 240×256×3 frame:

1. Collapse RGB to luminance: `gray = frame.mean(axis=2).astype(float32)`.
2. Block-average down to `(8, 9)` — we pick a 9-column width to produce
   8 gradient bits per row via "pixel to the right > pixel to the
   left." Implementation uses a `(8, 30, 9, 28).mean(axis=(1,3))`
   reshape, which is the cheapest downsampler that stays honest (no
   aliasing from strided sampling).
3. Compute `diff = small[:, 1:] > small[:, :-1]` → `(8, 8)` booleans.
4. Pack the 64 bits into a `np.uint64`.

Properties we care about:

- **Cheap**: ~30 μs per frame in pure numpy on a modern desktop. For
  a 20-frame capture stack, hashing is ~0.6 ms — negligible next to
  the rollout itself.
- **Translation-tolerant**: dHash compares adjacent luminance values
  after aggressive downsampling, so a 1-pixel scroll rarely flips a
  single bit.
- **Brightness-invariant**: because we only record *whether* left <
  right at each cell, a uniform intensity shift doesn't matter.
- **Comparison is a 64-bit XOR + popcount**: `bin(int(x)).count("1")`
  is adequate for our scale. If you need more throughput, use
  `numpy.unpackbits` and sum, or a compiled popcount.

Calibration: the `NULL_HAMMING_MAX=3` threshold comes from observing
that two consecutive no-cheat rollouts of SMB1 sometimes differ by
1–3 bits on the NTSC odd/even frame fence-post. Zero would false-
reject valid codes on that boundary; 3 keeps a small safety margin.

`CRASH_HAMMING_MIN=40` is empirical: PPU-garbage screens are typically
near 50% bits different (i.e. ~32 hamming) but noisy, so we require
a decisive mean to call it a crash. Re-tune per ROM if needed.

## 6. Stage 2: the deep evaluation

Only survivors of stage 1 reach here. They get:

1. Another rollout, `stage2_frames` long (default 300). Typically
   5× as long as stage 1. Same capture cadence, so ~20 captured
   frames.
2. `score_frames()` — computes the dHash stack **and** the 64-bin
   color histogram stack, with L1 distance to baseline for both.
3. Assigns a `bucket`: `null | interesting | likely_crash` based on
   four thresholds on `(hamming_mean, hamming_max, hist_mean,
   hist_max)`. Stage 1 already filtered the obvious cases; this is
   a belt-and-suspenders recheck over a longer window.
4. Saves a thumbnail: the middle captured frame as PNG under
   `$GENIE_THUMB_DIR/thumb_<code>.png`.

### Color histograms, in detail

`_quantize` in `scorer.py:75`:

```
q = (frame >> 6).astype(int32)           # 0..3 per channel
idx = (q[...,0] << 4) | (q[...,1] << 2) | q[...,2]   # 0..63
```

Right-shift by 6 keeps the top 2 bits of each 8-bit channel — a
4×4×4 palette cube, one 0..63 index per pixel. `np.bincount` gives us
the 64-bin distribution; we L1-normalize so it's a probability
distribution.

Why not just use dHash? dHash is a **structural** signal — "is the
layout the same?" It misses:

- Palette swaps (same shapes, different colors).
- Tinted whole screens (e.g. a code that corrupts the universal
  background color).
- HUD digit changes.

Histograms catch all of those. `hist_L1` ranges from 0 (identical
distribution) to 2.0 (no pixel in either distribution shares a bin).
Values above ~1.2 are typically crashes; 0.05–1.0 is the "interesting"
band. The two metrics are complementary: a code that changes structure
without changing colors registers on dHash only, and vice versa.

Computing a 64-bin histogram for a 240×256×3 frame is ~200 μs in
numpy via `(q[...,0]<<4 | q[...,1]<<2 | q[...,2]).ravel()` +
`bincount(minlength=64)`. Cheap, but *not* cheap enough to run in
stage 1 — 2× the cost would halve our throughput.

### Combined score for ranking

```python
score = hist_mean + 0.5 * (hamming_mean / 64)
```

The two metrics are on different scales (`hist_mean` ∈ [0, 2],
`hamming_mean` ∈ [0, 64]); we normalize hamming to [0, 1] and weight
it at half. You can tune the weight empirically — `search.py` at
the `rank_interesting` function is the one place to touch.

Null and crash buckets are multiplied by 0.1 when ranking, so they
sink to the bottom of the gallery without being dropped entirely.

## 7. Clustering (analyze.py)

Even after stage 2, many "different" candidates produce visually
identical output. Example: twenty different `(addr, value)` pairs that
all hit the same branch instruction and cause the same
death-to-black-screen transition. Ranking puts them all at the top in
a tied block, burying genuine diversity.

Fix: **bin the two stage-2 distances**, group by bin, pick a
representative per bin:

```python
HIST_BIN = 0.005    # width of histogram-mean bin
HAM_BIN  = 0.5      # width of hamming-mean bin
key = (round(hist_mean / 0.005) * 0.005,
       round(ham_mean  / 0.5) * 0.5)
```

Candidates sharing a key are treated as the same "effect." For each
cluster:

1. Sort members by combined score, descending.
2. Keep the top member as the representative.
3. Keep the rest as siblings (rendered as a collapsible list in
   `clustered.html`).

Ranking the **representatives** by score gives a much more compressed,
diverse gallery. Percentile-bucket them:

- top 5% → `top`
- next 20% → `promising`
- bottom 75% → `noise`

Clusters of size > 1 are a useful signal all by themselves: high-size
clusters often represent robust effects (multiple bytes that trigger
the same behavior), while size-1 clusters are often one-off
perturbations worth investigating.

### Why bin-and-represent, not k-means?

Two reasons:

1. **Scalar thresholds are interpretable**. "Group candidates within
   ±0.005 hist and ±0.5 hamming" has an obvious semantic meaning;
   k-means cluster indices don't.
2. **We don't need a "cluster count" hyperparameter**. The bin widths
   pick out the resolution we care about; the number of clusters falls
   out of the data. This is closer to DBSCAN in spirit, but cheaper
   because our feature space is only 2-D.

You could absolutely substitute k-means, HDBSCAN, or spectral
clustering on the full (hist, hamming, hist_max, hamming_max) feature
vector if you want finer structure. For a brute-force search aimed at
human eyeball, the 2-D binning gets 95% of the benefit for 0% of the
tuning cost.

## 8. Where the thresholds come from

Everything tuned in `scorer.py` lines 125–128:

```python
NULL_HAMMING_MAX = 3
NULL_HIST_MAX    = 0.02
CRASH_HAMMING_MIN = 40
CRASH_HIST_MIN   = 1.2
```

Methodology for re-tuning on a new ROM:

1. Run 10 no-cheat rollouts back-to-back with different RNG seeds.
   Record the max pairwise dHash + hist distances. This is the
   **noise floor** — set `NULL_*` thresholds just above.
2. Apply a code known to crash (e.g. write `0x00` to the reset vector
   at `$FFFC`). Record its distances. Set `CRASH_*` below that.
3. Apply a code known to be *interesting* (e.g. SMB1 infinite lives,
   `SXIOPO`). It should land in the "interesting" bucket.

If those three landmarks are well-separated, your thresholds
generalize. If they aren't, either the capture cadence is wrong
(try decreasing `capture_every`) or the input sequence doesn't
exercise the relevant code path.

## 9. Throughput budget

Typical per-candidate cost at stage 1, `stage1_frames=60`,
`capture_every=15`, on an 8-core desktop:

- Rollout: ~50 ms (dominated by the emulator; cheats are constant-
  time per CPU read).
- dHash 4 frames: ~0.1 ms.
- Hamming compare: ~5 μs.
- Queue round-trip: ~100 μs.

Per-core: ~50 ms → ~20/s. × 8 cores → ~160/s aggregate. In practice we
see 10–15/s per core because stage-2 spends an extra ~250 ms on
survivors. The search.py progress output is the truth; the back-of-
envelope is just to set expectations.

Stage 2 blow-up is the main reason stage 1 has to be strict. If 50%
of candidates pass stage 1, throughput drops by ~3×. On SMB1 with
walk-right, typical pass rates are 1–10%, so stage 2 doesn't
dominate.

## 10. Everything that is not here

Things this pipeline does not do, that a real production clone
might want:

- **RAM tracing** (as opposed to ROM tracing) for games that
  self-modify code or use mappers with bank switching.
- **Motion-aware features** — we compare aligned frames across
  baseline and candidate, not frame-to-frame motion vectors.
  A game whose intro plays at half-speed would register as
  "interesting" even if logically identical.
- **Audio features**. Some codes only affect sound output.
- **Learned features** (CNN embeddings). Would compress further than
  dHash + histograms and likely separate better, but requires a
  labeled corpus we don't have.

None of these are fundamental; they're just beyond v1.
