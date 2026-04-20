"""Visual scoring: compare patched frame stacks to a baseline frame stack.

Two complementary metrics:

1. phash (dHash): a 64-bit perceptual hash per frame. Cheap, fast, robust to
   small scroll shifts. Hamming distance between hashes is our stage-1 signal
   (crash/null reject).

2. Color histogram in NES palette space. The NES displays ~54 distinct colors;
   we bucket frames into 64 bins and compute L1 distance. This catches
   semantic changes like "HUD shows 9 lives", "screen tinted red", even when
   perceptual layout is unchanged.

Frame-stack comparison: we compute per-frame distances between aligned frames
(capture N of baseline vs capture N of candidate) and return both the mean
and the max. Max is the useful signal for codes whose effect takes a few
captures to manifest.

A code that crashes the emulator typically produces PPU garbage -- very high
hash distance AND very high histogram distance. A null code produces near-zero
distances. The sweet spot is moderate distances on both axes.
"""

from __future__ import annotations
from dataclasses import dataclass

import numpy as np


# ---------- perceptual hash (dHash) ----------

def _dhash_frame(frame: np.ndarray, hash_size: int = 8) -> np.uint64:
    """Compute a (hash_size*hash_size)-bit dHash as a uint64.

    dHash: downsample to (hash_size, hash_size+1) grayscale, then each bit is
    1 iff pixel[r,c+1] > pixel[r,c]. Robust to brightness shifts and small
    translations.
    """
    # luminance approximation: mean across channels
    gray = frame.astype(np.float32).mean(axis=2)
    # Downsample with simple block averaging. shape (H, W) -> (hash_size, hash_size+1)
    h, w = gray.shape
    h_step, w_step = h // hash_size, w // (hash_size + 1)
    small = gray[: h_step * hash_size, : w_step * (hash_size + 1)]
    small = small.reshape(hash_size, h_step, hash_size + 1, w_step).mean(axis=(1, 3))
    diff = small[:, 1:] > small[:, :-1]  # (hash_size, hash_size)
    bits = diff.flatten()
    # Pack into a uint64 (up to 64 bits).
    out = np.uint64(0)
    for b in bits[:64]:
        out = (out << np.uint64(1)) | np.uint64(bool(b))
    return out


def dhash_stack(frames: np.ndarray) -> np.ndarray:
    """(N, 240, 256, 3) -> (N,) uint64 hashes."""
    if len(frames) == 0:
        return np.empty((0,), dtype=np.uint64)
    return np.array([_dhash_frame(f) for f in frames], dtype=np.uint64)


def hamming(a: np.uint64, b: np.uint64) -> int:
    return int(bin(int(a ^ b)).count("1"))


def hamming_stack(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Elementwise Hamming distance over two uint64 arrays."""
    x = np.bitwise_xor(a, b)
    # popcount via numpy bit tricks (bit_count on Python ints is fast enough)
    return np.array([bin(int(v)).count("1") for v in x], dtype=np.int32)


# ---------- color histogram ----------

def _quantize(frame: np.ndarray) -> np.ndarray:
    """Quantize RGB frame to 64 bins: 4 per channel. Returns flat indices."""
    # frame is uint8, HxWx3. Shift-right 6 keeps the top 2 bits per channel.
    q = (frame >> 6).astype(np.int32)        # 0..3 per channel
    idx = (q[..., 0] << 4) | (q[..., 1] << 2) | q[..., 2]  # 0..63
    return idx


def color_hist_stack(frames: np.ndarray, bins: int = 64) -> np.ndarray:
    """(N, H, W, 3) -> (N, bins) float32 normalized histograms."""
    if len(frames) == 0:
        return np.empty((0, bins), dtype=np.float32)
    hists = np.empty((len(frames), bins), dtype=np.float32)
    for i, f in enumerate(frames):
        idx = _quantize(f).ravel()
        h = np.bincount(idx, minlength=bins).astype(np.float32)
        h /= h.sum() + 1e-12
        hists[i] = h
    return hists


def hist_l1(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """L1 distance between aligned histogram stacks. Returns (N,) float."""
    return np.abs(a - b).sum(axis=-1)


# ---------- combined score ----------

@dataclass
class ScoreResult:
    hamming_mean: float
    hamming_max: int
    hist_mean: float
    hist_max: float
    # Heuristic bucket based on the two distances
    bucket: str  # "null" | "interesting" | "likely_crash"

    def as_dict(self) -> dict:
        return {
            "hamming_mean": self.hamming_mean,
            "hamming_max": self.hamming_max,
            "hist_mean": self.hist_mean,
            "hist_max": self.hist_max,
            "bucket": self.bucket,
        }


# Defaults tuned for 64-bit dhash and 64-bin color histogram. Bias these
# empirically for your specific ROM + input sequence by running the null/
# crash baselines.
NULL_HAMMING_MAX = 3       # <= this is "changed nothing observable"
NULL_HIST_MAX = 0.02
CRASH_HAMMING_MIN = 40     # >= this (out of 64) is likely PPU garbage
CRASH_HIST_MIN = 1.2       # near-complete distribution collapse


def score_frames(
    baseline_hashes: np.ndarray,
    baseline_hists: np.ndarray,
    candidate_frames: np.ndarray,
) -> ScoreResult:
    """Compare candidate frames against precomputed baseline features."""
    n = min(len(baseline_hashes), len(candidate_frames))
    if n == 0:
        return ScoreResult(0.0, 0, 0.0, 0.0, "null")

    cand_hashes = dhash_stack(candidate_frames[:n])
    cand_hists = color_hist_stack(candidate_frames[:n])

    ham = hamming_stack(baseline_hashes[:n], cand_hashes)
    hist = hist_l1(baseline_hists[:n], cand_hists)

    ham_mean = float(ham.mean())
    ham_max = int(ham.max())
    hist_mean = float(hist.mean())
    hist_max = float(hist.max())

    if ham_max <= NULL_HAMMING_MAX and hist_max <= NULL_HIST_MAX:
        bucket = "null"
    elif ham_mean >= CRASH_HAMMING_MIN or hist_mean >= CRASH_HIST_MIN:
        bucket = "likely_crash"
    else:
        bucket = "interesting"

    return ScoreResult(ham_mean, ham_max, hist_mean, hist_max, bucket)


def precompute_baseline(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Returns (hashes, hists) for a baseline capture stack."""
    return dhash_stack(frames), color_hist_stack(frames)
