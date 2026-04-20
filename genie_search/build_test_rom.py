"""Build a minimal NROM iNES ROM that exercises the cheat system.

The test program is:
    reset:
        LDA #$42        ; A <- 0x42
        STA $0200       ; RAM[0x0200] = 0x42   (baseline)
        LDA $8010       ; A <- read from ROM offset $8010 (will be 0xAA)
        STA $0201       ; RAM[0x0201] = value read from $8010
    loop:
        JMP loop

Address $8010 in PRG is chosen deliberately: we'll set a Game Genie cheat
targeting $8010 with value 0x99 and see RAM[0x0201] change from 0xAA to 0x99.

The PRG is 16KB (NROM-128). Reset vector points to $C000 (mirror of $8000).
"""

from pathlib import Path
import struct


def build_test_rom() -> bytes:
    prg = bytearray(0x4000)  # 16KB PRG

    # The ROM is mapped at $C000-$FFFF (16KB NROM mirrors $8000-$BFFF into
    # $C000-$FFFF, so file offset 0 == CPU $8000 and also CPU $C000).
    # Place code at start of file, which will be reached via reset vector.
    # Opcodes (6502):
    #   A9 42      LDA #$42
    #   8D 00 02   STA $0200
    #   AD 10 80   LDA $8010
    #   8D 01 02   STA $0201
    #   4C 09 C0   JMP $C009   (back to "LDA $8010" so we see live cheat toggles)
    # Wait -- JMP back to LDA $8010 means $C006 (offset 6: AD 10 80)? Let's
    # lay this out more carefully.

    code = bytes([
        0xA9, 0x42,                # 0: LDA #$42
        0x8D, 0x00, 0x02,          # 2: STA $0200
        0xAD, 0x10, 0x80,          # 5: LDA $8010      <- target for cheat
        0x8D, 0x01, 0x02,          # 8: STA $0201
        0x4C, 0x05, 0xC0,          # 11: JMP $C005 (loop back to LDA $8010)
    ])
    prg[0:len(code)] = code

    # Place the target byte 0xAA at offset 0x10 so CPU $8010 / $C010 reads 0xAA
    prg[0x10] = 0xAA

    # Reset vector at $FFFC/$FFFD -> $C000 (start of our code)
    # For a 16KB NROM, file offset $3FFC = CPU $FFFC
    prg[0x3FFC] = 0x00
    prg[0x3FFD] = 0xC0
    # IRQ/BRK vector at $FFFE -> $C000 too (doesn't matter for this test)
    prg[0x3FFE] = 0x00
    prg[0x3FFF] = 0xC0
    # NMI vector at $FFFA -> $C000
    prg[0x3FFA] = 0x00
    prg[0x3FFB] = 0xC0

    # 8KB CHR (blank)
    chr_ = bytes(0x2000)

    # iNES header: "NES\x1A", 1 PRG bank (16KB), 1 CHR bank (8KB), mapper 0
    header = bytearray(16)
    header[0:4] = b"NES\x1A"
    header[4] = 1    # 1 x 16KB PRG
    header[5] = 1    # 1 x 8KB CHR
    header[6] = 0    # flags6: mapper 0 low nibble, horizontal mirroring
    header[7] = 0    # flags7: mapper 0 high nibble

    return bytes(header) + bytes(prg) + chr_


if __name__ == "__main__":
    rom = build_test_rom()
    out = Path(__file__).parent / "test_rom.nes"
    out.write_bytes(rom)
    print(f"Wrote {out} ({len(rom)} bytes)")
