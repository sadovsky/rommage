"""Brute-force Game Genie code finder for NES.

Example:
  python rommage.py search smb1.nes \\
      --input-sequence press_start \\
      --addr-range 0x8000-0xFFFF \\
      --stage1-frames 60 --stage2-frames 600 \\
      --sample 5000 --workers 8 \\
      --out results/smb1

This runs a behavioral search: for each candidate code, the ROM is executed
with the cheat applied and the resulting frame stack is compared to a
baseline. Codes that meaningfully change visible output are reported.
"""

from __future__ import annotations
import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

with contextlib.redirect_stderr(io.StringIO()):
    import runner
    from runner import (
        idle, press_start, walk_right, random_mash,
        NOOP, A, B, SELECT, START, UP, DOWN, LEFT, RIGHT,
    )
    from search import SearchConfig, run_search, rank_interesting, save_results, format_eta, load_partial
    from report import write_report
    from scorer import precompute_baseline


INPUT_SEQUENCES = {
    "idle": lambda n: idle(n),
    "press_start": lambda n: press_start(pre=60, hold=10, post=max(n - 70, 60)),
    "walk_right": lambda n: walk_right(pre=60, start_hold=10, mid=30, walk=max(n - 100, 60)),
    "walk_right_ingame": lambda n: [(RIGHT, n)],
    "random": lambda n: random_mash(n, seed=42),
}


def parse_range(s: str) -> range:
    """Parse '0x8000-0xFFFF' or '32768-65535' into an inclusive-exclusive
    range over CPU addresses."""
    if "-" not in s:
        raise argparse.ArgumentTypeError(f"expected LOW-HIGH, got {s!r}")
    lo_s, hi_s = s.split("-", 1)
    lo = int(lo_s, 0)
    hi = int(hi_s, 0)
    return range(lo, hi + 1)


def parse_int_hex(s: str) -> int:
    return int(s, 0)


def cmd_decode(args):
    from genie import decode
    gc = decode(args.code)
    cmp_str = f" compare=${gc.compare:02X}" if gc.compare is not None else ""
    print(f"{args.code}: CPU=${gc.cpu_address:04X} value=${gc.value:02X}{cmp_str}")


def cmd_encode(args):
    from genie import GenieCode
    addr = args.addr & 0x7FFF
    gc = GenieCode(addr, args.value & 0xFF,
                   None if args.compare is None else args.compare & 0xFF)
    print(str(gc))


