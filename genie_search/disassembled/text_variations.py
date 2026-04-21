"""Surgical text mutations: swap individual characters in SMB1 strings.

AOLEVPTA (TELEVO at idx 16) renders text but corrupts ground tiles, because
$0773 = VRAM_Buffer_AddrCtrl dispatches an entire write block — and that
block can span more than just a text region.

This script takes the opposite approach: instead of redirecting the
dispatcher, mutate one byte *inside* a known text string table. Each byte
is a tile index (SMB1 encodes '0'-'9' = $00-$09, 'A'-'Z' = $0A-$23,
' ' = $24). Changing one byte swaps one letter — nothing else moves, no
tile corruption, no dispatcher surprises.

Target strings (canonical PRG locations, verified):
    MARIO     @ $8755
    WORLD     @ $875D
    TIME      @ $8764
    GAME OVER @ $87B6
    LUIGI     @ $87ED

For each character byte, the sweep swaps in a handful of letter tile
indices to produce visible letter changes. Result is boring-but-clean:
"MARIO" becomes "MARIP" or "MAQIO" with the rest of the screen intact.
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


# ---------- string-table targets ----------
#
# (base_addr, text) — the text is used to label each byte with the letter
# it originally encoded so cards are easy to read.

STRINGS: list[tuple[int, str]] = [
    (0x8755, "MARIO"),
    (0x875D, "WORLD"),
    (0x8764, "TIME"),
    (0x87B6, "GAME OVER"),
    (0x87ED, "LUIGI"),
    (0x87A3, "TIME UP"),
    (0x87C3, "WELCOME TO WARP ZONE"),
]


def _tile(ch: str) -> int:
    """SMB1 tile index for a character in its font encoding."""
    if "0" <= ch <= "9":
        return ord(ch) - ord("0")
    if "A" <= ch <= "Z":
        return 0x0A + (ord(ch) - ord("A"))
    if ch == " ":
        return 0x24
    raise ValueError(ch)


# ---------- per-byte mutations ----------
#
# For each character byte in a string, swap to these replacement tiles.
# Pick visually-distinctive letters so changes are obvious on screen.

REPLACEMENT_LETTERS: list[str] = ["Q", "Z", "X", "O", "A", "1", " "]


# ---------- main ----------

def main(rom_path: str, out_dir: Path, seed: int = 0):
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    prg, _ = read_prg_bytes(rom_path)
    rollout = _random_rollout(seed)
    actions_full = list(WARMUP) + list(rollout)

    # Sanity-check the string bases match what's in the ROM.
    for base, s in STRINGS:
        for i, ch in enumerate(s):
            got = rom_byte_at(prg, base + i)
            want = _tile(ch)
            if got != want:
                raise AssertionError(
                    f"string mismatch at ${base+i:04X}: got ${got:02X} "
                    f"but {ch!r} should be ${want:02X}"
                )

    tasks: list[tuple] = []
    meta: list[tuple] = []
    for base, s in STRINGS:
        for i, ch in enumerate(s):
            if ch == " ":
                continue
            addr = base + i
            orig = _tile(ch)
            for repl in REPLACEMENT_LETTERS:
                new_val = _tile(repl)
                if new_val == orig:
                    continue
                # Build the display label: show the string with this
                # char swapped, e.g. MARIO with idx 2 → Q → "MAQIO".
                mutated = s[:i] + repl + s[i+1:]
                gc = GenieCode(address=addr & 0x7FFF, value=new_val, compare=orig)
                code_str = encode(gc)
                tasks.append((addr & 0x7FFF, new_val, orig, actions_full))
                meta.append((s, addr, orig, new_val, code_str, mutated, ch, repl))

    total = len(tasks)
    print(f"text sweep: {total} candidates across {len(STRINGS)} strings "
          f"(warmup {sum(d for _, d in WARMUP)} + "
          f"rollout {sum(d for _, d in rollout)} frames)")

    print("capturing baseline...")
    baseline = run_candidate(rom_path, None, rollout)
    Image.fromarray(baseline).save(thumbs / "baseline.png", optimize=True)
    Image.fromarray(np.zeros((240, 256, 3), dtype=np.uint8)).save(
        thumbs / "_crash.png", optimize=True
    )

    cards_by_str: dict[str, list[str]] = {s: [] for _, s in STRINGS}
    t0 = time.perf_counter()

    def on_each(i: int, task, frame):
        s, addr, orig, new_val, code_str, mutated, ch, repl = meta[i]

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
            f'<div class="desc">{html.escape(s)} &rarr; '
            f'<span style="color:#e8c96c">{html.escape(mutated)}</span> '
            f'<span style="color:#888">({ch}&rarr;{repl})</span></div>'
            f'<div class="boot {boot}">'
            f'{"boot: safe" if boot == "safe" else "boot: UNSAFE"}'
            f'</div>'
            f'</div>'
        )
        cards_by_str[s].append(card)

        done = i + 1
        if done % 10 == 0 or done == total:
            elapsed = time.perf_counter() - t0
            rate = done / elapsed if elapsed > 0 else 0.0
            eta = (total - done) / rate if rate > 0 else 0.0
            print(f"  {done:>3}/{total} ({100*done/total:>4.1f}%) "
                  f"@ {rate:.2f}/s eta {int(eta)}s "
                  f"[{s}:{code_str}:{boot}]", flush=True)

    n_crashes = _run_with_respawn(rom_path, tasks, on_each)
    print(f"\nruntime: {time.perf_counter() - t0:.1f}s "
          f"({n_crashes} worker crashes)")

    out = write_gallery(out_dir, "thumbs/baseline.png", cards_by_str,
                       filename="text.html")
    print(f"wrote {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", default=str(HERE.parent.parent / "smb1.nes"))
    ap.add_argument("--out", default=str(HERE), type=str)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    main(args.rom, Path(args.out), seed=args.seed)
