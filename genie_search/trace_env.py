"""TraceNESEnv: CheatNESEnv plus instruction-level CPU tracing.

Requires a lib_nes_env.so built with the tracer patch (see
nes-py-fork/nes-py-tracer.README.md). Degrades gracefully if the symbols are
absent, so importing this module never breaks a stock install.

The tracer is inactive until a filter is set, so an untraced run costs one
predictable branch per instruction and behaves identically to an unpatched
build. Two independent filters:

    watch(addr)      record instructions starting at a CPU address
    record_non_rom() record any instruction executed outside PRG ROM, which is
                     the signal that the program counter has derailed into RAM
                     or I/O space -- the thing an ACE audit is looking for

Example:

    env = TraceNESEnv("smb1.nes")
    env.reset()
    env.watch(0x8E16)          # the game's only computed jump
    env.record_non_rom(True)   # catch a derailed PC
    for _ in range(600):
        env.step(0)
    for ev in env.trace_events():
        print(ev)
"""

from __future__ import annotations

import contextlib
import ctypes
import io
from typing import Iterable, Iterator, NamedTuple

with contextlib.redirect_stderr(io.StringIO()):
    from nes_py.nes_env import _LIB

from cheat_env import CheatNESEnv

#: set when the instruction executed outside PRG ROM ($0000-$7FFF)
TRACE_FLAG_NON_ROM = 0x01

#: bytes per packed record returned by TraceGet
_RECORD_SIZE = 16


class TracingNotSupportedError(RuntimeError):
    """Raised when the installed lib_nes_env.so lacks the tracer symbols."""


def _install_trace_signatures() -> bool:
    """Configure ctypes signatures for the tracer symbols. Returns True when
    all of them are present, False otherwise."""
    required = {
        "TraceWatch": ([ctypes.c_void_p, ctypes.c_uint, ctypes.c_int], None),
        "TraceClearWatches": ([ctypes.c_void_p], None),
        "TraceRecordNonROM": ([ctypes.c_void_p, ctypes.c_int], None),
        "TraceSetCapacity": ([ctypes.c_void_p, ctypes.c_uint], None),
        "TraceClear": ([ctypes.c_void_p], None),
        "TraceCount": ([ctypes.c_void_p], ctypes.c_uint),
        "TraceDropped": ([ctypes.c_void_p], ctypes.c_ulonglong),
        "TraceActive": ([ctypes.c_void_p], ctypes.c_int),
        "TraceGet": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint], ctypes.c_uint),
    }
    for name, (argtypes, restype) in required.items():
        if not hasattr(_LIB, name):
            return False
        fn = getattr(_LIB, name)
        fn.argtypes = argtypes
        fn.restype = restype
    return True


TRACING_SUPPORTED = _install_trace_signatures()


class TraceEvent(NamedTuple):
    """One executed instruction, as captured at the fetch."""
    index: int      # monotonic instruction number since reset
    pc: int
    opcode: int
    a: int
    x: int
    y: int
    sp: int
    status: int
    flags: int

    @property
    def non_rom(self) -> bool:
        """True when this instruction executed outside PRG ROM."""
        return bool(self.flags & TRACE_FLAG_NON_ROM)

    def as_dict(self, **extra) -> dict:
        """A JSON-friendly record, matching the audit's trace schema."""
        d = {
            "index": self.index,
            "pc": f"0x{self.pc:04X}",
            "opcode": f"0x{self.opcode:02X}",
            "a": f"0x{self.a:02X}",
            "x": f"0x{self.x:02X}",
            "y": f"0x{self.y:02X}",
            "sp": f"0x{self.sp:02X}",
            "status": f"0x{self.status:02X}",
            "non_rom": self.non_rom,
        }
        d.update(extra)
        return d


class TraceNESEnv(CheatNESEnv):
    """CheatNESEnv with instruction-level CPU tracing."""

    def __init__(self, rom_path: str):
        if not TRACING_SUPPORTED:
            raise TracingNotSupportedError(
                "The installed lib_nes_env.so does not export the tracer "
                "symbols. Rebuild nes-py with nes-py-tracer.patch applied.")
        super().__init__(rom_path)

    # ---- filters -----------------------------------------------------

    def watch(self, addresses: int | Iterable[int], on: bool = True) -> None:
        """Record instructions starting at these CPU address(es)."""
        if isinstance(addresses, int):
            addresses = (addresses,)
        for a in addresses:
            _LIB.TraceWatch(self._env, a & 0xFFFF, 1 if on else 0)

    def clear_watches(self) -> None:
        _LIB.TraceClearWatches(self._env)

    def record_non_rom(self, on: bool = True) -> None:
        """Record any instruction executed outside PRG ROM."""
        _LIB.TraceRecordNonROM(self._env, 1 if on else 0)

    def set_trace_capacity(self, n: int) -> None:
        _LIB.TraceSetCapacity(self._env, int(n))

    @property
    def tracing_active(self) -> bool:
        """Whether any filter is active (i.e. the hook does real work)."""
        return bool(_LIB.TraceActive(self._env))

    # ---- buffer ------------------------------------------------------

    def clear_trace(self) -> None:
        _LIB.TraceClear(self._env)

    @property
    def trace_count(self) -> int:
        return int(_LIB.TraceCount(self._env))

    @property
    def trace_dropped(self) -> int:
        """Events discarded because the buffer was full."""
        return int(_LIB.TraceDropped(self._env))

    def trace_events(self, limit: int | None = None) -> list[TraceEvent]:
        """Return the recorded events, decoded."""
        n = self.trace_count if limit is None else min(limit, self.trace_count)
        if n == 0:
            return []
        buf = ctypes.create_string_buffer(n * _RECORD_SIZE)
        got = int(_LIB.TraceGet(self._env, buf, n))
        raw = buf.raw
        out = []
        for i in range(got):
            r = raw[i * _RECORD_SIZE:(i + 1) * _RECORD_SIZE]
            out.append(TraceEvent(
                index=int.from_bytes(r[12:16], "little"),
                pc=r[0] | r[1] << 8,
                opcode=r[2], a=r[3], x=r[4], y=r[5],
                sp=r[6], status=r[7], flags=r[8],
            ))
        return out

    def iter_non_rom(self) -> Iterator[TraceEvent]:
        """Only the events where the program counter had left PRG ROM."""
        return (e for e in self.trace_events() if e.non_rom)
