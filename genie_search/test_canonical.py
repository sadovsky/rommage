from genie import decode, encode, GenieCode

cases = [
    ("GOSSIP",   0xD1DD, 0x14, None),
    ("ZEXPYGLA", 0x94A7, 0x02, 0x03),
    ("SXIOPO",   0x91D9, 0xAD, None),
]

all_ok = True
for code, addr, val, cmp in cases:
    gc = decode(code)
    ok = (gc.cpu_address == addr and gc.value == val and gc.compare == cmp)
    mark = "OK  " if ok else "FAIL"
    cmp_str = f"${gc.compare:02X}" if gc.compare is not None else "None"
    print(f"{mark} {code:9} -> cpu=${gc.cpu_address:04X} val=${gc.value:02X} cmp={cmp_str}")
    if not ok:
        cmp_exp = f"${cmp:02X}" if cmp is not None else "None"
        print(f"      expected: cpu=${addr:04X} val=${val:02X} cmp={cmp_exp}")
        all_ok = False

    s = encode(gc)
    gc2 = decode(s)
    if gc2 != gc:
        print(f"      round-trip FAIL: {code} -> {s} -> {gc2}")
        all_ok = False

print()
n_ok = 0
n_total = 0
for addr in [0, 0x1234, 0x7FFF, 0x4000, 0x2222]:
    for val in [0, 0x55, 0xAA, 0xFF, 1, 0x80]:
        for cmp in [None, 0, 0xFF, 0x42]:
            gc = GenieCode(addr, val, cmp)
            s = encode(gc)
            gc2 = decode(s)
            n_total += 1
            if gc == gc2:
                n_ok += 1

print(f"Round-trip: {n_ok}/{n_total}")
print("ALL PASS" if all_ok and n_ok == n_total else "SOME FAIL")
