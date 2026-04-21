"""Variations around YEVLULIA (CPU=$BBE3).

YEVLULIA changes `CMP #$05 ; BNE +2 ; INC $2A,X` at $BBE2 so the CMP
fires on object type 7 instead of 5, producing the famous "coin rain
on coin pickup" cascade — the INC retriggers an effect routine for
additional object types.

The ROM has 8 structural siblings of this pattern (CMP#imm ; BNE/BEQ ;
INC). Each is a candidate for the same flavor of cascade glitch. This
script sweeps the CMP immediate operand at each site through a range of
values, so we can see which ones produce interesting visual cascades.

Target operand addresses (the byte AFTER each CMP opcode):

    $95D7  CMP #$4B ; BNE +3  ; INC abs $45
    $B2FA  CMP #$05 ; BNE +43 ; INC abs $5C
    $BBE3  CMP #$05 ; BNE +2  ; INC zp,X $2A  ← YEVLULIA site
    $C0FC  CMP #$06 ; BNE +35 ; INC abs $D9
    $C258  CMP #$0E ; BNE +3  ; INC abs $39
    $CBFE  CMP #$02 ; BNE +2  ; INC zp,X $A0
    $E9EE  CMP #$01 ; BEQ +70 ; INC zp $02
    $F6DC  CMP #$01 ; BNE +14 ; INC abs $C7
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


TARGETS: list[tuple[int, str]] = [
    (0x95D7, "CMP #$4B ; BNE +3 ; INC $45"),
    (0xB2FA, "CMP #$05 ; BNE +43 ; INC $5C"),
    (0xBBE3, "CMP #$05 ; BNE +2 ; INC $2A,X  (YEVLULIA)"),
    (0xC0FC, "CMP #$06 ; BNE +35 ; INC $D9"),
    (0xC258, "CMP #$0E ; BNE +3 ; INC $39"),
    (0xCBFE, "CMP #$02 ; BNE +2 ; INC $A0,X"),
    (0xE9EE, "CMP #$01 ; BEQ +70 ; INC $02"),
    (0xF6DC, "CMP #$01 ; BNE +14 ; INC $C7"),
]

# Sweep values 0..15 (covers the likely object-ID space without blowing up)
SWEEP_VALUES: list[int] = list(range(0, 16))


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
    print(f"cascade sweep: {total} candidates across {len(TARGETS)} sites")

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

        tag = ""
        if code_str == "YEVLULIA":
            tag = ' <span style="color:#e8c96c">[YEVLULIA]</span>'

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
            f'<div class="code">{html.escape(code_str)}{tag}</div>'
            f'<div class="addr">${addr:04X} '
            f'<span style="color:#555">${orig:02X}</span>'
            f'&nbsp;&rarr;&nbsp;'
            f'<span style="color:#e8c96c">${new_val:02X}</span></div>'
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
                       filename="cascade.html")
    print(f"wrote {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", default=str(HERE.parent.parent / "smb1.nes"))
    ap.add_argument("--out", default=str(HERE), type=str)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    main(args.rom, Path(args.out), seed=args.seed)
