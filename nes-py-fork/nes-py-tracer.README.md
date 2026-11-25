# nes-py instruction tracer patch

`nes-py-tracer.patch` adds optional **instruction-level CPU tracing** to
nes-py. It is independent of `nes-py-cheats.patch` but is developed on top of
it; apply the cheats patch first if you want both (the Python wrapper
`trace_env.py` subclasses `CheatNESEnv`).

> **Note.** Two tracer implementations were written concurrently. They are now
> unified: this patch is the single implementation, and it keeps the simpler
> `EnableTrace` / `TraceEvent(index)` / `ClearTrace` entry points from the other
> one alongside the filtered API below. The duplicate C++ hunks previously
> folded into `nes-py-cheats.patch` are superseded by this file; apply the
> cheats patch for cheats and this one for tracing.

It exists for the Game Genie / ACE audit work: the only way to know where the
6502 program counter actually goes is to look at every instruction, and the
single most interesting question — *does the PC ever leave PRG ROM?* — cannot
be answered by sampling RAM once per frame.

## What it adds

A `Tracer` owned by the `Emulator` and attached to the `CPU`. The hook sits at
the one instruction boundary in `CPU::cycle`, immediately after the opcode
fetch:

```cpp
const NES_Address trace_pc = register_PC;
NES_Byte op = bus.read(register_PC++);
++instruction_count;
if (tracer != nullptr && tracer->is_active())
    tracer->on_instruction(trace_pc, op, register_A, register_X,
                           register_Y, register_SP, flags.byte,
                           instruction_count);
```

**Cost when unused:** the tracer starts with no filters, so `is_active()` is
false and the hook is a single predictable branch. Behaviour is unchanged.

### Two independent filters

| filter | records |
|---|---|
| `TraceWatch(addr, 1)` | instructions starting at that CPU address |
| `TraceRecordNonROM(1)` | any instruction whose PC is below `$8000` |

The second is the derailment detector: on NROM all code lives in ROM, so an
instruction executing below `$8000` means the PC has gone into RAM or I/O
space. That is the precondition for arbitrary code execution.

### C API

| symbol | purpose |
|---|---|
| `EnableTrace(emu, on)` | record **every** instruction (unfiltered); off also clears |
| `TraceEvent(emu, i, out)` | copy one event by index |
| `ClearTrace(emu)` | discard recorded events |
| `TraceWatch(emu, addr, on)` | watch / unwatch a CPU address |
| `TraceClearWatches(emu)` | stop watching everything |
| `TraceRecordNonROM(emu, on)` | record execution outside PRG ROM |
| `TraceSetCapacity(emu, n)` | cap the retained event count |
| `TraceClear(emu)` | discard recorded events |
| `TraceCount(emu)` | number of recorded events |
| `TraceDropped(emu)` | events dropped because the buffer was full |
| `TraceActive(emu)` | whether any filter is active |
| `TraceGet(emu, out, max)` | copy events out as packed 16-byte records |

`TraceGet` record layout (little-endian):

```
offset  size  field
  0      2    pc
  2      1    opcode
  3      1    a
  4      1    x
  5      1    y
  6      1    sp
  7      1    status (flags byte)
  8      1    flags  (bit 0 = PC was outside PRG ROM)
  9      3    padding
 12      4    instruction index since reset
```

### Backup / restore

`Emulator::restore()` assigns `cpu = backup_cpu`, which would copy a stale
tracer pointer. Both `backup()` and `restore()` re-attach `&tracer`, so
tracing survives the state rollbacks the search harness relies on. The
`Tracer` is a member of `Emulator`, so its address is stable.

## Applying and building

```bash
cd /path/to/nes-py
git apply /path/to/rommage/nes-py-fork/nes-py-tracer.patch
python3 setup.py build_ext --inplace
```

Verify the symbols:

```bash
nm -D nes_py/lib_nes_env.cpython-*.so | grep ' T Trace'
```

Twelve trace symbols should appear (`EnableTrace`, `ClearTrace`, `TraceEvent`
and nine `Trace*`).

Unfiltered mode is convenient but expensive: an NES runs roughly 1.8 million
instructions per second, so `EnableTrace` is for short windows. For long runs
use `TraceWatch` / `TraceRecordNonROM`, which filter at the source and keep both
memory and cost bounded.

## Python usage

`genie_search/trace_env.py` wraps it:

```python
from trace_env import TraceNESEnv

env = TraceNESEnv("smb1.nes")
env.reset()
env.watch(0x8E04)          # SMB1's jump engine; A holds the dispatch index
env.record_non_rom(True)   # catch a derailed program counter
for _ in range(600):
    env.step(0)

for e in env.iter_non_rom():
    print("PC left ROM:", e.as_dict())
```

`trace_env.py` degrades gracefully: importing it against a stock build works,
and constructing `TraceNESEnv` raises `TracingNotSupportedError`.

## Caveat

nes-py's CPU core does not implement the undocumented 6502 opcodes; it prints
`failed to execute opcode` on **stdout** and continues. Real hardware executes
several of those as NOPs, so a derailed run diverges from hardware once it hits
one. The tracer records the derailment faithfully; what happens afterwards is
not hardware-accurate.
