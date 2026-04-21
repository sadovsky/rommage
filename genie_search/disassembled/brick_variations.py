"""Brick-break candidate sweep — RAM-poked Big Mario + scripted brick break.

The mushroom-pickup route proved unreliable (timing-sensitive, Mario often
died to the Goomba). Instead, this sweep pokes $0756=1 (PlayerSize=big)
and $079E=60 (StarInvincibleTimer) on every frame of the rollout, keeping
Mario Big and shrug-resistant regardless of Goomba collisions. The sweep
then runs him right and running-jumps into the brick row just past the
first pipe.

Capture timing: ~10 frames after the first brick shatters, while
fragments are still in the air. With a successful "persistent shatter"
cheat those fragments will still be visible well past their normal
~15-frame despawn window.

Candidates swept:
  $BCC0  bump-timer cap ($11)   — brick-bump cycle, small Mario
  $BBBF  $2A,X reset cap ($30)  — hammer/coin family reset (yevlulia sibling)
  $BBE3  $05 type gate (YEVLULIA itself, for reference)
"""

from __future__ import annotations
import sys
import time
import html
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import contextlib
import io
import os

with contextlib.redirect_stderr(io.StringIO()):
    from cheat_env import CheatNESEnv
from genie import GenieCode, encode, decode as decode_genie
from analyze import _has_structure
from search import read_prg_bytes, rom_byte_at
from runner import NOOP, A, B, START, RIGHT
from PIL import Image
import numpy as np

from generate import WARMUP, _run_with_respawn, write_gallery


# ---------- brick-break rollout ----------
#
# After WARMUP, Mario is at the level 1-1 start as Big Mario (thanks to
# APZLGK). The schedule below runs him right, jumps over two pipes, then
# mashes jump near the brick row to force a shatter before capture.

BRICK_ROLLOUT: list[tuple[int, int]] = [
    # Big Mario walks forward, jumps through the first few ?-blocks, then
    # running-jumps into the brick row just past the first pipe. RAM poke
    # (see _worker_main) keeps him Big throughout.
    (RIGHT, 30),
    (RIGHT | A, 18),    # clear Goomba
    (RIGHT, 40),
    (RIGHT | B, 30),    # start running
    (RIGHT | B | A, 20),  # first running-jump into brick row (shorter -6)
    # Fragments appear ~frame 124-155 post-warmup. Previous timing (26+8+10)
    # landed the capture at frame 162 — past fragment death. Shortening the
    # jump by 6 frames + shorter post-hang lands capture mid-fragment-alive.
    (RIGHT | B, 6),
    (NOOP, 10),
]

BIG_MARIO_CHEAT = None   # RAM poke in _worker_main handles it.