def cmd_search(args):
    if not os.path.exists(args.rom):
        print(f"ROM not found: {args.rom}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs = out_dir / "thumbs"

    action_fn = INPUT_SEQUENCES[args.input_sequence]
    stage1 = action_fn(args.stage1_frames)
    stage2 = action_fn(args.stage2_frames)
    warmup_seq = args.warmup_input_sequence or args.input_sequence
    warmup_fn = INPUT_SEQUENCES[warmup_seq]
    warmup = warmup_fn(args.warmup_frames) if args.warmup_frames > 0 else None

    cfg = SearchConfig(
        rom_path=args.rom,
        stage1_actions=stage1,
        stage2_actions=stage2,
        warmup_actions=warmup,
        addr_range=args.addr_range,
        value_range=range(0, 256, max(1, args.value_stride)),
        include_6letter=not args.no_6letter,
        include_8letter=not args.no_8letter,
        capture_every=args.capture_every,
        sample=args.sample,
        num_workers=args.workers,
        thumb_dir=str(thumbs),
        seed=args.seed,
        require_boot_safe=args.require_boot_safe,
        boot_check_frames=args.boot_check_frames,
        partial_path=str(out_dir / "results.partial.pkl"),
        resume=args.resume,
        save_every=args.save_every,
        trace_live_addrs=args.trace_live_addrs,
        trace_probe_frames=args.trace_probe_frames,
        trace_cache_path=str(out_dir / "live_addrs.json"),
    )

    def progress(done: int, total: int, stats: dict):
        pct = 100 * done / total
        rate = stats.get("rate", 0.0)
        eta_str = format_eta(stats.get("eta_s", 0.0))
        n_s1 = stats.get("n_passed_s1", 0)
        n_bad = stats.get("n_boot_unsafe", 0)
        msg = (f"  progress: {done:,}/{total:,} ({pct:.1f}%) "
               f"@ {rate:.1f}/s  eta {eta_str}  "
               f"stage1_pass={n_s1}")
        if cfg.require_boot_safe:
            msg += f"  boot_unsafe={n_bad}"
        print(msg, flush=True)

    print(f"searching {args.rom}...", flush=True)
    results = run_search(cfg, progress_callback=progress)

    # Save pickle
    save_results(results, str(out_dir / "results.pkl"))

    top = rank_interesting(results)
    print(f"ranked {len(top)} interesting candidates")
    for r in top[:20]:
        s2 = r.s2 or {}
        print(f"  {r.code_str:9}  ${r.cpu_addr:04X}:={r.value:02X}"
              f"  bucket={s2.get('bucket','?'):<13}"
              f"  hist={s2.get('hist_mean',0):.3f}"
              f"  ham={s2.get('hamming_mean',0):.1f}")

    # Render baseline frame for the report
    from runner import RolloutRunner
    with RolloutRunner(args.rom, warmup_sequence=warmup) as r:
        base_frames = r.run(stage2, cheats=[], capture_every=args.capture_every)
    baseline_mid = base_frames[len(base_frames) // 2] if len(base_frames) else None
    if baseline_mid is None:
        import numpy as np
        baseline_mid = np.zeros((240, 256, 3), dtype="uint8")

    n_passed = sum(1 for r in results if r.passed_stage1)
    index = write_report(
        out_dir=str(out_dir),
        rom_path=args.rom,
        baseline_frame=baseline_mid,
        top=top,
        total_evaluated=len(results),
        n_passed_stage1=n_passed,
        top_k=args.top_k,
    )
    print(f"wrote report: {index}")


def cmd_report(args):
    """Regenerate index.html from an existing results pickle.

    Picks results.pkl if present, else results.partial.pkl. Reuses the
    existing thumbs/baseline.png if present; otherwise --rom is required to
    re-render it.
    """
    out_dir = Path(args.out)
    final = out_dir / "results.pkl"
    partial = out_dir / "results.partial.pkl"
    if final.exists():
        src = final
    elif partial.exists():
        src = partial
        print(f"using partial results from {partial.name} (run in progress)")
    else:
        print(f"no results.pkl or results.partial.pkl in {out_dir}", file=sys.stderr)
        sys.exit(1)

    results = load_partial(str(src))
    print(f"loaded {len(results):,} results from {src.name}")

    top = rank_interesting(results)
    print(f"ranked {len(top)} interesting candidates")

    baseline_mid = None
    baseline_png = out_dir / "thumbs" / "baseline.png"
    if args.rom:
        action_fn = INPUT_SEQUENCES[args.input_sequence]
        stage2 = action_fn(args.stage2_frames)
        warmup_seq = args.warmup_input_sequence or args.input_sequence
        warmup = INPUT_SEQUENCES[warmup_seq](args.warmup_frames) if args.warmup_frames > 0 else None
        print("rendering fresh baseline frame...", flush=True)
        from runner import RolloutRunner
        with RolloutRunner(args.rom, warmup_sequence=warmup) as r:
            base_frames = r.run(stage2, cheats=[], capture_every=args.capture_every)
        if base_frames:
            baseline_mid = base_frames[len(base_frames) // 2]
    elif not baseline_png.exists():
        print("(no --rom given and no existing thumbs/baseline.png; "
              "baseline will be a black placeholder)", flush=True)

    n_passed = sum(1 for r in results if r.passed_stage1)
    index = write_report(
        out_dir=str(out_dir),
        rom_path=args.rom or "(unknown)",
        baseline_frame=baseline_mid,
        top=top,
        total_evaluated=len(results),
        n_passed_stage1=n_passed,
        top_k=args.top_k,
    )
    print(f"wrote report: {index}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rommage.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    # decode
    sp = sub.add_parser("decode", help="Decode a Game Genie code")
    sp.add_argument("code")
    sp.set_defaults(func=cmd_decode)

    # encode
    sp = sub.add_parser("encode", help="Encode (addr, value[, compare]) to a code")
    sp.add_argument("--addr", type=parse_int_hex, required=True,
                    help="CPU address, e.g. 0x8010")
    sp.add_argument("--value", type=parse_int_hex, required=True,
                    help="replacement byte, e.g. 0x99")
    sp.add_argument("--compare", type=parse_int_hex, default=None,
                    help="optional compare byte for 8-letter code")
    sp.set_defaults(func=cmd_encode)

    # search
    sp = sub.add_parser("search", help="Brute-force search for interesting codes")
    sp.add_argument("rom")
    sp.add_argument("--out", default="./genie_results",
                    help="output directory")
    sp.add_argument("--input-sequence", default="press_start",
                    choices=list(INPUT_SEQUENCES.keys()))
    sp.add_argument("--warmup-frames", type=int, default=0,
                    help="frames to run (no cheat, no capture) before the "
                         "initial state snapshot. Speeds up per-candidate "
                         "rollouts if the game has a long intro.")
    sp.add_argument("--warmup-input-sequence", default=None,
                    choices=list(INPUT_SEQUENCES.keys()),
                    help="input sequence for warmup. Defaults to matching "
                         "--input-sequence. Use a different sequence when the "
                         "rollout should assume the game is already in-play "
                         "(e.g. warmup=walk_right, rollout=walk_right_ingame).")
    sp.add_argument("--stage1-frames", type=int, default=60,
                    help="frames for the fast reject pass")
    sp.add_argument("--stage2-frames", type=int, default=300,
                    help="frames for the deep-eval pass")
    sp.add_argument("--capture-every", type=int, default=15)
    sp.add_argument("--addr-range", type=parse_range, default=parse_range("0x8000-0xFFFF"))
    sp.add_argument("--value-stride", type=int, default=1,
                    help="stride over replacement values (use 16 or 32 for "
                         "coarse initial sweeps)")
    sp.add_argument("--no-6letter", action="store_true")
    sp.add_argument("--no-8letter", action="store_true")
    sp.add_argument("--sample", type=int, default=None,
                    help="random subsample of candidates")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--workers", type=int, default=0,
                    help="parallel processes (0 = os.cpu_count())")
    sp.add_argument("--top-k", type=int, default=64,
                    help="cards to include in the HTML report")
    sp.add_argument("--require-boot-safe", action="store_true",
                    help="Reject candidates that break boot. Adds a stage-0 "
                         "check per candidate: reset + apply cheat + step "
                         "--boot-check-frames frames, require the final frame "
                         "to match the no-cheat baseline.")
    sp.add_argument("--boot-check-frames", type=int, default=60,
                    help="frames to step for the stage-0 boot check")
    sp.add_argument("--trace-live-addrs", action="store_true",
                    help="Pre-stage: for each PRG address, run one short rollout "
                         "with a perturbed byte to detect whether the address is "
                         "ever read during play. Candidate enumeration is then "
                         "restricted to live addresses. Cached to "
                         "<out>/live_addrs.json; delete to re-run.")
    sp.add_argument("--trace-probe-frames", type=int, default=60,
                    help="frames to run per address during --trace-live-addrs")
    sp.add_argument("--resume", action="store_true",
                    help="If <out>/results.partial.pkl exists, skip candidates "
                         "already evaluated and continue where it left off.")
    sp.add_argument("--save-every", type=int, default=200,
                    help="Flush partial results to disk every N new candidates "
                         "(0 disables incremental saves).")
    sp.set_defaults(func=cmd_search)

    # report
    sp = sub.add_parser("report",
                        help="(Re)generate index.html from an existing results pickle "
                             "(works on partial runs)")
    sp.add_argument("--out", required=True,
                    help="results directory (the one passed to `search --out`)")
    sp.add_argument("--rom", default=None,
                    help="ROM path — required only if thumbs/baseline.png doesn't exist")
    sp.add_argument("--input-sequence", default="press_start",
                    choices=list(INPUT_SEQUENCES.keys()))
    sp.add_argument("--warmup-input-sequence", default=None,
                    choices=list(INPUT_SEQUENCES.keys()))
    sp.add_argument("--warmup-frames", type=int, default=0)
    sp.add_argument("--stage2-frames", type=int, default=300)
    sp.add_argument("--capture-every", type=int, default=15)
    sp.add_argument("--top-k", type=int, default=64)
    sp.set_defaults(func=cmd_report)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
