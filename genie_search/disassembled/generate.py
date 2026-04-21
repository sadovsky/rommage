"""Targeted Game Genie code generator using disassembly-derived addresses.

Rather than brute-forcing the full 32KB PRG, we pick a small curated set of
ROM addresses organized by subsystem (audio / graphics / palettes / enemies /
player / timing) drawn from the SMB1 disassembly at
https://6502disassembly.com/nes-smb/SuperMarioBros.html.

For each address we apply a handful of byte mutations (NOP, BRK, bit-flip,
byte-invert) and run each candidate through:
  1. Reset + warmup (intro skip via Start → walk right)
  2. ~120 frames of scripted mixed input (walk right + occasional jumps)
  3. Capture end-frame, run the structural boot-safety check from analyze.py

Output is a single `gallery.html` grouped by category, showing the thumbnail,
the Game Genie letter code, and a boot-safe tag. Useful as a warm-start for
finding *interesting* codes without running the full search pipeline.
"""

from __future__ import annotations
import os
import sys
import time
import html
import random
from pathlib import Path

# Parent dir = genie_search, where cheat_env / analyze / genie live.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import contextlib
import io
with contextlib.redirect_stderr(io.StringIO()):
    from cheat_env import CheatNESEnv
from genie import GenieCode, encode
from analyze import _has_structure, _silence_c_fds
from search import read_prg_bytes, rom_byte_at
from runner import NOOP, A, B, START, UP, DOWN, LEFT, RIGHT
from PIL import Image


# ---------- curated addresses (from the disassembly) ----------
#
# Each entry is (cpu_addr, note). Original byte is read from the ROM at
# runtime — the disassembly reference is used only to pick which bytes to
# poke and what to name them.

