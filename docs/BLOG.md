# Brute-forcing Nintendo cheat codes, looked at as a search problem

*A data scientist's tour of a silly project that ended up being a good
excuse to think about cascaded classifiers, ablation studies, perceptual
hashing, and the many interesting ways `multiprocessing.Pool` can ruin
your afternoon.*

## The setup

A Game Genie code is a scrambled 6- or 8-letter string that patches a
single byte in an NES ROM at runtime. Decoded, it's a triple:
`(cpu_address, replacement_value, optional_compare_byte)`. When the CPU
reads from `cpu_address`, the cartridge returns `replacement_value`
instead — unconditionally for 6-letter codes, only if the real byte
matches `compare_byte` for 8-letter codes.

In the 90s, finding new codes was pure craft: read the disassembly, guess
what the routine does, patch it, play the game, repeat. In 2026, with a
laptop that does 3000 NES frames/sec and `multiprocessing` (sort of),
we can just try a lot of them.

The naive question is "which patches change the game in interesting ways
instead of crashing it?" Framed like that, it's a **search over
behaviors**, not values. The ROM byte space for a single 16KB NROM
cartridge is ~8M candidates (32K addresses × 256 values, minus no-ops).
For each candidate we need to: apply the patch, run the game, capture
frames, score them. Budget: at ~2 candidates/sec/core, 8 cores, that's
~140 hours for the full space. Sampling and pruning matter.

The rest of this post walks through the pipeline, which ended up being
an interesting little ML-flavored system even though there's no model
being trained.

## Frames as features

The first question is: what does "interesting" mean, numerically?

A candidate rollout is a tensor — 240×256×3 pixels × N captured frames,
say 20 frames at a stride of 15 over a 300-frame rollout. That's a
~3.7MB of raw pixels per candidate, and we're going to evaluate millions
of candidates. No way we're holding the raw stacks; we need dense
features.

Two cheap, complementary ones:

1. **dHash** (difference hash) — a 64-bit perceptual hash per frame. You
   downsample to 9×8, take row-wise greater-than differences, and the
   resulting bit pattern is invariant to smooth intensity shifts. Hamming
   distance between two dHashes is a decent "are these frames
   structurally the same?" metric. Fast to compute, fast to compare
   (single XOR + popcount per pair).
2. **Color histograms** — quantize RGB to 4×4×4 bins, count pixels.
   64-dim L1 distance is our "is the color distribution different?"
   metric. Catches palette-level changes dHash misses (e.g. a palette
   corruption that keeps shapes intact but swaps every blue for red).

Together, per candidate frame stack, we collapse ~74MB of pixels to a
(20,) int64 hash array plus a (20, 64) float32 histogram — a ~7000×
compression that still preserves the distinctions we care about.

This is the boring-but-important part: 90% of the work in any
behavioral-search pipeline is picking features that make your distance
metric meaningful *and* cheap. dHash + quantized histograms are the
perceptual-search version of TF-IDF + cosine — not the fanciest thing
you could do, but tuned to the task they're hard to beat for cost.

## Cascade classifiers, retro edition

Running a 300-frame rollout takes ~0.5s. At 8M candidates, even
perfectly parallelized, that's the entire weekend. So: **most
candidates are not interesting and we should bail on them cheaply.**

This is just Viola-Jones in funny clothes. Ninety-five-plus percent of
random byte patches land in one of two boring buckets:

- **Null** — the patched byte is never actually read during the rollout,
  so the game behaves identically to the baseline. dHash distance to
  baseline ≈ 0.
- **Crash** — the patched byte gets read, the CPU hits an illegal opcode
  stream, the screen goes to a constant color or a garbage pattern.
  dHash distance ≈ 32 (maximal, for 64-bit hashes).

Neither needs a full 300-frame rollout to detect. Stage 1 of the search
runs a 60-frame rollout, computes only the dHash distance (skipping
histograms), and rejects anything whose max-distance is below a null
threshold or whose mean-distance is above a crash threshold.

In practice stage 1 kills ~35% of candidates, and the survivors go to
stage 2: the full 300-frame rollout with both dHash and histogram
distances, ranked by a combined score. The stage-1/stage-2 split is
~1.5× cheaper than running everything long, and the boundary between
them is tuneable independently of the search itself — you can make
stage 1 stricter on one ROM, looser on another.

## Ablation as a dimensionality reduction

Even stage-1 rejects are expensive: a 60-frame rollout is ~0.1s. So I
added a pre-stage: **for each of the 32K PRG addresses, run exactly
one 60-frame rollout with a perturbed byte and ask "does the output
change at all, at any frame, relative to the no-cheat baseline?"**

