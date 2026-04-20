"""Game Genie code encoding/decoding for NES.

Decoding equations (verified against GOSSIP -> D1DD:14 and
ZEXPYGLA -> 94A7?03:02):

  n0..nN are the nibbles of the code, n0 being the first letter.

  addr = 0x8000
       | ((n3 & 7) << 12)
       | ((n5 & 7) <<  8)
       | ((n4 & 8) <<  8)
       | ((n2 & 7) <<  4)
       | ((n1 & 8) <<  4)
       |  (n4 & 7)
       |  (n3 & 8)

  6-letter:
    data = ((n1 & 7) << 4) | ((n0 & 8) << 4) | (n0 & 7) | (n5 & 8)

  8-letter:
    data    = ((n1 & 7) << 4) | ((n0 & 8) << 4) | (n0 & 7) | (n7 & 8)
    compare = ((n7 & 7) << 4) | ((n6 & 8) << 4) | (n6 & 7) | (n5 & 8)

The high bit of n2 is unused by the decoder -> two 6-letter codes can decode
to the same (addr, data). Our encoder always emits n2's high bit = 0.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Iterable


GENIE_ALPHABET = "APZLGITYEOXUKSVN"
_LETTER_TO_NIB = {c: i for i, c in enumerate(GENIE_ALPHABET)}


@dataclass(frozen=True)
class GenieCode:
    address: int           # 15-bit code address (CPU addr = 0x8000 | address)
    value: int             # 8-bit replacement
    compare: Optional[int] # 8-bit compare (None = 6-letter)

    @property
    def cpu_address(self) -> int:
        return 0x8000 | (self.address & 0x7FFF)

    def __str__(self) -> str:
        return encode(self)


def _letters_to_nibs(code: str) -> list[int]:
    code = code.strip().upper()
    try:
        return [_LETTER_TO_NIB[c] for c in code]
    except KeyError as e:
        raise ValueError(f"Invalid Game Genie letter: {e.args[0]!r}") from None


def _nibs_to_letters(nibs: list[int]) -> str:
    return "".join(GENIE_ALPHABET[x & 0xF] for x in nibs)


def decode(code: str) -> GenieCode:
    n = _letters_to_nibs(code)
    if len(n) not in (6, 8):
        raise ValueError(f"Code must be 6 or 8 letters, got {len(n)}")

    addr = (
        ((n[3] & 7) << 12) |
        ((n[5] & 7) <<  8) |
        ((n[4] & 8) <<  8) |
        ((n[2] & 7) <<  4) |
        ((n[1] & 8) <<  4) |
         (n[4] & 7)        |
         (n[3] & 8)
    )
    # address above is the offset from $8000, so it's a 15-bit value
    # (top bit is never set). cpu_address property re-adds 0x8000.

    if len(n) == 6:
        data = ((n[1] & 7) << 4) | ((n[0] & 8) << 4) | (n[0] & 7) | (n[5] & 8)
        return GenieCode(addr, data, None)

    data = ((n[1] & 7) << 4) | ((n[0] & 8) << 4) | (n[0] & 7) | (n[7] & 8)
    cmp_ = ((n[7] & 7) << 4) | ((n[6] & 8) << 4) | (n[6] & 7) | (n[5] & 8)
    return GenieCode(addr, data, cmp_)


def encode(code: GenieCode) -> str:
    a = code.address & 0x7FFF
    d = code.value & 0xFF
    n = [0] * (8 if code.compare is not None else 6)

    # Invert the address formula.
    n[3] |= (a >> 12) & 0x7                # addr[14:12] -> n3[2:0]
    n[5] |= (a >>  8) & 0x7                # addr[10:8]? careful
    n[4] |= ((a >> 11) & 0x1) << 3         # addr[11]    -> n4[3]? careful
    n[2] |= (a >>  4) & 0x7                # addr[6:4]   -> n2[2:0]
    n[1] |= ((a >>  7) & 0x1) << 3         # addr[7]     -> n1[3]? careful
    n[3] |= ((a >>  3) & 0x1) << 3         # addr[3]     -> n3[3]
    n[4] |=  a        & 0x7                # addr[2:0]   -> n4[2:0]

    # Hmm -- I need to re-derive the inverse from the forward formula
    # carefully. Let me redo with explicit bit-by-bit mapping.
    n = [0] * (8 if code.compare is not None else 6)

    # Forward: addr =
    #   ((n3 & 7) << 12)  -> addr bits 14,13,12  from n3 bits 2,1,0
    #   ((n5 & 7) <<  8)  -> addr bits 10, 9, 8? No: (n5&7)<<8 = bits 10,9,8
    #                        Wait, (n5&7) is 3 bits, << 8 makes bits 10,9,8.
    #                        But that conflicts with the next line's bit 8.
    #                        Reconciling with canonical example:
    #                        SXIOPO -> 0x91D9 (0x8000+0x11D9)
    #                        SXIOPO nibs: S=D, X=A, I=5, O=9, P=1, O=9
    #                        n3=9, n5=9, n4=1, n2=5, n1=A, n0=D
    #                        addr = (9&7)<<12 | (9&7)<<8 | (1&8)<<8
    #                             | (5&7)<<4 | (A&8)<<4 | (1&7) | (9&8)
    #                             = 1<<12 | 1<<8 | 0<<8 | 5<<4 | 8<<4 | 1 | 8
    #                             = 0x1000 | 0x100 | 0 | 0x50 | 0x80 | 1 | 8
    #                             = 0x11D9  ✓
    # So (n5 & 7) << 8 occupies addr bits 10, 9, 8. But (n4 & 8) << 8 also
    # lands on addr bit 11 (because 8<<8 = 0x800 = bit 11). OH -- (n4&8)
    # is already bit 3 of n4 shifted; value 8 << 8 = 0x800 = bit 11. Yes.
    # So the address bit layout from the forward formula:
    #   n3 bits 2,1,0 -> addr bits 14,13,12
    #   n5 bits 2,1,0 -> addr bits 10, 9, 8
    #   n4 bit 3      -> addr bit 11
    #   n2 bits 2,1,0 -> addr bits  6, 5, 4
    #   n1 bit 3      -> addr bit  7
    #   n3 bit 3      -> addr bit  3
    #   n4 bits 2,1,0 -> addr bits  2, 1, 0

    n[3] |= (a >> 12) & 0x7           # addr[14:12]
    n[5] |= (a >>  8) & 0x7           # addr[10:8]
    n[4] |= ((a >> 11) & 0x1) << 3    # addr[11]
    n[2] |= (a >>  4) & 0x7           # addr[6:4]
    n[1] |= ((a >>  7) & 0x1) << 3    # addr[7]
    n[3] |= ((a >>  3) & 0x1) << 3    # addr[3]
    n[4] |=  a        & 0x7           # addr[2:0]

    if code.compare is None:
        # data layout:
        #   n1 bits 2,1,0 -> data bits 6,5,4
        #   n0 bit 3      -> data bit 7
        #   n0 bits 2,1,0 -> data bits 2,1,0
        #   n5 bit 3      -> data bit 3
        n[1] |= (d >> 4) & 0x7
        n[0] |= ((d >> 7) & 0x1) << 3
        n[0] |=  d        & 0x7
        n[5] |= ((d >> 3) & 0x1) << 3
    else:
        c = code.compare & 0xFF
        # data: same except data bit 3 comes from n7 bit 3
        n[1] |= (d >> 4) & 0x7
        n[0] |= ((d >> 7) & 0x1) << 3
        n[0] |=  d        & 0x7
        n[7] |= ((d >> 3) & 0x1) << 3
        # compare:
        #   n7 bits 2,1,0 -> cmp bits 6,5,4
        #   n6 bit 3      -> cmp bit 7
        #   n6 bits 2,1,0 -> cmp bits 2,1,0
        #   n5 bit 3      -> cmp bit 3
        n[7] |= (c >> 4) & 0x7
        n[6] |= ((c >> 7) & 0x1) << 3
        n[6] |=  c        & 0x7
        n[5] |= ((c >> 3) & 0x1) << 3

    return _nibs_to_letters(n)


def iter_6letter(addr_range: Iterable[int], value_range: Iterable[int] = range(256)):
    value_list = list(value_range)
    for addr in addr_range:
        for val in value_list:
            yield GenieCode(addr, val, None)


def iter_8letter(addr_range: Iterable[int], value_range: Iterable[int],
                 compare_map: dict):
    """compare_map: cpu_addr -> original byte (int) or collection of bytes."""
    value_list = list(value_range)
    for addr in addr_range:
        cpu_addr = 0x8000 | (addr & 0x7FFF)
        if cpu_addr not in compare_map:
            continue
        cmp_bytes = compare_map[cpu_addr]
        if isinstance(cmp_bytes, int):
            cmp_bytes = (cmp_bytes,)
        for cmp_byte in cmp_bytes:
            for val in value_list:
                if val == cmp_byte:
                    continue
                yield GenieCode(addr, val, cmp_byte)
