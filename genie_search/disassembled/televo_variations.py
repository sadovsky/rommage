"""Variations around TELEVO (CPU=$89BE, value=$0E).

TELEVO is the operand of `LDA #$06` at $89BE, which immediately feeds
`STA $0773` at $89BF. $0773 is SMB1's VRAM_Buffer_AddrCtrl — a dispatch
index into a table of pre-canned PPU write blocks ("write score", "write
coin count", "write WORLD 1-1", etc). Changing the index redirects the
writer to a different string, which is how TELEVO puts unexpected text
on screen.

The ROM has five `LDA #imm; STA $0773` sites:

    $864B: LDA #$0B  (at $864C: the immediate operand)
    $86FA: LDA #$06  (at $86FB)
    $89BE: LDA #$06  (at $89BF) ← TELEVO operand
    $8A5C: LDA #$06  (at $8A5D)
    $9A0A: LDA #$08  (at $9A0B)

Each immediate operand is a separate dispatch trigger. Mutating each one
through indexes 0..31 samples the dispatch table and reveals which
indexes produce readable text, scrambled strings, or crashes.

The rollout captures ~2s after warmup so status-bar / dispatched text
has had a chance to render.
"""

from __future__ import annotations
import sys
import time
import html
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from genie import GenieCode, encode
from analyze import _has_structure
from search import read_prg_bytes, rom_byte_at
from PIL import Image
import numpy as np

from generate import (
    WARMUP, _random_rollout, run_candidate, _run_with_respawn,
    write_gallery,
)


# ---------- target operand addresses ----------
#
# Each of these is the one-byte immediate operand of an `LDA #imm`
# instruction that is immediately stored to VRAM_Buffer_AddrCtrl ($0773).
# Mutating the operand changes which dispatch entry fires.

TARGETS: list[tuple[int, str]] = [
    (0x89BE, "TELEVO operand — LDA #$06 → STA $0773"),
    (0x864C, "LDA #$0B → STA $0773"),
    (0x86FB, "LDA #$06 → STA $0773"),
    (0x8A5D, "LDA #$06 → STA $0773"),
    (0x9A0B, "LDA #$08 → STA $0773"),
]


# ---------- value sweep ----------
#
# VRAM_Buffer_AddrCtrl indexes a pointer table. Legal entries are small
# non-negative integers; values past the table end walk into adjacent data
# and produce garbage or hangs. Sweep 0..31 to cover the legitimate range
# plus a handful of overrun indexes.

SWEEP_VALUES: list[int] = list(range(0, 32))


# ---------- main ----------

def main(rom_path: str, out_dir: Path, seed: int = 0):
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    prg, _ = read_prg_bytes(rom_path)
    rollout = _random_rollout(seed)
    actions_full = list(WARMUP) + list(rollout)

    tasks: list[tuple] = []
    meta: list[tuple] = []
    for addr, label in TARGETS:
        orig = rom_byte_at(prg, addr)
        for new_val in SWEEP_VALUES:
            if new_val == orig:
                continue
            gc = GenieCode(address=addr & 0x7FFF, value=new_val, compare=orig)
            code_str = encode(gc)
            tasks.append((addr & 0x7FFF, new_val, orig, actions_full))
            meta.append((f"${addr:04X}", addr, orig, new_val, code_str, label))

    total = len(tasks)
    print(f"TELEVO sweep: {total} candidates across {len(TARGETS)} addresses "
          f"(warmup {sum(d for _, d in WARMUP)} + "
          f"rollout {sum(d for _, d in rollout)} frames)")

    print("capturing baseline...")
    baseline = run_candidate(rom_path, None, rollout)
    Image.fromarray(baseline).save(thumbs / "baseline.png", optimize=True)
    Image.fromarray(np.zeros((240, 256, 3), dtype=np.uint8)).save(
        thumbs / "_crash.png", optimize=True
    )

    cards_by_addr: dict[str, list[str]] = {f"${a:04X}": [] for a, _ in TARGETS}
    t0 = time.perf_counter()

    def on_each(i: int, task, frame):
        addr_key, addr, orig, new_val, code_str, label = meta[i]

        televo_tag = ""
        if code_str == "TELEVO":
            televo_tag = ' <span style="color:#e8c96c">[TELEVO]</span>'

        if frame is None:
            boot = "unsafe"
            thumb_path = "thumbs/_crash.png"
        else:
            boot = "safe" if _has_structure(frame) else "unsafe"
            thumb_name = f"{code_str}.png"
            Image.fromarray(frame).save(thumbs / thumb_name, optimize=True)
            thumb_path = f"thumbs/{thumb_name}"

        card = (
            f'<div class="card {boot}">'
            f'<img src="{html.escape(thumb_path)}" '
            f'alt="{html.escape(code_str)}" loading="lazy">'
            f'<div class="code">{html.escape(code_str)}{televo_tag}</div>'
            f'<div class="addr">${addr:04X} '
            f'<span style="color:#555">${orig:02X}</span>'
            f'&nbsp;&rarr;&nbsp;'
            f'<span style="color:#e8c96c">${new_val:02X}</span> '
            f'<span style="color:#888">(idx {new_val})</span></div>'
            f'<div class="desc">{html.escape(label)}</div>'
            f'<div class="boot {boot}">'
            f'{"boot: safe" if boot == "safe" else "boot: UNSAFE"}'
            f'</div>'
            f'</div>'
        )
        cards_by_addr[addr_key].append(card)

        done = i + 1
        if done % 10 == 0 or done == total:
            elapsed = time.perf_counter() - t0
            rate = done / elapsed if elapsed > 0 else 0.0
            eta = (total - done) / rate if rate > 0 else 0.0
            print(f"  {done:>3}/{total} ({100*done/total:>4.1f}%) "
                  f"@ {rate:.2f}/s eta {int(eta)}s "
                  f"[{addr_key}:{code_str}:{boot}]", flush=True)

    n_crashes = _run_with_respawn(rom_path, tasks, on_each)
    print(f"\nruntime: {time.perf_counter() - t0:.1f}s "
          f"({n_crashes} worker crashes)")

    out = write_gallery(out_dir, "thumbs/baseline.png", cards_by_addr,
                       filename="televo.html")
    print(f"wrote {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", default=str(HERE.parent.parent / "smb1.nes"))
    ap.add_argument("--out", default=str(HERE), type=str)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    main(args.rom, Path(args.out), seed=args.seed)
