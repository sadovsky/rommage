"""Cluster stage-2 survivors and rank by percentile.

The built-in ranker applies a fixed score formula and relies on hand-tuned
absolute thresholds. It tends to flood the top of the list with tied-score
candidates that all share one visual artifact (e.g. dozens of codes that all
happen to trip a death→black-screen transition).

This script:
  1. Clusters stage-2 survivors by rounded (hist_mean, hamming_mean).
  2. Picks one representative per cluster.
  3. Buckets the representatives by score percentile — so the top tier is
     defined relative to this specific run, not to a fixed constant.

Usage:
  python analyze.py results/smb1
"""

from __future__ import annotations
import argparse
import contextlib
import html
import io
import os
import pickle
import shutil
from collections import defaultdict
from pathlib import Path

import numpy as np

with contextlib.redirect_stderr(io.StringIO()):
    from rommage import INPUT_SEQUENCES
    from genie import GenieCode
    from scorer import _dhash_frame, _quantize, hamming


HIST_BIN = 0.005   # histogram-mean bin width
HAM_BIN = 0.5      # dhash-hamming-mean bin width

# Boot-safety thresholds: applied to the end-of-warmup frame, comparing
# cheat-active warmup against no-cheat warmup. Lenient because some codes
# alter ambient rendering (HUD digits, sprite palette) without breaking boot.
BOOT_HAM_MAX = 8
BOOT_HIST_MAX = 0.12


def _frame_hist(frame: np.ndarray) -> np.ndarray:
    idx = _quantize(frame).ravel()
    h = np.bincount(idx, minlength=64).astype(np.float32)
    return h / (h.sum() + 1e-12)


def _run_warmup(rom_path: str, warmup_seq, cheats=()) -> np.ndarray:
    """Return the final frame after stepping through warmup_seq from reset."""
    with contextlib.redirect_stderr(io.StringIO()):
        from cheat_env import CheatNESEnv
    env = CheatNESEnv(rom_path)
    env.reset()
    for c in cheats:
        env.add_cheat(c)
    for action, duration in warmup_seq:
        for _ in range(duration):
            env.step(action)
    frame = env.screen.copy()
    env.close()
    return frame


def check_boot_safety(
    rom_path: str,
    warmup_seq,
    reps: list[tuple[dict, int]],
) -> dict[str, bool]:
    """For each cluster rep, run warmup with the cheat active from reset and
    compare the end-frame to the no-cheat end-frame. Returns code_str -> bool.
    """
    baseline_frame = _run_warmup(rom_path, warmup_seq)
    base_hash = _dhash_frame(baseline_frame)
    base_hist = _frame_hist(baseline_frame)

    out: dict[str, bool] = {}
    for rep, _ in reps:
        code = GenieCode(
            address=rep["cpu_addr"] & 0x7FFF,
            value=rep["value"],
            compare=rep.get("compare"),
        )
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                frame = _run_warmup(rom_path, warmup_seq, cheats=[code])
            h = _dhash_frame(frame)
            hist_d = float(np.abs(_frame_hist(frame) - base_hist).sum())
            out[rep["code_str"]] = (
                hamming(h, base_hash) <= BOOT_HAM_MAX and hist_d <= BOOT_HIST_MAX
            )
        except Exception:
            out[rep["code_str"]] = False
    return out


def score_of(r: dict) -> float:
    s2 = r.get("s2") or {}
    return s2.get("hist_mean", 0.0) + 0.5 * (s2.get("hamming_mean", 0.0) / 64.0)


def cluster_key(r: dict) -> tuple[float, float]:
    s2 = r.get("s2") or {}
    h = s2.get("hist_mean", 0.0)
    m = s2.get("hamming_mean", 0.0)
    return (round(h / HIST_BIN) * HIST_BIN, round(m / HAM_BIN) * HAM_BIN)


def bucket_for_percentile(p: float) -> str:
    if p >= 95:
        return "top"
    if p >= 75:
        return "promising"
    return "noise"


