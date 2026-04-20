"""Smoke test: verify cheats actually intercept CPU reads.

Baseline: RAM[0x0201] should read 0xAA (the byte at CPU $8010).
With cheat ($8010, 0x99, compare=-1): RAM[0x0201] should read 0x99.
With 8-letter cheat ($8010, 0x77, compare=0xAA): RAM[0x0201] should read 0x77.
With 8-letter cheat ($8010, 0x77, compare=0x00): cheat doesn't match, 0xAA.
"""

import contextlib
import io
import os
with contextlib.redirect_stderr(io.StringIO()):
    from cheat_env import CheatNESEnv

ROM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_rom.nes")


def run(cheats, frames=5):
    env = CheatNESEnv(ROM)
    env.reset()
    for code in cheats:
        env.add_cheat(code)
    # Run a few frames so the CPU has plenty of time to execute our code
    for _ in range(frames):
        env.step(0)
    # RAM[0x0200] is the sanity-check byte 0x42
    # RAM[0x0201] is the byte read via our cheat-target address
    val_sanity = int(env.ram[0x0200])
    val_test = int(env.ram[0x0201])
    env.close()
    return val_sanity, val_test


print("Test 1: baseline, no cheats")
s, t = run([])
print(f"  RAM[0x0200]=0x{s:02X}  RAM[0x0201]=0x{t:02X}")
assert s == 0x42, f"sanity byte wrong: got 0x{s:02X}"
assert t == 0xAA, f"expected baseline 0xAA, got 0x{t:02X}"
print("  OK")

print("\nTest 2: 6-letter cheat ($8010 -> 0x99)")
from genie import GenieCode
s, t = run([GenieCode(address=0x0010, value=0x99, compare=None)])
print(f"  RAM[0x0200]=0x{s:02X}  RAM[0x0201]=0x{t:02X}")
assert t == 0x99, f"expected 0x99, got 0x{t:02X}"
print("  OK")

print("\nTest 3: 8-letter cheat with matching compare ($8010 -> 0x77 if 0xAA)")
s, t = run([GenieCode(address=0x0010, value=0x77, compare=0xAA)])
print(f"  RAM[0x0200]=0x{s:02X}  RAM[0x0201]=0x{t:02X}")
assert t == 0x77, f"expected 0x77, got 0x{t:02X}"
print("  OK")

print("\nTest 4: 8-letter cheat with wrong compare (should NOT fire)")
s, t = run([GenieCode(address=0x0010, value=0x77, compare=0x00)])
print(f"  RAM[0x0200]=0x{s:02X}  RAM[0x0201]=0x{t:02X}")
assert t == 0xAA, f"expected 0xAA (no fire), got 0x{t:02X}"
print("  OK")

print("\nTest 5: add + remove + verify baseline is restored")
from cheat_env import CheatNESEnv
env = CheatNESEnv(ROM)
env.reset()
env.add_cheat(GenieCode(address=0x0010, value=0x99, compare=None))
for _ in range(3):
    env.step(0)
assert env.ram[0x0201] == 0x99
assert env.cheat_count == 1
env.remove_cheat(GenieCode(address=0x0010, value=0x99, compare=None))
assert env.cheat_count == 0
for _ in range(3):
    env.step(0)
assert env.ram[0x0201] == 0xAA, f"got 0x{env.ram[0x0201]:02X}"
env.close()
print("  OK")

print("\nALL TESTS PASS")