CATEGORIES: dict[str, list[tuple[int, str]]] = {
    "audio": [
        (0x803E, "STA $4015 — APU channel enable"),
        (0x8043, "STA $2001 operand near sound init"),
        (0x80E4, "JSR SoundEngine"),
        (0x84E9, "LDA #$40 — 1-up Sfx code"),
        (0x84EB, "STA Square2SoundQueue"),
    ],
    "graphics": [
        (0x8004, "LDA #%00010000 — PPUCTRL init opcode"),
        (0x8006, "PPUCTRL init operand"),
        (0x80B1, "LDA #$02 — DMA page opcode"),
        (0x80B3, "STA $4014 — OAMDMA"),
        (0x80AE, "LDA #$00 — OAMADDR opcode"),
        (0x80B0, "OAMADDR operand"),
        (0x815F, "STA $2005 — scroll X"),
        (0x8165, "STA $2005 — scroll Y"),
        (0x816C, "STA $2000 — PPUCTRL with NMI"),
        (0x8227, "LDA #$F8 — off-screen Y"),
        (0x822A, "STA $0200,Y — OAM Y write"),
    ],
    "palettes": [
        (0x85BB, "palette entry 1 — water"),
        (0x85BC, "palette entry 2 — ground"),
        (0x85BD, "palette entry 3 — underground"),
        (0x85BE, "palette entry 4 — castle"),
        (0x85CF, "bg color variant"),
        (0x85D0, "bg color variant"),
        (0x85D1, "bg color (black)"),
        (0x85D2, "bg color (black)"),
    ],
    "enemies": [
        (0x8395, "STX $08 — enemy object offset"),
        (0x8397, "JSR EnemiesAndLoopsCore"),
        (0x8500, "LDY $06E5,X — Enemy_SprDataOffset"),
        (0x8503, "LDA $16,X — Enemy_ID"),
        (0x8505, "CMP #$12 — Spiny check"),
        (0x8509, "CMP #$0D — PiranhaPlant check"),
        (0x850D, "CMP #$05 — HammerBro check"),
    ],
    "player_physics": [
        (0x80F6, "LDA TimerControl"),
        (0x8119, "INC FrameCounter"),
        (0x83C1, "LDA Player_PageLoc"),
        (0x83C9, "CMP #$60 — scroll threshold"),
        (0x83D1, "JSR AutoControlPlayer"),
        (0x83E4, "ADC #$80 — scroll fractional"),
        (0x83E6, "LDA #$01 — scroll step"),
    ],
    "timing": [
        (0x8082, "SEI — disable interrupts"),
        (0x8085, "AND #%01111111 — NMI mask"),
        (0x809C, "ORA #%00011110 — display enable"),
        (0x80A1, "AND #%11100111 — display mask"),
        (0x80A3, "STA $2001 — PPUMASK"),
        (0x80E7, "JSR ReadJoypads"),
        (0x80EA, "JSR PauseRoutine"),
        (0x80ED, "JSR UpdateTopScore"),
        (0x8100, "DEC IntervalTimerControl"),
        (0x8111, "DEC timer array,X"),
        (0x8181, "RTI — end NMI"),
    ],
    # Expanded NMI-handler opcode bytes. VSZAPEPZ-style hits live here:
    # redefining one opcode byte in the 60×/sec interrupt handler tends to
    # produce slow-burn glitches (screen corruption, sprite drift, stuck
    # scroll) rather than outright crashes.
    "nmi_hotpath": [
        (0x8082, "LDA Mirror_PPU_CTRL_REG1"),
        (0x8087, "STA to mirror"),
        (0x808A, "AND name-table mask"),
        (0x808C, "STA $2000 (PPUCTRL)"),
        (0x808F, "LDA Mirror_PPU_CTRL_REG2"),
        (0x8092, "AND display bits mask"),
        (0x8094, "LDY DisableScreenFlag"),
        (0x8097, "BNE — skip if disabled"),
        (0x8099, "LDA reload reg2 mirror"),
        (0x809E, "STA back to mirror"),
        (0x80A6, "LDX $2002 (PPUSTATUS)"),
        (0x80A9, "LDA #$00"),
        (0x80AB, "JSR InitScroll"),
        (0x80AE, "STA $2003 (OAMADDR)"),
        (0x80B1, "LDA #$02"),
        (0x80B6, "LDX buffer control"),
        (0x80B9, "LDA indirect low"),
        (0x80BC, "STA at $00"),
        (0x80BE, "LDA indirect high"),
        (0x80C3, "JSR UpdateScreen"),
        (0x80C6, "LDY init offset"),
        (0x80CB, "CPX #$06"),
        (0x80CD, "BNE — loop"),
        (0x80CF, "INY"),
        (0x80D0, "LDX from table"),
        (0x80D3, "LDA #$00"),
        (0x80D5, "STA buffer offset"),
        (0x80D8, "STA buffer data"),
        (0x80DB, "STA reset control"),
        (0x80DE, "LDA reg2 reload"),
        (0x80E1, "STA $2001 (PPUMASK)"),
        (0x80F0, "LDA GamePauseStatus"),
        (0x80F3, "LSR — rotate right"),
        (0x80F4, "BCS — branch if set"),
        (0x80F9, "BEQ — timer zero"),
        (0x80FB, "DEC timer"),
        (0x80FE, "BNE — skip"),
        (0x8102, "DEC interval timer"),
        (0x8105, "BPL — loop"),
        (0x8107, "LDA #$14"),
        (0x8109, "STA reset value"),
        (0x810C, "LDX #$23"),
        (0x810E, "LDA timer array"),
        (0x8113, "DEC timer,X"),
        (0x8116, "DEX"),
        (0x8117, "BPL — loop"),
        (0x811B, "LDX offset init"),
        (0x811D, "LDY count init"),
        (0x811F, "LDA LFSR byte 1"),
        (0x8122, "AND bit 1"),
        (0x8124, "STA temp"),
        (0x8126, "LDA LFSR byte 2"),
        (0x8129, "AND bit 1"),
        (0x812B, "EOR bits"),
        (0x812D, "CLC"),
        (0x812E, "BEQ — skip"),
        (0x8130, "SEC"),
        (0x8131, "ROR register"),
        (0x8134, "INX"),
        (0x8135, "DEY"),
        (0x8136, "BNE — loop"),
        (0x813B, "BEQ — sprite0 skip"),
        (0x813D, "LDA $2002 (sprite0 wait)"),
        (0x8140, "AND sprite0 bit"),
        (0x8142, "BNE — wait loop"),
        (0x8144, "LDA GamePauseStatus"),
        (0x8147, "LSR"),
        (0x8148, "BCS"),
        (0x814A, "JSR MoveSpritesOffscreen"),
        (0x814D, "JSR SpriteShuffler"),
        (0x8150, "LDA $2002"),
        (0x8153, "AND sprite0"),
        (0x8155, "BEQ — wait for sprite0"),
        (0x8157, "LDY delay"),
        (0x8159, "DEY"),
        (0x815A, "BNE — tight delay loop"),
        (0x815C, "LDA HorizontalScroll"),
        (0x8162, "LDA VerticalScroll"),
        (0x8168, "LDA PPUCTRL mirror"),
        (0x816B, "PHA"),
        (0x816F, "LDA GamePauseStatus"),
        (0x8172, "LSR"),
        (0x8173, "BCS"),
        (0x8175, "JSR OperModeExecutionTree"),
        (0x8178, "LDA $2002"),
        (0x817B, "PLA"),
        (0x817C, "ORA #$80 (NMI enable)"),
        (0x817E, "STA $2000"),
    ],
}