def analyze(
    results_dir: Path,
    rom_path: str | None = None,
    warmup_frames: int = 0,
    warmup_seq_name: str = "walk_right",
) -> None:
    final = results_dir / "results.pkl"
    partial = results_dir / "results.partial.pkl"
    if final.exists():
        src = final
    elif partial.exists():
        src = partial
        print(f"(using partial results from {partial.name} — run still in progress)")
    else:
        raise FileNotFoundError(
            f"no results.pkl or results.partial.pkl found in {results_dir}"
        )
    with open(src, "rb") as f:
        all_results = pickle.load(f)

    survivors = [r for r in all_results if r.get("passed_stage1") and r.get("s2")]
    print(f"{len(all_results):,} total, {len(survivors):,} stage-2 survivors\n")
    if not survivors:
        return

    # Cluster
    clusters: dict[tuple[float, float], list[dict]] = defaultdict(list)
    for r in survivors:
        clusters[cluster_key(r)].append(r)

    # One representative per cluster: highest-scoring, ties broken by code_str
    reps: list[tuple[dict, int]] = []
    for members in clusters.values():
        members.sort(key=lambda r: (-score_of(r), r["code_str"]))
        reps.append((members[0], len(members)))

    reps.sort(key=lambda t: -score_of(t[0]))
    rep_scores = np.array([score_of(rep) for rep, _ in reps])

    print(f"clustered into {len(reps)} groups (bins: hist±{HIST_BIN}, ham±{HAM_BIN})")
    print(f"score dist over reps: "
          f"p50={np.percentile(rep_scores, 50):.4f} "
          f"p75={np.percentile(rep_scores, 75):.4f} "
          f"p95={np.percentile(rep_scores, 95):.4f} "
          f"max={rep_scores.max():.4f}\n")

    boot_map: dict[str, bool] = {}
    if rom_path and warmup_frames > 0:
        warmup_seq = INPUT_SEQUENCES[warmup_seq_name](warmup_frames)
        print(f"checking boot-safety for {len(reps)} cluster reps "
              f"(warmup: {warmup_seq_name}, {warmup_frames} frames)...")
        boot_map = check_boot_safety(rom_path, warmup_seq, reps)
        n_safe = sum(boot_map.values())
        print(f"{n_safe}/{len(reps)} cluster reps are boot-safe\n")
    else:
        print("(skipping boot-safety check: pass --rom and --warmup-frames)\n")

    # Table
    boot_col = "boot" if boot_map else ""
    header = (f"{'rank':>4}  {'size':>4}  {'pct':>5}  {'tier':<10}  "
              f"{'code':<9}  {'addr':>5}  {'val':>3}  "
              f"{'hist':>6}  {'ham':>5}  {'score':>6}")
    if boot_map:
        header += f"  {boot_col:>5}"
    print(header)
    print("-" * len(header))
    for rank, (rep, size) in enumerate(reps, 1):
        score = score_of(rep)
        pct = 100 * (rep_scores <= score).sum() / len(rep_scores)
        tier = bucket_for_percentile(pct)
        s2 = rep["s2"] or {}
        row = (f"{rank:>4}  {size:>4}  {pct:>4.0f}%  {tier:<10}  "
               f"{rep['code_str']:<9}  "
               f"{rep['cpu_addr']:>5X}  {rep['value']:>3X}  "
               f"{s2.get('hist_mean', 0):>6.3f}  "
               f"{s2.get('hamming_mean', 0):>5.1f}  "
               f"{score:>6.3f}")
        if boot_map:
            row += f"  {'yes' if boot_map[rep['code_str']] else 'NO':>5}"
        print(row)

    write_clustered_report(
        results_dir, reps, rep_scores, len(all_results), len(survivors), boot_map
    )


def write_clustered_report(
    out_dir: Path,
    reps: list[tuple[dict, int]],
    rep_scores: np.ndarray,
    total_evaluated: int,
    n_passed_stage1: int,
    boot_map: dict[str, bool] | None = None,
) -> None:
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    cards = []
    for rep, size in reps:
        score = score_of(rep)
        pct = 100 * (rep_scores <= score).sum() / len(rep_scores)
        tier = bucket_for_percentile(pct)
        s2 = rep["s2"] or {}

        code = rep["code_str"]
        thumb_src = rep.get("thumbnail_path")
        if thumb_src and os.path.exists(thumb_src):
            dest = thumbs / f"{code}.png"
            if os.path.abspath(thumb_src) != os.path.abspath(dest):
                shutil.copyfile(thumb_src, dest)
        elif not (thumbs / f"{code}.png").exists():
            continue

        cmp_str = f" (cmp ${rep['compare']:02X})" if rep.get("compare") is not None else ""
        boot_html = ""
        boot_class = ""
        if boot_map:
            safe = boot_map.get(code, False)
            boot_class = " boot-safe-card" if safe else " boot-unsafe-card"
            boot_html = (f'<div class="boot-{"safe" if safe else "unsafe"}">'
                         f'{"boot: safe" if safe else "boot: UNSAFE"}</div>')
        cards.append(
            f'<div class="card tier-{tier}{boot_class}">'
            f'<img src="thumbs/{html.escape(code)}.png" alt="{html.escape(code)}">'
            f'<div class="code">{html.escape(code)}</div>'
            f'<div class="addr">${rep["cpu_addr"]:04X} := ${rep["value"]:02X}{html.escape(cmp_str)}</div>'
            f'<div class="stats"><span class="tier-label-{tier}">{tier}</span> '
            f'&middot; p{pct:.0f} &middot; cluster size {size} '
            f'&middot; hist {s2.get("hist_mean", 0):.3f} '
            f'&middot; ham {s2.get("hamming_mean", 0):.1f}</div>'
            f'{boot_html}'
            f'</div>'
        )

    n_safe = sum(1 for v in (boot_map or {}).values() if v)
    n_unsafe = len(boot_map or {}) - n_safe
    if boot_map:
        filters = (
            '<div class="filters">'
            f'<label><input type="checkbox" id="show-safe" checked> '
            f'boot-safe <span style="color:#6ce06c">({n_safe})</span></label>'
            f'<label><input type="checkbox" id="show-unsafe" checked> '
            f'boot-unsafe <span style="color:#d06c6c">({n_unsafe})</span></label>'
            '<span class="count" id="shown-count"></span>'
            '</div>'
        )
    else:
        filters = ""
    cards_html = chr(10).join(cards)

    page = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Clustered results</title>
