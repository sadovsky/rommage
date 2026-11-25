"""Tests for the instruction tracer. Run: python3 test_trace_env.py

Checks the properties the ACE audit depends on:
  * tracing is inactive until a filter is set (no behaviour change when unused)
  * enabling tracing does not alter emulation
  * watched addresses are captured with correct register state
  * the non-ROM detector is armed and, on a healthy run, stays silent
  * tracing survives backup/restore
"""
import contextlib
import io
import sys

with contextlib.redirect_stderr(io.StringIO()):
    from trace_env import TraceNESEnv, TRACING_SUPPORTED

ROM = "/home/sadovsky/code/rommage/smb1.nes"
START = [(0, 40), (8, 10), (0, 60)]
JUMPENGINE, COMPUTED_JMP = 0x8E04, 0x8E16

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def boot(env, extra=0):
    for a, n in START:
        for _ in range(n):
            env.step(a)
    for _ in range(extra):
        env.step(128)


def test_inactive_by_default():
    env = TraceNESEnv(ROM); env.reset()
    check("inactive before any filter", not env.tracing_active)
    boot(env, 60)
    check("no events recorded while inactive", env.trace_count == 0,
          f"count={env.trace_count}")
    env.close()


def test_tracing_does_not_change_emulation():
    """The same inputs must produce the same RAM with and without tracing."""
    def run(trace):
        env = TraceNESEnv(ROM); env.reset()
        if trace:
            env.watch([JUMPENGINE, COMPUTED_JMP]); env.record_non_rom(True)
        boot(env, 180)
        snap = bytes(env.ram[:0x800])
        env.close()
        return snap
    check("tracing does not alter emulation", run(False) == run(True))


def test_watch_captures_dispatches():
    env = TraceNESEnv(ROM); env.reset()
    env.watch([JUMPENGINE, COMPUTED_JMP])
    boot(env, 120)
    ev = env.trace_events()
    at_engine = [e for e in ev if e.pc == JUMPENGINE]
    at_jmp = [e for e in ev if e.pc == COMPUTED_JMP]
    check("dispatches captured", len(at_engine) > 0, f"n={len(at_engine)}")
    check("engine entry and computed jump pair up",
          len(at_engine) == len(at_jmp), f"{len(at_engine)} vs {len(at_jmp)}")
    check("computed jump opcode is $6C (JMP indirect)",
          all(e.opcode == 0x6C for e in at_jmp))
    check("engine entry opcode is $0A (ASL A)",
          all(e.opcode == 0x0A for e in at_engine))
    check("no event is flagged non-ROM on a healthy run",
          not any(e.non_rom for e in ev))
    env.close()


def test_non_rom_detector_silent_on_healthy_run():
    env = TraceNESEnv(ROM); env.reset()
    env.record_non_rom(True)
    check("non-ROM filter activates the tracer", env.tracing_active)
    boot(env, 600)
    check("PC never leaves PRG ROM in normal play", env.trace_count == 0,
          f"count={env.trace_count}")
    env.close()


def test_capacity_and_dropped():
    env = TraceNESEnv(ROM); env.reset()
    env.set_trace_capacity(10)
    env.watch(JUMPENGINE)
    boot(env, 180)
    check("capacity respected", env.trace_count <= 10, f"count={env.trace_count}")
    check("overflow counted as dropped", env.trace_dropped > 0,
          f"dropped={env.trace_dropped}")
    env.clear_trace()
    check("clear empties the buffer", env.trace_count == 0)
    env.close()


def test_survives_backup_restore():
    env = TraceNESEnv(ROM); env.reset()
    boot(env)
    env.watch(JUMPENGINE)
    env._backup()
    for _ in range(30):
        env.step(128)
    env._restore()
    env.clear_trace()
    for _ in range(60):
        env.step(128)
    check("still recording after backup/restore", env.trace_count > 0,
          f"count={env.trace_count}")
    env.close()


if __name__ == "__main__":
    if not TRACING_SUPPORTED:
        sys.exit("lib_nes_env.so lacks the tracer symbols; apply nes-py-tracer.patch")
    print("instruction tracer tests")
    for fn in (test_inactive_by_default,
               test_tracing_does_not_change_emulation,
               test_watch_captures_dispatches,
               test_non_rom_detector_silent_on_healthy_run,
               test_capacity_and_dropped,
               test_survives_backup_restore):
        print(f"\n{fn.__name__}:")
        fn()
    print("\n" + ("ALL PASS" if not _fails else f"FAILURES: {_fails}"))
    sys.exit(1 if _fails else 0)
