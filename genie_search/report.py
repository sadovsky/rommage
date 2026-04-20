"""Generate an HTML thumbnail gallery of the top-K candidates.

Output layout:
  out_dir/
    index.html
    thumbs/
      <code>.png     (copies of the saved thumbnails)
      baseline.png
"""

from __future__ import annotations
import html
import os
import shutil
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image

from search import CandidateResult


HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Game Genie search results: {rom_name}</title>
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
  .card {{ background: #1a1a1a; padding: 8px; border-radius: 6px; }}
  .card img {{ width: 256px; height: 240px; image-rendering: pixelated;
              display: block; }}
  .code {{ font-size: 15px; color: #fff; margin: 6px 0 2px; }}
  .addr {{ font-size: 11px; color: #888; }}
  .stats {{ font-size: 11px; color: #aaa; margin-top: 4px; }}
  .bucket-interesting {{ color: #6ce06c; }}
  .bucket-likely_crash {{ color: #e8a94a; }}
  .bucket-null {{ color: #666; }}
</style></head><body>
<h1>Game Genie search results: {rom_name}</h1>
<div class="meta">{meta}</div>
<div class="baseline">
  <div>
    <div style="color:#888;font-size:12px">baseline</div>
    <img src="thumbs/baseline.png" alt="baseline">
  </div>
  <div>
    <div style="color:#888;font-size:12px">top stats across {total_evaluated} evaluated</div>
    <div style="font-size:12px;line-height:1.6">
      {summary}
    </div>
  </div>
</div>
<div class="grid">
{cards}
</div>
</body></html>
"""

CARD_TEMPLATE = """<div class="card">
  <img src="thumbs/{code}.png" alt="{code}">
  <div class="code">{code}</div>
  <div class="addr">${addr:04X} := ${val:02X}{cmp_str}</div>
  <div class="stats"><span class="bucket-{bucket}">{bucket}</span>
    &middot; hist {hist_mean:.3f} / max {hist_max:.3f}
    &middot; ham {ham_mean:.1f}
  </div>
</div>"""


def write_report(
    out_dir: str,
    rom_path: str,
    baseline_frame: np.ndarray,
    top: Sequence[CandidateResult],
    total_evaluated: int,
    n_passed_stage1: int,
    top_k: int = 64,
) -> str:
    """Write index.html + thumbs into out_dir. Returns path to index.html."""
    out = Path(out_dir)
    thumbs = out / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    Image.fromarray(baseline_frame).save(thumbs / "baseline.png", optimize=True)

    cards_html = []
    for r in top[:top_k]:
        if r.thumbnail_path and os.path.exists(r.thumbnail_path):
            dest = thumbs / f"{r.code_str}.png"
            if os.path.abspath(r.thumbnail_path) != os.path.abspath(dest):
                shutil.copyfile(r.thumbnail_path, dest)
        else:
            continue
        s2 = r.s2 or {}
        cmp_str = f" (cmp ${r.compare:02X})" if r.compare is not None else ""
        cards_html.append(CARD_TEMPLATE.format(
            code=html.escape(r.code_str),
            addr=r.cpu_addr,
            val=r.value,
            cmp_str=cmp_str,
            bucket=html.escape(s2.get("bucket", "null")),
            hist_mean=s2.get("hist_mean", 0.0),
            hist_max=s2.get("hist_max", 0.0),
            ham_mean=s2.get("hamming_mean", 0.0),
        ))

    summary_bits = [
        f"{total_evaluated:,} candidates evaluated",
        f"{n_passed_stage1:,} passed stage-1 filter",
        f"{len(top):,} ranked as interesting",
        f"showing top {min(top_k, len(top))}",
    ]
    summary = "<br>".join(html.escape(s) for s in summary_bits)

    html_text = HTML_TEMPLATE.format(
        rom_name=html.escape(os.path.basename(rom_path)),
        meta=html.escape(rom_path),
        total_evaluated=total_evaluated,
        summary=summary,
        cards="\n".join(cards_html),
    )

    index = out / "index.html"
    index.write_text(html_text, encoding="utf-8")
    return str(index)
