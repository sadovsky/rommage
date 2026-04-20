"""Headless rollout runner.

Reuses a single CheatNESEnv per worker and swaps cheats between runs via
ClearCheats + AddCheat, avoiding per-candidate emulator init.

Fast-reset trick: call backup() once after the initial reset, then use
restore() between candidates. State-restore is much cheaper than full reset.
"""

from __future__ import annotations
import contextlib
import io
from typing import Iterable, Sequence

import numpy as np

with contextlib.redirect_stderr(io.StringIO()):
    from cheat_env import CheatNESEnv, CodeLike

# NES controller button bitmasks
NOOP   = 0x00
A      = 0x01
B      = 0x02
SELECT = 0x04
START  = 0x08
UP     = 0x10
DOWN   = 0x20
LEFT   = 0x40
RIGHT  = 0x80


class RolloutRunner:
    """Persistent emulator; runs many cheat configurations efficiently.

    If `warmup_frames > 0`, the first N frames are run with no cheats and no
    captures, then a state-backup is taken. Subsequent runs restore() back to
    that checkpoint instead of doing a full reset(). This skips title-screen
    boilerplate on every candidate.
    """

    def __init__(
        self,
        rom_path: str,
        warmup_sequence: Sequence[tuple[int, int]] | None = None,
    ):
        self.rom_path = rom_path
        self.env = CheatNESEnv(rom_path)
        self.env.reset()
        self._has_backup = False

        if warmup_sequence:
            self.env.clear_cheats()
            for action, duration in warmup_sequence:
                for _ in range(duration):
                    self.env.step(action)
            self.env._backup()
            self._has_backup = True

    def close(self) -> None:
        self.env.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _restart(self) -> None:
        if self._has_backup:
            self.env._restore()
        else:
            self.env.reset()

    def boot_check(
        self,
        boot_sequence: Sequence[tuple[int, int]],
        cheat: CodeLike,
    ) -> np.ndarray | None:
        """Reset fresh, apply cheat from frame 0, run boot_sequence, return
        the final frame. Returns None if the rollout raised (e.g. opcode fault).

        Leaves the env in a dirty state — caller must re-enter via run() which
        will _restart() back to the shared backup.

        Note: env.reset() in nes-py restores the backup if one exists, so we
        call _LIB.Reset directly to force a true fresh reset without touching
        the saved backup buffer.
        """
        from nes_py.nes_env import _LIB
        env = self.env
        _LIB.Reset(env._env)
        env.clear_cheats()
        env.add_cheat(cheat)
        try:
            for action, duration in boot_sequence:
                for _ in range(duration):
                    env.step(action)
        except Exception:
            return None
        return env.screen.copy()

    def run(
        self,
        input_sequence: Sequence[tuple[int, int]],
        cheats: Iterable[CodeLike] = (),
        capture_every: int = 30,
    ) -> np.ndarray:
        """Run one rollout, return captured frames (uint8, NHWC)."""
        env = self.env
        self._restart()
        env.clear_cheats()
        for code in cheats:
            env.add_cheat(code)

        captures = []
        frame_idx = 0
        for action, duration in input_sequence:
            for _ in range(duration):
                env.step(action)
                if frame_idx % capture_every == 0:
                    captures.append(env.screen.copy())
                frame_idx += 1

        if not captures:
            return np.empty((0, 240, 256, 3), dtype=np.uint8)
        return np.stack(captures, axis=0)


# Pre-baked input sequences -------------------------------------------------

def idle(frames: int = 600) -> list[tuple[int, int]]:
    return [(NOOP, frames)]


def press_start(
    pre: int = 120, hold: int = 10, post: int = 600
) -> list[tuple[int, int]]:
    return [(NOOP, pre), (START, hold), (NOOP, post)]


def walk_right(
    pre: int = 120, start_hold: int = 10, mid: int = 60, walk: int = 600,
) -> list[tuple[int, int]]:
    return [(NOOP, pre), (START, start_hold), (NOOP, mid), (RIGHT, walk)]


def random_mash(frames: int = 600, seed: int = 0) -> list[tuple[int, int]]:
    import random
    rng = random.Random(seed)
    seq = []
    remaining = frames
    while remaining > 0:
        btn = rng.choice([NOOP, A, B, LEFT, RIGHT, UP, DOWN, START])
        dur = min(remaining, rng.randint(20, 40))
        seq.append((btn, dur))
        remaining -= dur
    return seq