# ---------- byte mutations ----------

# Why these specific XOR deltas: 6502 opcode encoding is aaa-bbb-cc where
# "cc" picks instruction group and "bbb" picks addressing mode. Flipping
# single bits often maps one legal instruction onto another legal one —
# e.g. XOR 0x20 flips LDA↔STA, EOR↔ADC, JMP↔JSR; XOR 0x04 shifts the
# addressing-mode field; XOR 0x40 swaps branch conditions (BCC↔BVC,
# BPL↔BVS). That's exactly the kind of mutation that produced VSZAPEPZ:
# AND (0x29) → DEC zp,X (0xD6).
MUTATIONS: list[tuple[str, callable]] = [
    ("inv",    lambda v: v ^ 0xFF),
    ("flip20", lambda v: v ^ 0x20),   # load↔store / JSR↔JMP / ADC↔EOR
    ("flip40", lambda v: v ^ 0x40),   # branch-condition + arithmetic shifts
    ("flip04", lambda v: v ^ 0x04),   # addressing-mode shift within family
    ("nop",    lambda v: 0xEA),
    ("lsb",    lambda v: v ^ 0x01),
]


# ---------- rollout ----------

WARMUP: list[tuple[int, int]] = [
    # Skip title screen: wait, press Start, walk right into gameplay.
    (NOOP, 60),
    (START, 10),
    (NOOP, 60),
    (RIGHT, 170),
]

def _random_rollout(seed: int, n_frames: int = 150) -> list[tuple[int, int]]:
    """Mixed input emphasising right-movement + jumps (what SMB actually is
    doing on level 1). Deterministic per-seed so thumbnails are reproducible."""
    rng = random.Random(seed)
    seq: list[tuple[int, int]] = []
    remaining = n_frames
    while remaining > 0:
        btn = rng.choice([
            RIGHT, RIGHT, RIGHT,          # right walking (most common)
            RIGHT | A, RIGHT | A,         # running jumps
            RIGHT | B,                    # run
            A,                            # in-place jump
            NOOP,                         # breather
        ])
        dur = min(remaining, rng.randint(4, 16))
        seq.append((btn, dur))
        remaining -= dur
    return seq


def run_candidate(
    rom_path: str, cheat: GenieCode | None, rollout: list[tuple[int, int]]
):
    """Boot ROM, apply cheat, run warmup + rollout, return the end frame.
    Runs in-process; caller must ensure the cheat won't crash the emulator
    (use `run_candidate_safe` for untrusted byte mutations)."""
    env = CheatNESEnv(rom_path)
    try:
        env.reset()
        if cheat is not None:
            env.add_cheat(cheat)
        for action, dur in WARMUP + rollout:
            for _ in range(dur):
                env.step(action)
        end_frame = env.screen.copy()
    finally:
        env.close()
    return end_frame


# ---------- crash-resilient worker ----------
#
# Arbitrary byte mutations can produce illegal memory accesses inside the
# native emulator and trip a SIGSEGV that Python can't catch. We therefore
# run every candidate inside a persistent subprocess: one task in flight at
# a time, with respawn-on-death so a single crash costs us one candidate
# instead of the whole run.

import multiprocessing as mp
import queue as _queue_mod