If the answer is no, that address is never read during that particular
play sequence. Whatever byte we patch there, the game won't notice. We
drop the address entirely from the candidate space.

This is differential ablation — the same trick people use to decide
which input features a model actually uses, or which neurons fire.
Applied to a 6502 CPU on Super Mario Bros. 1 walking right, it prunes
32,768 addresses to 1,789 (5.5%), yielding an ~18× reduction in the
search space for that play sequence. On some games with long intros and
narrow active code paths, it prunes more like 90%.

You get this for the price of one rollout per address — a fixed cost of
~9 minutes on 8 cores, amortized over the subsequent millions of
candidates.

Crucially, the pruning is **play-sequence-specific**. Tracing with
"walk right on level 1-1" and then searching gives you codes that affect
that scenario. Want codes that only fire on boss fights? Trace with a
boss-fight input sequence. This turns the exploratory search into a
scientific instrument: you pick the scenario, you get codes selective
to it.

## The multiprocessing war story

The part nobody wants to write about.

`multiprocessing.Pool.imap_unordered` is great when workers behave.
Ours don't: some candidate bytes, when patched in, send the 6502 CPU
into an infinite loop on a valid-but-malformed opcode stream. The
worker goes into C++ and never comes back. `pool.terminate()` sends
SIGTERM, which a worker stuck in native code can ignore arbitrarily
long. Worse, `multiprocessing.Pool` has a **worker-handler thread that
silently respawns workers that died**, which means SIGKILL in a
watchdog gets you eight zombies followed by eight live replacements
holding torn pool state. Cleanup then deadlocks on the result-handler
thread blocked on closed pipes.

Debugging this was the usual sequence of thinking you'd fixed it, going
to get coffee, coming back to find the process alive at 0.7% CPU and no
log output for twenty minutes.

The fix was to drop `multiprocessing.Pool` entirely and own the
lifecycle: a bare `mp.Process` per worker, two `mp.Queue`s for tasks
and results, and a watchdog loop that polls the result queue with a
2-second timeout. If no result arrives for 60 seconds the main loop
SIGKILLs every worker directly and bails with whatever it's got. Queues
get `cancel_join_thread()` so the feeder thread doesn't block trying to
flush tasks nobody's going to read.

This is the part I want to flag for any data scientist who reaches for
`multiprocessing.Pool` because the docs make it look simple: **if your
workers can hang, you probably don't want `Pool`.** Its restart behavior
and cleanup semantics assume workers are well-behaved, and they will
burn you the first time you have a real hang. Raw `mp.Process` +
`mp.Queue` is ~40 lines and you can reason about every edge case.

## What falls out

On SMB1, walking right on 1-1, a search over 2000 sampled candidates
(with live-address tracing + boot-safety filter) surfaces a mix of:

- Obvious physics tweaks (high jumps, ghost Mario, walk-through-walls).
- Palette / sprite corruptions that leave the game playable but weird.
- Timing / speed adjustments (slow clock, fast music).
- A long tail of "looks the same but something subtle changed" —
  candidates for human inspection via the generated HTML gallery.

1,257 of 2,000 candidates survived the stage-1 filter. Ranking them by
combined dHash + histogram distance gives you a browseable gallery where
the first 50 cards are visually striking and the rest trail off into
subtle variations. This, to me, is the most data-sciencey part of the
project: the output isn't a number, it's a ranked set of hypotheses for
a human to look at.

## Takeaways

1. **Cheap features + cheap distance metrics + a cascade beats a smart
   single-stage scorer.** The expensive thing isn't the model, it's
   iterating over millions of candidates.
2. **Differential ablation is a general dimensionality-reduction tool**
   whenever you're searching over a discrete input space that feeds a
   black box. You just need a cheap "did anything change" signal.
3. **Parallel execution is harder than it looks when your unit of work
   can hang**, and the stdlib's highest-level abstraction isn't
   necessarily the most robust one. Own the lifecycle when lives (or
   weekends) depend on it.
4. **Rendering the output for human inspection is a feature, not an
   afterthought.** For exploratory searches, "here's a ranked gallery"
   often beats "here's a scalar."

The code is at [github.com/sadovsky/rommage](https://github.com/sadovsky/rommage)
— NROM-only in this first pass (which covers SMB1, Donkey Kong, Balloon
Fight, Ice Climber, Excitebike, Mario Bros, Clu Clu Land, Popeye). Port
guide is in `docs/PORTING.md`.