# Candidates to sweep.
#
# Original 3: immediate neighbors of the brick-bump code at $BBxx/$BCxx.
# Plus all 8 velocity-reset-gate sites (from reset_variations.py) and all
# 8 cascade-gate sites (from cascade_variations.py). A "persistent brick
# pieces" cheat should live at one of the gate sites that governs a
# fragment-lifetime counter.
TARGETS: list[tuple[int, str]] = [
    # Original candidates.
    (0xBCC0, "CMP #$11 bump-timer cap"),
    (0xBBBF, "CMP #$30 reset cap ($2A,X — YEVLULIA sibling)"),
    (0xBBE3, "CMP #$05 YEVLULIA type gate"),
    # Velocity-reset gates (from reset_variations).
    (0x82C6, "CMP #$06 → STA $0770 (game mode reset)"),
    (0x95B8, "CMP #$0E → STA $07 (event/sprite idx)"),
    (0xBC13, "CMP #$64 → STA $075E (coin tally / 1-up)"),
    (0xC6C9, "CMP #$FF → STA $06DD"),
    (0xCA17, "CMP #$01 → STA $00"),
    (0xE134, "CMP #$05 → STA $00"),
    (0xF33C, "CMP #$02 → STA $07C6"),
    # Cascade gates (from cascade_variations).
    (0x95D7, "CMP #$4B ; BNE ; INC $45"),
    (0xB2FA, "CMP #$05 ; BNE ; INC $5C"),
    (0xC0FC, "CMP #$06 ; BNE ; INC $D9"),
    (0xC258, "CMP #$0E ; BNE ; INC $39"),
    (0xCBFE, "CMP #$02 ; BNE ; INC $A0,X"),
    (0xE9EE, "CMP #$01 ; BEQ ; INC $02"),
    (0xF6DC, "CMP #$01 ; BNE ; INC $C7"),
    # Block-state counter ($0399) cap sites — discovered by scanning
    # PRG for INC/LDA/CMP on $0399.
    (0xB96C, "CMP #$08 after INC $0399 (block counter cap)"),
    (0xB99D, "CMP #$20 after STA $0399 (block counter cap 32)"),
    (0xB0B1, "CMP #$60 after LDA $0399 (block counter cap 96)"),
    # Fragment-despawn gates — found by RAM-diffing a shatter rollout
    # vs a no-shatter rollout, then locating CMP #imm instructions in
    # the $BE/$BF region that read fragment-slot bytes.
    (0xBE30, "CMP #$C2 after LDA ($06),Y — if==, STA #$00 (despawn Y?)"),
    (0xBEAD, "CMP #$F0 after LDA $D7,X (offscreen threshold)"),
    (0xBEC6, "CMP #$05 after AND #$0F (fragment type discriminator)"),
    # Fragment spawn velocity immediates — at $BE46 "LDA #$F0; STA $60,X"
    # and two LDA #velocity; STA store pairs right after. Changing these
    # should alter how fragments fly — slower=stay longer on screen.
    (0xBE47, "LDA #$F0 — fragment X-velocity seed"),
    (0xBE4D, "LDA #$FA — fragment Y-velocity seed"),
    (0xBE51, "LDA #$FC — fragment Y-velocity seed 2"),
    # Per-slot $0434,X counter increments/decrements — discovered via
    # RAM-diff $043E counter (values 0,16,32,48,64) → PRG scan for writes
    # to $0434 range. The +$10 step at $CF14 matches the diff pattern.
    (0xCF15, "ADC #$10 — $0434,X step of 16 (fragment lifetime?)"),
    (0xCBF7, "ADC #$01 — $0434,X step of 1"),
    (0xCC0C, "SBC #$01 — $0434,X step of -1"),
    (0xCF0D, "CMP #$08 gate before $0434,X += 16"),
    # Brick-bump cooldown timer. $B4A0 LDA #$20 / $B4A2 STA $0782.
    # The timer ticks down via DEC $0780,X at $8113 every frame. Raising
    # #$20 should keep brick-bump state active much longer — possibly
    # keeping fragments rendered.
    (0xB4A1, "LDA #$20 — bump-cooldown timer ($0782) init"),
    # Corresponding DEC $0780,X loop bound: $8101 LDX #$14 (20). If the
    # loop only walks X=0..$14, and fragment timer is at X=$82, maybe
    # the bound matters. (Actually $82 > $14 so the timer lives on
    # forever? Unless the other branch hits at X=$23.)
    (0x8101, "LDX #$14 — timer-tick loop upper bound"),
    (0x810D, "LDX #$23 — timer-tick loop upper bound (other branch)"),
    (0x8108, "LDA #$14 — reloaded counter value"),
]

# Re-running needs wider sweep on the $BE region to catch more subtle effects.


SWEEP_VALUES: list[int] = [0x00, 0x01, 0x02, 0x05, 0x07, 0x10, 0x20,
                           0x40, 0x80, 0xFE, 0xFF]