def _worker_main(rom_path: str, task_q, result_q) -> None:
    # Silence fd 1/2 so illegal-opcode spam from the C++ core goes nowhere.
    dn = os.open(os.devnull, os.O_WRONLY)
    os.dup2(dn, 1)
    os.dup2(dn, 2)
    os.close(dn)

    import contextlib as _c
    import io as _io
    with _c.redirect_stderr(_io.StringIO()):
        from cheat_env import CheatNESEnv as _CheatNESEnv
    from genie import GenieCode as _GC

    env = _CheatNESEnv(rom_path)
    try:
        while True:
            task = task_q.get()
            if task is None:
                return
            addr, val, cmp, actions = task
            try:
                env.reset()
                env.clear_cheats()
                if addr is not None:
                    env.add_cheat(_GC(addr & 0x7FFF, val, cmp))
                for action, dur in actions:
                    for _ in range(dur):
                        env.step(action)
                result_q.put(env.screen.copy())
            except Exception as e:
                result_q.put(("__err__", repr(e)))
    finally:
        try:
            env.close()
        except Exception:
            pass


def _run_with_respawn(
    rom_path: str,
    tasks: list[tuple],
    on_each,
    per_task_timeout: float = 20.0,
):
    """Send `tasks` through a persistent worker subprocess, one at a time,
    respawning the worker whenever it dies. Calls `on_each(i, task, frame)`
    for every task — with `frame=None` on crash/timeout/error.
    """
    ctx = mp.get_context("spawn")

    def _spawn():
        task_q = ctx.Queue()
        result_q = ctx.Queue()
        p = ctx.Process(
            target=_worker_main, args=(rom_path, task_q, result_q), daemon=True
        )
        p.start()
        return p, task_q, result_q

    worker, task_q, result_q = _spawn()
    n_crashes = 0
    try:
        for i, task in enumerate(tasks):
            if not worker.is_alive():
                try: worker.close()
                except Exception: pass
                worker, task_q, result_q = _spawn()
            task_q.put(task)
            try:
                item = result_q.get(timeout=per_task_timeout)
            except _queue_mod.Empty:
                # Worker crashed (SIGSEGV) or got stuck. Kill + respawn.
                if worker.is_alive():
                    try: worker.kill()
                    except Exception: pass
                    worker.join(timeout=2)
                n_crashes += 1
                on_each(i, task, None)
                worker, task_q, result_q = _spawn()
                continue
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "__err__":
                on_each(i, task, None)
                continue
            on_each(i, task, item)
    finally:
        try:
            task_q.put(None)
            worker.join(timeout=2)
        except Exception:
            pass
        if worker.is_alive():
            try: worker.kill()
            except Exception: pass
    return n_crashes


# ---------- gallery HTML ----------

CSS = """
body { font-family: ui-monospace, Menlo, Consolas, monospace;
       background: #111; color: #ddd; margin: 24px; }
h1 { font-size: 20px; color: #fff; }
h2 { font-size: 16px; color: #6ce06c; margin-top: 32px;
     border-bottom: 1px solid #333; padding-bottom: 6px; }
.meta { color: #888; font-size: 13px; margin-bottom: 24px; }
.baseline { display: flex; gap: 16px; align-items: flex-start;
            padding: 12px; background: #1a1a1a; border-radius: 6px;
            margin-bottom: 24px; }
.baseline img { image-rendering: pixelated; width: 256px; height: 240px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, 272px);
        gap: 16px; }
.card { background: #1a1a1a; padding: 8px; border-radius: 6px;
        border-left: 3px solid #333; }
.card.safe { border-left-color: #6ce06c; }
.card.unsafe { border-left-color: #d06c6c; opacity: 0.75; }
.card img { width: 256px; height: 240px; image-rendering: pixelated;
            display: block; }
.code { font-size: 15px; color: #fff; margin: 6px 0 2px; }
.addr { font-size: 11px; color: #888; }
.desc { font-size: 11px; color: #aaa; margin-top: 4px;
        min-height: 28px; }
.mut { font-size: 11px; color: #e8c96c; }
.boot { font-size: 11px; margin-top: 4px; }
.boot.safe { color: #6ce06c; }
.boot.unsafe { color: #d06c6c; }
"""


def write_gallery(out_dir: Path, baseline_path: str, cards_by_cat: dict[str, list[str]],
                  filename: str = "gallery.html"):
    total = sum(len(v) for v in cards_by_cat.values())
    n_safe = sum(
        c.count('class="boot safe"') for cards in cards_by_cat.values() for c in cards
    )
    sections = []
    for cat, cards in cards_by_cat.items():
        if not cards:
            continue
        sections.append(
            f'<h2>{html.escape(cat)} ({len(cards)})</h2>\n'
            f'<div class="grid">\n{chr(10).join(cards)}\n</div>'
        )

    page = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Disassembly-targeted Game Genie gallery</title>