<style>
  body {{ font-family: ui-monospace, Menlo, Consolas, monospace;
         background: #111; color: #ddd; margin: 24px; }}
  h1 {{ font-size: 18px; color: #fff; }}
  .meta {{ color: #888; font-size: 13px; margin-bottom: 24px; }}
  .baseline {{ display: flex; gap: 16px; align-items: flex-start;
              padding: 12px; background: #1a1a1a; border-radius: 6px;
              margin-bottom: 24px; }}
  .baseline img {{ image-rendering: pixelated; width: 256px; height: 240px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, 272px);
          gap: 16px; }}
  .card {{ background: #1a1a1a; padding: 8px; border-radius: 6px;
           border-left: 3px solid #333; }}
  .card.tier-top {{ border-left-color: #6ce06c; }}
  .card.tier-promising {{ border-left-color: #e8c96c; }}
  .card.tier-noise {{ border-left-color: #444; opacity: 0.6; }}
  .card img {{ width: 256px; height: 240px; image-rendering: pixelated;
              display: block; }}
  .code {{ font-size: 15px; color: #fff; margin: 6px 0 2px; }}
  .addr {{ font-size: 11px; color: #888; }}
  .stats {{ font-size: 11px; color: #aaa; margin-top: 4px; }}
  .tier-label-top {{ color: #6ce06c; }}
  .tier-label-promising {{ color: #e8c96c; }}
  .tier-label-noise {{ color: #666; }}
  .boot-safe {{ font-size: 11px; color: #6ce06c; margin-top: 2px; }}
  .boot-unsafe {{ font-size: 11px; color: #d06c6c; margin-top: 2px; }}
  .filters {{ padding: 12px; background: #1a1a1a; border-radius: 6px;
              margin-bottom: 24px; font-size: 13px; display: flex;
              gap: 18px; align-items: center; }}
  .filters label {{ cursor: pointer; user-select: none; }}
  .filters input {{ vertical-align: middle; margin-right: 6px; }}
  .filters .count {{ color: #888; margin-left: auto; font-size: 12px; }}
  body.hide-boot-safe .boot-safe-card {{ display: none; }}
  body.hide-boot-unsafe .boot-unsafe-card {{ display: none; }}
</style></head><body>
<h1>Clustered results ({len(reps)} clusters from {n_passed_stage1} survivors / {total_evaluated:,} evaluated)</h1>
<div class="meta">bins: hist±{HIST_BIN}, ham±{HAM_BIN} &middot; tiers: p95+ top, p75+ promising, rest noise</div>
<div class="baseline">
  <div>
    <div style="color:#888;font-size:12px">baseline</div>
    <img src="thumbs/baseline.png" alt="baseline">
  </div>
</div>
{filters}
<div class="grid">
{cards_html}
</div>
<script>
(function() {{
  var body = document.body;
  function bind(id, cls) {{
    var el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('change', function() {{
      body.classList.toggle(cls, !el.checked);
      updateCount();
    }});
  }}
  function updateCount() {{
    var total = document.querySelectorAll('.grid .card').length;
    var shown = 0;
    document.querySelectorAll('.grid .card').forEach(function(c) {{
      if (c.offsetParent !== null) shown++;
    }});
    var lbl = document.getElementById('shown-count');
    if (lbl) lbl.textContent = 'showing ' + shown + ' of ' + total;
  }}
  bind('show-safe', 'hide-boot-safe');
  bind('show-unsafe', 'hide-boot-unsafe');
  updateCount();
}})();
</script>
</body></html>
"""
    out = out_dir / "clustered.html"
    out.write_text(page, encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("--rom", default=None,
                    help="ROM path — required for boot-safety check")
    ap.add_argument("--warmup-frames", type=int, default=0,
                    help="warmup frame count to replay for boot-safety check")
    ap.add_argument("--warmup-input-sequence", default="walk_right",
                    choices=list(INPUT_SEQUENCES.keys()))
    args = ap.parse_args()
    analyze(
        args.results_dir,
        rom_path=args.rom,
        warmup_frames=args.warmup_frames,
        warmup_seq_name=args.warmup_input_sequence,
    )
