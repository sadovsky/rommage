"""CheatNESEnv: NESEnv with Game Genie cheat injection.

Requires the patched lib_nes_env.so (see nes-py-cheats.README.md). Gracefully
degrades if the cheat symbols aren't present, so importing this module never
crashes on a stock nes-py install.
"""

from __future__ import annotations
import ctypes
import contextlib
import io
import os
from typing import Iterable, Union

# Silence gym deprecation warning at import.
_stderr_buf = io.StringIO()
with contextlib.redirect_stderr(_stderr_buf):
    from nes_py import NESEnv as _BaseNESEnv
    from nes_py.nes_env import _LIB

from genie import GenieCode, decode as decode_genie


class CheatsNotSupportedError(RuntimeError):
    """Raised when the installed lib_nes_env.so lacks cheat symbols."""


class TraceEvent(ctypes.Structure):
    _fields_ = [("pc", ctypes.c_ushort), ("opcode", ctypes.c_ubyte),
                ("a", ctypes.c_ubyte), ("x", ctypes.c_ubyte),
                ("y", ctypes.c_ubyte), ("sp", ctypes.c_ubyte),
                ("status", ctypes.c_ubyte), ("cycles", ctypes.c_int)]


def _install_cheat_signatures() -> bool:
    """Configure ctypes signatures for the cheat symbols. Returns True if
    all four symbols are present, False otherwise."""
    required = {
        "AddCheat": (
            [ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_int],
            ctypes.c_int,
        ),
        "RemoveCheat": (
            [ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_int],
            ctypes.c_int,
        ),
        "ClearCheats": ([ctypes.c_void_p], None),
        "CheatCount": ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (argtypes, restype) in required.items():
        if not hasattr(_LIB, name):
            return False
        fn = getattr(_LIB, name)
        fn.argtypes = argtypes
        fn.restype = restype
    return True


CHEATS_SUPPORTED = _install_cheat_signatures()
TRACE_SUPPORTED = all(hasattr(_LIB, name) for name in ("EnableTrace", "TraceCount", "TraceEvent", "ClearTrace"))
if TRACE_SUPPORTED:
    _LIB.EnableTrace.argtypes, _LIB.EnableTrace.restype = [ctypes.c_void_p, ctypes.c_int], None
    _LIB.TraceCount.argtypes, _LIB.TraceCount.restype = [ctypes.c_void_p], ctypes.c_int
    _LIB.TraceEvent.argtypes, _LIB.TraceEvent.restype = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(TraceEvent)], ctypes.c_int
    _LIB.ClearTrace.argtypes, _LIB.ClearTrace.restype = [ctypes.c_void_p], None


CodeLike = Union[str, GenieCode, tuple]


def _coerce_code(code: CodeLike) -> GenieCode:
    """Accept a Game Genie letter string, a GenieCode, or a raw tuple
    (cpu_addr, value, compare_or_None)."""
    if isinstance(code, GenieCode):
        return code
    if isinstance(code, str):
        return decode_genie(code)
    if isinstance(code, tuple) and len(code) == 3:
        cpu_addr, value, compare = code
        return GenieCode(
            address=cpu_addr & 0x7FFF,
            value=value & 0xFF,
            compare=None if compare is None else (compare & 0xFF),
        )
    raise TypeError(f"Can't coerce {type(code).__name__} to GenieCode")


class CheatNESEnv(_BaseNESEnv):
    """NESEnv with Game Genie cheat injection.

    Usage:
        env = CheatNESEnv("smb1.nes")
        env.reset()
        env.add_cheat("SXIOPO")   # infinite lives
        for _ in range(600):
            env.step(0)
        env.clear_cheats()
    """

    def __init__(self, rom_path: str):
        if not CHEATS_SUPPORTED:
            raise CheatsNotSupportedError(
                "The installed lib_nes_env.so does not export cheat symbols. "
                "Rebuild nes-py with the nes-py-cheats.patch applied, or use "
                "the stock NESEnv (without cheat support)."
            )
        super().__init__(rom_path)

    def add_cheat(self, code: CodeLike) -> int:
        gc = _coerce_code(code)
        compare = -1 if gc.compare is None else gc.compare
        return _LIB.AddCheat(self._env, gc.cpu_address, gc.value, compare)

    def add_cheats(self, codes: Iterable[CodeLike]) -> None:
        for code in codes:
            self.add_cheat(code)

    def remove_cheat(self, code: CodeLike) -> bool:
        gc = _coerce_code(code)
        compare = -1 if gc.compare is None else gc.compare
        return bool(_LIB.RemoveCheat(self._env, gc.cpu_address, gc.value, compare))

    def clear_cheats(self) -> None:
        _LIB.ClearCheats(self._env)

    def enable_trace(self, enabled: bool = True) -> None:
        if not TRACE_SUPPORTED:
            raise CheatsNotSupportedError("Installed emulator lacks native trace symbols.")
        _LIB.EnableTrace(self._env, int(enabled))

    def drain_trace(self):
        if not TRACE_SUPPORTED:
            raise CheatsNotSupportedError("Installed emulator lacks native trace symbols.")
        count = int(_LIB.TraceCount(self._env))
        for index in range(count):
            event = TraceEvent()
            if _LIB.TraceEvent(self._env, index, ctypes.byref(event)):
                yield event
        _LIB.ClearTrace(self._env)

    @property
    def cheat_count(self) -> int:
        return int(_LIB.CheatCount(self._env))
