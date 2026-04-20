# nes-py Game Genie fork

This patch adds CPU-read interception for Game Genie cheats to
[nes-py](https://github.com/Kautenja/nes-py).

## What it adds

Four new C API symbols exported from `lib_nes_env.so`:

```
int  AddCheat(Emulator*, unsigned int addr, unsigned char value, int compare)
int  RemoveCheat(Emulator*, unsigned int addr, unsigned char value, int compare)
void ClearCheats(Emulator*)
int  CheatCount(Emulator*)
```

`compare` is `-1` for 6-letter Game Genie codes (unconditional replace) or
`0..255` for 8-letter codes (replace only when the original ROM byte matches
`compare`). `addr` should be the full CPU address in `$8000..$FFFF`.

Cheats are consulted on every CPU read from `$8000..$FFFF` inside
`MainBus::read`. A shared `CheatTable` (held as `std::shared_ptr`) is owned
by the `Emulator` and referenced by both the live `bus` and the `backup_bus`,
so cheats survive `backup()/restore()` cycles.

## How to apply

```bash
git clone https://github.com/Kautenja/nes-py.git
cd nes-py
git apply /path/to/nes-py-cheats.patch
cd nes_py/nes
scons                                # produces lib_nes_env.so
cd ../..

# Option 1: install over your existing pip install
python -c "import nes_py, os; print(os.path.dirname(nes_py.__file__))"
# copy nes_py/nes/lib_nes_env.so into that directory, replacing the existing
# lib_nes_env.cpython-*.so

# Option 2: install from this source tree
pip install -e .
```

## Files touched

- `nes_py/nes/include/cheat.hpp` (new)
- `nes_py/nes/include/main_bus.hpp` — added `shared_ptr<CheatTable>` member
- `nes_py/nes/include/emulator.hpp` — added cheat API and backup/restore wiring
- `nes_py/nes/src/main_bus.cpp` — consult cheat table on PRG reads
- `nes_py/nes/src/emulator.cpp` — initialize shared cheat table in ctor
- `nes_py/nes/src/lib_nes_env.cpp` — export C symbols

## Verified

- Baseline build passes with changes applied.
- All four new symbols present in the resulting `.so`.
- Changes are non-invasive: when no cheats are active (`cheats->empty()`),
  the hot path adds a single branch per read with no heap or map access.
