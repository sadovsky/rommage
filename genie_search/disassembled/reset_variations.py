"""Variations around YEVLULIA's *opposite* shape: velocity-reset gates.

YEVLULIA mutates an INC-gate: `CMP #imm ; BNE ; INC $2A,X` — changing which
object type gets its counter incremented. The effect is that coins no longer
tick their despawn counter, so they hang in the air when picked up.

The mirror-image pattern is the *reset* gate:

    LDA <counter>
    CMP #<cap>
    BNE skip
    LDA #$00
    STA <counter>

These are the "when counter hits the cap, zero it" lines. Mutating the CMP
immediate so it never matches makes the counter walk past its cap — the
counter/state never resets. This is the mechanic expected to drive a
"broken block pieces never despawn" cheat (analogous to coin rain for
YEVLULIA).

The ROM has 8 such sites. Immediate operand addresses (the byte AFTER
the CMP opcode):

    $82C6  LDA $0E    CMP #$06 → STA $0770  (game mode)
    $95B8  LDX $08    CMP #$0E → STA $07    (event/sprite idx)
    $BBBF  LDA $2A,X  CMP #$30 → STA $2A,X  (YEVLULIA family!)
    $BC13  LDA $075E  CMP #$64 → STA $075E  (coin tally, 100 = 1up)
    $C6C9  LDA $06DD  CMP #$FF → STA $06DD
    $CA17  LDA $00    CMP #$01 → STA $00    (temp)
    $E134  LDA $16,X  CMP #$05 → STA $00
    $F33C  LDA $07B2  CMP #$02 → STA $07C6

$BBBF is the most interesting lead for broken-block cascade: it resets
$2A,X — the exact zero-page slot YEVLULIA increments. Flipping its cap
away from $30 should let that counter run past its normal limit.
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
    (0x82C6, "CMP #$06 → STA $0770 (game mode reset)"),
    (0x95B8, "CMP #$0E → STA $07 (event/sprite idx)"),
    (0xBBBF, "CMP #$30 → STA $2A,X (YEVLULIA's counter cap)"),
    (0xBC13, "CMP #$64 → STA $075E (coin tally / 1-up)"),
    (0xC6C9, "CMP #$FF → STA $06DD"),
    (0xCA17, "CMP #$01 → STA $00"),
    (0xE134, "CMP #$05 → STA $00"),
    (0xF33C, "CMP #$02 → STA $07C6"),
]

SWEEP_VALUES: list[int] = list(range(0, 32))


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
    print(f"reset-gate sweep: {total} candidates across {len(TARGETS)} sites")

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
            f'<div class="code">{html.escape(code_str)}</div>'
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
                       filename="reset.html")
    print(f"wrote {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", default=str(HERE.parent.parent / "smb1.nes"))
    ap.add_argument("--out", default=str(HERE), type=str)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    main(args.rom, Path(args.out), seed=args.seed)
