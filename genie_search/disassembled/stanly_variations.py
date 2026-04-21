"""Variations around STANLY (CPU=$F70B, value=$E5).

The original byte at $F70B is $85 (STA zp opcode). STANLY flips it to $E5
(SBC zp), which means the sound engine — instead of storing a music-pointer
low byte into ZP $F9 — subtracts whatever's at $F9 from A. Subsequent
stores from the same block then propagate the corrupted value into the
other music pointer bytes. The result is a twisted but still-running note
stream.

This script sweeps:

1. A broad **value sweep** at $F70B itself — every other opcode byte in the
   "cc=01" 6502 group (ORA/AND/EOR/ADC/LDA/CMP/SBC in all addressing modes),
   plus a handful of low-byte operand flips. Each changes STA zp into a
   different read-instead-of-store.

2. **Sibling-address mutations** at the other STA zp opcodes nearby
   ($F701, $F706, $F710) which load the other three bytes of the same
   music-pointer tuple. Mutating those produces different flavors of the
   same glitch.

The HTML gallery flags boot-safe vs. crash, grouped by target address.
Thumbnails are visual sanity checks — the actual interesting signal is
audio and needs an emulator to appreciate.
"""

from __future__ import annotations
import os
import sys
import time
import html
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import contextlib
import io
with contextlib.redirect_stderr(io.StringIO()):
    pass

from genie import GenieCode, encode
from analyze import _has_structure
from search import read_prg_bytes, rom_byte_at
from PIL import Image
import numpy as np

# Reuse all the heavy lifting from generate.py
from generate import (
    WARMUP, _random_rollout, run_candidate, _run_with_respawn,
    CSS, write_gallery,
)


# ---------- target addresses ----------
#
# These are the STA zp instructions in the sound-engine block at $F700–$F716
# that STANLY touches. Each expects the music-pointer byte in A and stores
# it to a zero-page variable (likely Squ1NoteLenBuffer / Squ2_SfxLenCounter
# / similar per-channel state). Mutating any of them produces a variation
# of STANLY-style audio corruption.

TARGETS: list[tuple[int, str]] = [
    (0xF70B, "STANLY target — STA $F9 (primary music ptr)"),
    (0xF701, "STA $F5 (music ptr byte 0)"),
    (0xF706, "STA $F6 (music ptr byte 1)"),
    (0xF710, "STA $F8 (music ptr byte 3)"),
]


# ---------- value mutations ----------
#
# These are the "cc=01" 6502 group opcodes (same addressing-mode encoding
# as STA zp = $85). Swapping into any of them changes an instruction that
# writes A into one that reads/modifies A, which is exactly what STANLY
# does with SBC.

GROUP_CC01_OPCODES: list[tuple[int, str]] = [
    (0x05, "ORA zp — OR with ZP byte"),
    (0x25, "AND zp — AND with ZP byte"),
    (0x45, "EOR zp — XOR with ZP byte"),
    (0x65, "ADC zp — add to A"),
    (0x85, "STA zp — ORIGINAL"),
    (0xA5, "LDA zp — load from ZP (A replaced)"),
    (0xC5, "CMP zp — compare (A unchanged, flags set)"),
    (0xE5, "SBC zp — STANLY's choice"),
]

# STX/STY as alternates (different encoding but still store opcodes of
# similar size — swapping to these reinterprets the operand byte too).
STORE_ALTS: list[tuple[int, str]] = [
    (0x84, "STY zp — store Y instead of A"),
    (0x86, "STX zp — store X instead of A"),
]

# A few wildcard bytes to sample operand-land: these turn STA zp into a
# two-byte no-op-ish or a pull-stack or similar single-byte instruction.
WILDCARDS: list[tuple[int, str]] = [
    (0xEA, "NOP"),
    (0x08, "PHP — push P onto stack"),
    (0x28, "PLP — pull P from stack"),
    (0x48, "PHA"),
    (0x68, "PLA"),
    (0x00, "BRK — typically crash"),
]

ALL_MUTATIONS = GROUP_CC01_OPCODES + STORE_ALTS + WILDCARDS


# ---------- main ----------

def main(rom_path: str, out_dir: Path, seed: int = 0):
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    prg, _ = read_prg_bytes(rom_path)
    rollout = _random_rollout(seed)
    actions_full = list(WARMUP) + list(rollout)

    # Build the task + meta lists
    tasks: list[tuple] = []
    meta: list[tuple] = []
    for addr, label in TARGETS:
        orig = rom_byte_at(prg, addr)
        for new_val, mut_desc in ALL_MUTATIONS:
            if new_val == orig:
                continue
            gc = GenieCode(address=addr & 0x7FFF, value=new_val, compare=orig)
            code_str = encode(gc)
            tasks.append((addr & 0x7FFF, new_val, orig, actions_full))
            meta.append((f"${addr:04X}", addr, orig, new_val, code_str,
                         label, mut_desc))

    total = len(tasks)
    print(f"STANLY sweep: {total} candidates across {len(TARGETS)} addresses "
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
        addr_key, addr, orig, new_val, code_str, label, mut_desc = meta[i]

        # Highlight STANLY itself with a little badge
        stanly_tag = ""
        if code_str == "STANLY":
            stanly_tag = ' <span style="color:#e8c96c">[STANLY]</span>'

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
            f'<div class="code">{html.escape(code_str)}{stanly_tag}</div>'
            f'<div class="addr">${addr:04X} '
            f'<span style="color:#555">${orig:02X}</span>'
            f'&nbsp;&rarr;&nbsp;'
            f'<span style="color:#e8c96c">${new_val:02X}</span></div>'
            f'<div class="desc">{html.escape(mut_desc)}</div>'
            f'<div class="boot {boot}">'
            f'{"boot: safe" if boot == "safe" else "boot: UNSAFE"}'
            f'</div>'
            f'</div>'
        )
        cards_by_addr[addr_key].append(card)

        done = i + 1
        if done % 5 == 0 or done == total:
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
                        filename="stanly.html")
    print(f"wrote {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", default=str(HERE.parent.parent / "smb1.nes"))
    ap.add_argument("--out", default=str(HERE), type=str)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    main(args.rom, Path(args.out), seed=args.seed)