<style>{CSS}</style>
</head><body>
<h1>Disassembly-targeted codes ({total} candidates · {n_safe} boot-safe)</h1>
<div class="meta">Addresses pulled from
  <a href="https://6502disassembly.com/nes-smb/SuperMarioBros.html"
     style="color:#6ce06c">the SMB1 disassembly</a>;
  each byte was mutated four ways (invert / NOP / zero / LSB-flip) then run
  through a scripted rollout (warmup → ~150 frames of mixed input).
</div>
<div class="baseline">
  <div>
    <div style="color:#888;font-size:12px">baseline (no cheat)</div>
    <img src="{html.escape(baseline_path)}" alt="baseline">
  </div>
</div>
{chr(10).join(sections)}
</body></html>
"""
    out = out_dir / filename
    out.write_text(page, encoding="utf-8")
    return out


# ---------- main ----------

def main(rom_path: str, out_dir: Path, seed: int = 0):
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    prg, _ = read_prg_bytes(rom_path)
    rollout = _random_rollout(seed)
    actions_full = list(WARMUP) + list(rollout)

    # Build task list: (addr, val, cmp, actions) + parallel metadata list
    # (category, addr, orig, new_val, mut_name, code_str, desc).
    tasks: list[tuple] = []
    meta: list[tuple] = []
    for category, entries in CATEGORIES.items():
        for addr, desc in entries:
            orig = rom_byte_at(prg, addr)
            for mut_name, mut_fn in MUTATIONS:
                new_val = mut_fn(orig) & 0xFF
                if new_val == orig:
                    continue
                gc = GenieCode(address=addr & 0x7FFF, value=new_val, compare=orig)
                code_str = encode(gc)
                tasks.append((addr & 0x7FFF, new_val, orig, actions_full))
                meta.append((category, addr, orig, new_val, mut_name, code_str, desc))

    total = len(tasks)
    print(f"generating {total} candidates across {len(CATEGORIES)} categories "
          f"(warmup {sum(d for _, d in WARMUP)} + "
          f"rollout {sum(d for _, d in rollout)} frames)")

    # Baseline — safe to run in-process since there's no cheat.
    print("capturing baseline...")
    baseline = run_candidate(rom_path, None, rollout)
    Image.fromarray(baseline).save(thumbs / "baseline.png", optimize=True)

    import numpy as np
    Image.fromarray(np.zeros((240, 256, 3), dtype=np.uint8)).save(
        thumbs / "_crash.png", optimize=True
    )

    cards_by_cat: dict[str, list[str]] = {k: [] for k in CATEGORIES}
    t0 = time.perf_counter()

    def on_each(i: int, task, frame):
        category, addr, orig, new_val, mut_name, code_str, desc = meta[i]
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
            f'<span style="color:#e8c96c">${new_val:02X}</span> '
            f'<span class="mut">({mut_name})</span></div>'
            f'<div class="desc">{html.escape(desc)}</div>'
            f'<div class="boot {boot}">'
            f'{"boot: safe" if boot == "safe" else "boot: UNSAFE"}'
            f'</div>'
            f'</div>'
        )
        cards_by_cat[category].append(card)

        done = i + 1
        if done % 5 == 0 or done == total:
            elapsed = time.perf_counter() - t0
            rate = done / elapsed if elapsed > 0 else 0.0
            eta = (total - done) / rate if rate > 0 else 0.0
            print(f"  {done:>3}/{total} ({100*done/total:>4.1f}%) "
                  f"@ {rate:.2f}/s eta {int(eta)}s "
                  f"[{category}:{code_str}:{boot}]", flush=True)

    n_crashes = _run_with_respawn(rom_path, tasks, on_each)
    print(f"\nruntime: {time.perf_counter() - t0:.1f}s "
          f"({n_crashes} worker crashes)")

    out = write_gallery(out_dir, "thumbs/baseline.png", cards_by_cat)
    print(f"wrote {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", default=str(HERE.parent.parent / "smb1.nes"))
    ap.add_argument("--out", default=str(HERE), type=str)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    main(args.rom, Path(args.out), seed=args.seed)