# Confirmed brick-persistence cheats + sibling / cousin variants found
# by opcode-flip sweep of the $BE00-$BF00 brick-bump bank. These are
# tested explicitly in main() so the gallery always has verified entries.
#
# The disassembled fragment-despawn block is at $BEA2-$BED3:
#     $BEA2  LDA #$F0
#     $BEA4  CMP $D9,X      ; Y-position ceiling check
#     $BEA6  BCS $BEAA      ; if A >= $D9,X, skip clamp
#     $BEA8  STA $D9,X      ; clamp Y to #$F0  <-- GXZUETSP neutralizes
#     $BEAA  LDA $D7,X      ; load X-position
#     $BEAC  CMP #$F0       ; off-right edge?
#     $BEAF  BCC $BED1      ; on-screen → go commit store
#     $BEB1  BCS $BECF      ; off-screen → run LDA #$00 first  <-- TOULXVGO redirects
#     $BECF  LDA #$00       ; zero A before commit
#     $BED1  STA $26,X      ; commit slot state  <-- GXILOTSP neutralizes
#
# CONFIRMED format: (addr, new_val, label). The compare byte is read
# from ROM, so these work like the sweep but with explicit values.
CONFIRMED: list[tuple[int, int, str]] = [
    # The three user-reported persistence cheats.
    (0xBED1, 0x24,
     "GXILOTSP: STA $26,X → BIT $26 — slot state never cleared"),
    (0xBEB2, 0x1E,
     "TOULXVGO: BCS offset $1C→$1E — skip LDA #$00 before slot commit"),
    (0xBEA8, 0x24,
     "GXZUETSP: STA $D9,X → BIT $D9 — fragment Y can exceed #$F0 "
     "(wraps around)"),
    # Sibling variants of TOULXVGO at $BEB2 — the BCS offset admits
    # multiple redirects that skip the LDA #$00.
    (0xBEB2, 0x18,
     "AOLLXVGO: BCS offset $1C→$18 — variant of TOULXVGO "
     "(displaced HUD + slot-state preservation)"),
    (0xBEB2, 0x1A,
     "ZOLLXVGO: BCS offset $1C→$1A — variant of TOULXVGO"),
    # Cousin: $BE32 BNE offset tweak in the fragment-spawn code.
    # Causes the brick-tile check ($BE2F CMP #$C2) to keep running
    # even when the tile isn't a brick — triggers visible fragment +
    # score-popup cascade near the jump.
    (0xBE32, 0x09,
     "PALLXVIE: BNE offset $0D→$09 — extends brick-bump so extra "
     "blocks emit fragment shards + 200pt popup"),
    # Fragment-draw routine at $EB80-$EBCC (reached via JSR from the bump
    # block). Three new hits, all in the code that pushes fragment OAM
    # bytes + the metatile pointer used by JMP $F282.
    (0xEBB2, 0x24,
     "GXLTXLSA: STA $01 → BIT $01 — metatile-pointer high byte not "
     "refreshed; fragments keep drawing on pipe past despawn"),
    (0xEBC9, 0x2C,
     "GXGVOUOO: STA $0210,Y → BIT $0210 — fragment OAM Y-position "
     "not written; one sprite slot keeps its old tile"),
    (0xEB96, 0x16,
     "TOPTVLZP: BCC offset $12→$16 — skips JSR $EBB7 and lands in "
     "the pointer-setup block; stray fragment sprite at lower-left"),
]


# ---------- worker with stacked cheats ----------
#
# generate._run_with_respawn only supports one Game Genie code per task.
# We need two (Big Mario + the test cheat), so inline a custom worker.

import multiprocessing as mp
import queue as _queue_mod


def _worker_main(rom_path, task_q, result_q, base_cheat):
    dn = os.open(os.devnull, os.O_WRONLY)
    os.dup2(dn, 1)
    os.dup2(dn, 2)
    os.close(dn)
    import contextlib as _c, io as _io
    with _c.redirect_stderr(_io.StringIO()):
        from cheat_env import CheatNESEnv as _Env
    from genie import GenieCode as _GC

    env = _Env(rom_path)
    warmup_len = sum(d for _, d in WARMUP)
    try:
        while True:
            task = task_q.get()
            if task is None:
                return
            addr, val, cmp, actions = task
            try:
                env.reset()
                env.clear_cheats()
                if base_cheat is not None:
                    env.add_cheat(base_cheat)
                if addr is not None:
                    env.add_cheat(_GC(addr & 0x7FFF, val, cmp))
                # Step through all actions; once warmup is done, poke
                # Big Mario + invincibility every frame. The one-time
                # $0754/$0033 poke at the transition suppresses the
                # size-change animation flag so Mario behaves as Big
                # immediately instead of being frozen in a grow/shrink
                # animation. Clear $079E 3 frames before capture so
                # Mario renders visibly in the final frame.
                total_frames = sum(d for _, d in actions)
                frame = 0
                transitioned = False
                for action, dur in actions:
                    for _ in range(dur):
                        if frame >= warmup_len:
                            if not transitioned:
                                env.ram[0x0754] = 0
                                env.ram[0x0033] = 1
                                transitioned = True
                            env.ram[0x0756] = 1   # PlayerSize = big
                            # Invincibility keeps Goomba collisions from
                            # interrupting Mario's physics, but $079E
                            # triggers rainbow-flicker rendering. Force
                            # the timer to zero near capture so Mario
                            # renders normally in the thumbnail.
                            if frame < total_frames - 6:
                                env.ram[0x079E] = 60
                            else:
                                env.ram[0x079E] = 0
                        env.step(action)
                        frame += 1
                result_q.put(env.screen.copy())
            except Exception as e:
                result_q.put(("__err__", repr(e)))
    finally:
        try:
            env.close()
        except Exception:
            pass


def _run_stacked(rom_path, tasks, on_each, base_cheat,
                 per_task_timeout: float = 20.0):
    ctx = mp.get_context("spawn")

    def _spawn():
        task_q = ctx.Queue()
        result_q = ctx.Queue()
        p = ctx.Process(
            target=_worker_main,
            args=(rom_path, task_q, result_q, base_cheat),
            daemon=True,
        )
        p.start()
        return p, task_q, result_q

    worker, task_q, result_q = _spawn()
    n_crashes = 0
    try:
        for i, task in enumerate(tasks):
            if not worker.is_alive():
                worker, task_q, result_q = _spawn()
            task_q.put(task)
            try:
                item = result_q.get(timeout=per_task_timeout)
            except _queue_mod.Empty:
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


# ---------- main ----------

def main(rom_path: str, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs = out_dir / "thumbs_brick"
    thumbs.mkdir(parents=True, exist_ok=True)

    prg, _ = read_prg_bytes(rom_path)
    actions_full = list(WARMUP) + list(BRICK_ROLLOUT)

    big_mario_gc = decode_genie(BIG_MARIO_CHEAT) if BIG_MARIO_CHEAT else None

    print(f"rollout: {sum(d for _,d in WARMUP)} warmup + "
          f"{sum(d for _,d in BRICK_ROLLOUT)} brick frames")
    print(f"base cheat: {BIG_MARIO_CHEAT}")

    tasks: list[tuple] = []
    meta: list[tuple] = []

    # Baseline task: no test cheat (only Big Mario).
    tasks.append((None, 0, 0, actions_full))
    meta.append(("baseline", 0, 0, 0, "baseline (Big Mario only)", ""))

    # Confirmed cheats go first so the gallery always has the known-good
    # examples at top, even if the full sweep is truncated.
    for addr, new_val, label in CONFIRMED:
        orig = rom_byte_at(prg, addr)
        gc = GenieCode(address=addr & 0x7FFF, value=new_val, compare=orig)
        code_str = encode(gc)
        tasks.append((addr & 0x7FFF, new_val, orig, actions_full))
        meta.append((f"${addr:04X}", addr, orig, new_val, code_str, label))

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
    print(f"brick-break sweep: {total} tasks "
          f"({len(TARGETS)} sites × {len(SWEEP_VALUES)}ish values + baseline)")

    Image.fromarray(np.zeros((240, 256, 3), dtype=np.uint8)).save(
        thumbs / "_crash.png", optimize=True
    )

    cards_by_addr: dict[str, list[str]] = {"baseline": []}
    for a, _, _ in CONFIRMED:
        cards_by_addr.setdefault(f"${a:04X}", [])
    cards_by_addr.update({f"${a:04X}": cards_by_addr.get(f"${a:04X}", [])
                          for a, _ in TARGETS})
    t0 = time.perf_counter()

    def on_each(i, task, frame):
        m = meta[i]
        if m[0] == "baseline":
            addr_key = "baseline"
            code_str = m[4]
            label = m[5]
            addr, orig, new_val = 0, 0, 0
        else:
            addr_key, addr, orig, new_val, code_str, label = m

        if frame is None:
            boot = "unsafe"
            thumb_path = "thumbs_brick/_crash.png"
        else:
            boot = "safe" if _has_structure(frame) else "unsafe"
            thumb_name = (f"baseline.png" if m[0] == "baseline"
                          else f"{code_str}.png")
            Image.fromarray(frame).save(thumbs / thumb_name, optimize=True)
            thumb_path = f"thumbs_brick/{thumb_name}"

        if m[0] == "baseline":
            addr_html = "(no test cheat)"
        else:
            addr_html = (f'${addr:04X} <span style="color:#555">'
                        f'${orig:02X}</span>&nbsp;&rarr;&nbsp;'
                        f'<span style="color:#e8c96c">${new_val:02X}</span>')

        card = (
            f'<div class="card {boot}">'
            f'<img src="{html.escape(thumb_path)}" '
            f'alt="{html.escape(code_str)}" loading="lazy">'
            f'<div class="code">{html.escape(code_str)}</div>'
            f'<div class="addr">{addr_html}</div>'
            f'<div class="desc">{html.escape(label)}</div>'
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

    n_crashes = _run_stacked(rom_path, tasks, on_each, big_mario_gc)
    print(f"\nruntime: {time.perf_counter() - t0:.1f}s "
          f"({n_crashes} worker crashes)")

    out = write_gallery(out_dir, "thumbs_brick/baseline.png", cards_by_addr,
                       filename="brick.html")
    print(f"wrote {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", default=str(HERE.parent.parent / "smb1.nes"))
    ap.add_argument("--out", default=str(HERE), type=str)
    args = ap.parse_args()
    main(args.rom, Path(args.out))
