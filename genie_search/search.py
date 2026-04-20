"""Search driver: enumerate candidates, evaluate in parallel, rank results.

Two-stage pipeline:
  Stage 1 (fast reject): short rollout (~60 frames), phash-only distance.
    Drops nulls and obvious crashes cheaply. Typically kills 95%+ of candidates.
  Stage 2 (deep eval): full rollout (~600 frames) on survivors, both metrics.

Parallelism: multiprocessing.Pool of workers, each with its own persistent
RolloutRunner. Each worker receives a batch of candidates and streams back
partial results to reduce memory pressure and give incremental progress.

Candidate enumeration is smart: for 8-letter codes we use the ROM's PRG
contents to constrain `compare` to bytes that actually exist at each
address, shrinking the effective 8-letter space dramatically.
"""

from __future__ import annotations
import contextlib
import io
import multiprocessing as mp
import os
import pickle
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import json

import numpy as np

with contextlib.redirect_stderr(io.StringIO()):
    from genie import GenieCode, encode
    from runner import RolloutRunner
    from scorer import (
        ScoreResult, precompute_baseline, score_frames,
        dhash_stack, hamming_stack, _quantize,
    )


def _frame_hist(frame: np.ndarray, bins: int = 64) -> np.ndarray:
    idx = _quantize(frame).ravel()
    h = np.bincount(idx, minlength=bins).astype(np.float32)
    return h / (h.sum() + 1e-12)


def _truncate_sequence(
    seq: Sequence[tuple[int, int]], max_frames: int
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    remaining = max_frames
    for action, duration in seq:
        if remaining <= 0:
            break
        use = min(duration, remaining)
        out.append((action, use))
        remaining -= use
    return out


# ------------------------------ candidate enumeration ------------------------------

def read_prg_bytes(rom_path: str) -> tuple[bytes, int]:
    """Return (PRG bytes, PRG size in bytes). NROM-assumed."""
    raw = Path(rom_path).read_bytes()
    if raw[:4] != b"NES\x1A":
        raise ValueError("Not an iNES ROM")
    prg_units = raw[4]
    chr_units = raw[5]
    trainer = bool(raw[6] & 0x04)
    off = 16 + (512 if trainer else 0)
    prg_size = prg_units * 16 * 1024
    return raw[off : off + prg_size], prg_size


def rom_byte_at(prg: bytes, cpu_addr: int) -> int:
    """For NROM, return the PRG byte mapped to CPU address cpu_addr."""
    assert 0x8000 <= cpu_addr <= 0xFFFF
    offset = cpu_addr - 0x8000
    if len(prg) == 0x4000:
        offset &= 0x3FFF  # 16KB mirror
    return prg[offset]


def enumerate_candidates(
    rom_path: str,
    addr_range: Iterable[int] | None = None,
    value_range: range | None = None,
    include_6letter: bool = True,
    include_8letter: bool = True,
    sample: int | None = None,
    seed: int = 0,
) -> list[GenieCode]:
    """Generate candidate codes over the chosen search space.

    For 8-letter codes, `compare` is pinned to the actual ROM byte at each
    address (there's no point enumerating compares the code could never match).
    """
    prg, _ = read_prg_bytes(rom_path)
    addr_range = addr_range or range(0x8000, 0x10000)
    value_range = value_range or range(256)

    out: list[GenieCode] = []
    for cpu_addr in addr_range:
        orig = rom_byte_at(prg, cpu_addr)
        code_addr = cpu_addr & 0x7FFF
        for val in value_range:
            if val == orig:
                continue  # no-op patch
            if include_6letter:
                out.append(GenieCode(code_addr, val, None))
            if include_8letter:
                out.append(GenieCode(code_addr, val, orig))

    if sample is not None and sample < len(out):
        import random
        rng = random.Random(seed)
        out = rng.sample(out, sample)

    return out


# ------------------------------ worker ------------------------------

# Process-local worker state (avoids pickling the emulator).
_WORKER_STATE: dict = {}


def _silence_worker_stderr() -> None:
    """Redirect fd 1 and fd 2 to /dev/null in a worker process.

    nes-py's C++ CPU prints 'failed to execute opcode' to std::cout (fd 1) on
    undefined opcodes. Perturbing ROM bytes during brute-force search triggers
    this on most candidates, producing hundreds of MB of spam that bottlenecks
    I/O. Workers don't emit progress or surface tracebacks to the user (the
    pool forwards exceptions via its result protocol) so both fds are safe
    to silence.
    """
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)


def _worker_init(
    rom_path: str,
    warmup_actions: Sequence[tuple[int, int]] | None,
    stage1_actions: Sequence[tuple[int, int]],
    stage2_actions: Sequence[tuple[int, int]],
    baseline_hashes: np.ndarray,
    baseline_hists: np.ndarray,
    stage1_baseline_hashes: np.ndarray,
    capture_every: int,
    boot_sequence: Sequence[tuple[int, int]] | None,
    boot_baseline_hash: int | None,
    boot_baseline_hist: np.ndarray | None,
    boot_ham_max: int,
    boot_hist_max: float,
):
    _silence_worker_stderr()
    _WORKER_STATE["runner"] = RolloutRunner(rom_path, warmup_sequence=warmup_actions)
    _WORKER_STATE["stage1_actions"] = stage1_actions
    _WORKER_STATE["stage2_actions"] = stage2_actions
    _WORKER_STATE["baseline_hashes"] = baseline_hashes
    _WORKER_STATE["baseline_hists"] = baseline_hists
    _WORKER_STATE["stage1_baseline_hashes"] = stage1_baseline_hashes
    _WORKER_STATE["capture_every"] = capture_every
    _WORKER_STATE["boot_sequence"] = boot_sequence
    _WORKER_STATE["boot_baseline_hash"] = boot_baseline_hash
    _WORKER_STATE["boot_baseline_hist"] = boot_baseline_hist
    _WORKER_STATE["boot_ham_max"] = boot_ham_max
    _WORKER_STATE["boot_hist_max"] = boot_hist_max


@dataclass
class CandidateResult:
    code_str: str               # letter form
    cpu_addr: int
    value: int
    compare: int | None
    # Stage-1 phash-only stats
    s1_hamming_max: int
    s1_hamming_mean: float
    passed_stage1: bool
    # Stage-2 full stats (only present if passed_stage1)
    s2: dict | None = None
    # A representative patched frame path (thumbnail) if stage-2 ran
    thumbnail_path: str | None = None
    # None = not checked; False = failed boot-safety, stage1/2 skipped
    boot_safe: bool | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _eval_one(candidate: GenieCode) -> CandidateResult:
    st = _WORKER_STATE
    runner: RolloutRunner = st["runner"]
    s1_actions = st["stage1_actions"]
    s2_actions = st["stage2_actions"]
    capture_every = st["capture_every"]
    s1_base_hashes = st["stage1_baseline_hashes"]

    # Stage 0: boot-safety precheck (optional)
    boot_safe: bool | None = None
    boot_seq = st.get("boot_sequence")
    if boot_seq:
        from scorer import _dhash_frame, hamming
        frame = runner.boot_check(boot_seq, candidate)
        if frame is None:
            boot_safe = False
        else:
            cand_hash = _dhash_frame(frame)
            ham = hamming(cand_hash, st["boot_baseline_hash"])
            cand_hist = _frame_hist(frame)
            hist_d = float(np.abs(cand_hist - st["boot_baseline_hist"]).sum())
            boot_safe = ham <= st["boot_ham_max"] and hist_d <= st["boot_hist_max"]
        if not boot_safe:
            return CandidateResult(
                code_str=encode(candidate),
                cpu_addr=candidate.cpu_address,
                value=candidate.value,
                compare=candidate.compare,
                s1_hamming_max=0,
                s1_hamming_mean=0.0,
                passed_stage1=False,
                boot_safe=False,
            )

    # Stage 1
    s1_frames = runner.run(s1_actions, cheats=[candidate], capture_every=capture_every)
    s1_cand_hashes = dhash_stack(s1_frames)
    n = min(len(s1_base_hashes), len(s1_cand_hashes))
    if n == 0:
        return CandidateResult(
            code_str=encode(candidate),
            cpu_addr=candidate.cpu_address,
            value=candidate.value,
            compare=candidate.compare,
            s1_hamming_max=0,
            s1_hamming_mean=0.0,
            passed_stage1=False,
            boot_safe=boot_safe,
        )
    ham = hamming_stack(s1_base_hashes[:n], s1_cand_hashes[:n])
    s1_max, s1_mean = int(ham.max()), float(ham.mean())

    # Stage-1 filter: reject nulls AND obvious crashes
    from scorer import NULL_HAMMING_MAX, CRASH_HAMMING_MIN
    passed = (s1_max > NULL_HAMMING_MAX) and (s1_mean < CRASH_HAMMING_MIN)

    result = CandidateResult(
        code_str=encode(candidate),
        cpu_addr=candidate.cpu_address,
        value=candidate.value,
        compare=candidate.compare,
        s1_hamming_max=s1_max,
        s1_hamming_mean=s1_mean,
        passed_stage1=passed,
        boot_safe=boot_safe,
    )
    if not passed:
        return result

    # Stage 2
    s2_frames = runner.run(s2_actions, cheats=[candidate], capture_every=capture_every)
    s2_score = score_frames(
        st["baseline_hashes"], st["baseline_hists"], s2_frames
    )
    result.s2 = s2_score.as_dict()
    # Save middle frame as thumbnail (caller will move it to output dir)
    if len(s2_frames) > 0:
        from PIL import Image
        mid = s2_frames[len(s2_frames) // 2]
        tmp_dir = os.environ.get("GENIE_THUMB_DIR", "/tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, f"thumb_{result.code_str}.png")
        Image.fromarray(mid).save(path, optimize=True)
        result.thumbnail_path = path

    return result


# ------------------------------ live-address tracing ------------------------------

# A separate worker-state slot so tracing pools don't collide with search pools
# if someone ever runs both in-process. In practice we only have one pool at a
# time, but keeping the keys namespaced keeps the code readable.

def _probe_worker_init(
    rom_path: str,
    warmup_actions: Sequence[tuple[int, int]] | None,
    probe_actions: Sequence[tuple[int, int]],
    probe_baseline_hashes: np.ndarray,
    capture_every: int,
    prg_bytes: bytes,
):
    _silence_worker_stderr()
    _WORKER_STATE["runner"] = RolloutRunner(rom_path, warmup_sequence=warmup_actions)
    _WORKER_STATE["probe_actions"] = probe_actions
    _WORKER_STATE["probe_base_hashes"] = probe_baseline_hashes
    _WORKER_STATE["capture_every"] = capture_every
    _WORKER_STATE["prg_bytes"] = prg_bytes


def _probe_one(cpu_addr: int) -> tuple[int, bool]:
    st = _WORKER_STATE
    prg: bytes = st["prg_bytes"]
    orig = rom_byte_at(prg, cpu_addr)
    probe = (orig ^ 0xFF) & 0xFF
    if probe == orig:
        probe = (orig ^ 0x55) & 0xFF
    gc = GenieCode(cpu_addr & 0x7FFF, probe, None)
    try:
        frames = st["runner"].run(
            st["probe_actions"], cheats=[gc], capture_every=st["capture_every"]
        )
    except Exception:
        # Emulator fault on this byte = address is live (byte change broke exec).
        return (cpu_addr, True)
    cand_hashes = dhash_stack(frames)
    base = st["probe_base_hashes"]
    n = min(len(base), len(cand_hashes))
    if n == 0:
        return (cpu_addr, False)
    ham = hamming_stack(base[:n], cand_hashes[:n])
    return (cpu_addr, int(ham.max()) > 0)


def _worker_loop(
    setup_fn: Callable,
    setup_args: tuple,
    work_fn: Callable,
    task_q,
    result_q,
) -> None:
    """Worker entry point for `_run_procs_with_watchdog`.

    Runs the setup function once (shared with Pool-era initializers), then
    pulls tasks from `task_q` and pushes results to `result_q` until it sees
    a `None` sentinel.
    """
    _silence_worker_stderr()
    setup_fn(*setup_args)
    while True:
        task = task_q.get()
        if task is None:
            return
        try:
            result = work_fn(task)
        except Exception:
            # Task errored inside the emulator; report a marker so the main
            # loop accounts for it as "consumed" and can stop waiting.
            result_q.put(("__error__", task))
            continue
        result_q.put(result)


def _run_procs_with_watchdog(
    n_workers: int,
    setup_fn: Callable,
    setup_args: tuple,
    work_fn: Callable,
    tasks: Iterable,
    on_result: Callable,
    stagnation_timeout: float = 30.0,
    label: str = "pool",
) -> tuple[int, bool]:
    """Spawn N workers, run `work_fn` over `tasks` with a SIGKILL watchdog.

    This replaces `multiprocessing.Pool` for our use case because Pool:
      (a) silently restarts SIGKILL'd workers via its worker_handler thread
          (so killing once in a watchdog does nothing),
      (b) on `terminate()` and `close()+join()` deadlocks when workers died
          with un-ACKed tasks still in the task queue.

    Instead we own the lifecycle: raw `mp.Process` workers backed by a pair
    of `mp.Queue`s, and on stagnation we SIGKILL every worker directly and
    bail. Queues are drained with `cancel_join_thread()` so the main process
    isn't stuck flushing pickled tasks nobody's going to read.

    Calls `on_result(item)` for each non-error result. Returns
    `(n_consumed, timed_out)`.
    """
    import queue as queue_module

    ctx = mp.get_context("spawn")
    task_q = ctx.Queue()
    result_q = ctx.Queue()

    task_list = list(tasks)
    n_expected = len(task_list)

    workers: list = []
    for _ in range(n_workers):
        p = ctx.Process(
            target=_worker_loop,
            args=(setup_fn, setup_args, work_fn, task_q, result_q),
            daemon=True,
        )
        p.start()
        workers.append(p)

    for t in task_list:
        task_q.put(t)
    # One sentinel per worker so clean-exit path returns naturally.
    for _ in workers:
        task_q.put(None)

    n_got = 0
    timed_out = False
    last_progress = time.perf_counter()

    while n_got < n_expected:
        try:
            item = result_q.get(timeout=2)
        except queue_module.Empty:
            stale = time.perf_counter() - last_progress
            if stale > stagnation_timeout:
                timed_out = True
                print(f"  watchdog ({label}): {stale:.0f}s of no progress — "
                      f"SIGKILL workers", flush=True)
                for w in workers:
                    try:
                        w.kill()
                    except Exception:
                        pass
                break
            # All workers dead with results pending = we lost some to crashes
            # the error marker didn't cover (e.g. segfault). Bail cleanly.
            if all(not w.is_alive() for w in workers):
                print(f"  {label}: all workers exited; collected "
                      f"{n_got}/{n_expected} results", flush=True)
                break
            continue
        last_progress = time.perf_counter()
        n_got += 1
        if isinstance(item, tuple) and len(item) == 2 and item[0] == "__error__":
            continue
        on_result(item)

    # Shutdown: let clean exits finish, SIGKILL the rest.
    deadline = time.perf_counter() + 3.0
    for w in workers:
        remaining = max(0.0, deadline - time.perf_counter())
        try:
            w.join(timeout=remaining)
        except Exception:
            pass
    for w in workers:
        if w.is_alive():
            try:
                w.kill()
            except Exception:
                pass
    for w in workers:
        try:
            w.join(timeout=1.0)
        except Exception:
            pass

    # Avoid blocking on the queue feeder thread trying to flush unread tasks.
    try:
        task_q.cancel_join_thread()
        task_q.close()
    except Exception:
        pass
    try:
        result_q.cancel_join_thread()
        result_q.close()
    except Exception:
        pass

    return n_got, timed_out


def trace_live_addresses(
    cfg: "SearchConfig",
    progress_callback: Callable[[int, int, dict], None] | None = None,
) -> set[int]:
    """Identify PRG addresses whose value actually affects the rollout.

    For each address we force the byte to a perturbed value via a 6-letter
    cheat (no compare) and run a short rollout; if any captured frame's
    dHash differs from the no-cheat baseline, the address is 'live'.

    This is parallelized across workers the same way as the main search.
    """
    addr_range = list(cfg.addr_range or range(0x8000, 0x10000))
    prg, _ = read_prg_bytes(cfg.rom_path)
    probe_actions = _truncate_sequence(cfg.stage1_actions, cfg.trace_probe_frames)

    # Baseline for probe length (main process)
    with RolloutRunner(cfg.rom_path, warmup_sequence=cfg.warmup_actions) as r:
        base_frames = r.run(probe_actions, cheats=[], capture_every=cfg.capture_every)
    probe_hashes = dhash_stack(base_frames)

    n_workers = cfg.num_workers or os.cpu_count() or 1
    init_args = (
        cfg.rom_path,
        list(cfg.warmup_actions) if cfg.warmup_actions else None,
        list(probe_actions),
        probe_hashes,
        cfg.capture_every,
        prg,
    )

    print(f"tracing {len(addr_range):,} PRG addresses "
          f"({cfg.trace_probe_frames} frames/probe)...")
    live: set[int] = set()
    seen: set[int] = set()
    t0 = time.perf_counter()
    progress_every = max(200, min(2000, len(addr_range) // 40))
    STAGNATION_TIMEOUT = 30

    done_counter = [0]

    def on_probe(item):
        addr, is_live = item
        seen.add(addr)
        if is_live:
            live.add(addr)
        done_counter[0] += 1
        done = done_counter[0]
        if progress_callback and done % progress_every == 0:
            elapsed = time.perf_counter() - t0
            rate = done / elapsed if elapsed > 0 else 0.0
            eta = (len(addr_range) - done) / rate if rate > 0 else 0.0
            progress_callback(done, len(addr_range), {
                "rate": rate, "eta_s": eta, "elapsed": elapsed,
                "n_live": len(live),
            })

    _, timed_out = _run_procs_with_watchdog(
        n_workers=n_workers,
        setup_fn=_probe_worker_init,
        setup_args=init_args,
        work_fn=_probe_one,
        tasks=addr_range,
        on_result=on_probe,
        stagnation_timeout=STAGNATION_TIMEOUT, label="trace",
    )
    if timed_out:
        # Unfinished addresses are conservatively marked live so we never miss
        # a real hit — the downstream search will re-probe them.
        missing = [a for a in addr_range if a not in seen]
        print(f"  trace: stalled {STAGNATION_TIMEOUT}s — assuming "
              f"{len(missing):,} unfinished addresses are live")
        live.update(missing)

    dt = time.perf_counter() - t0
    pct = 100 * len(live) / max(1, len(addr_range))
    print(f"traced {len(addr_range):,} addresses in {dt:.1f}s; "
          f"{len(live):,} live ({pct:.1f}%)")
    return live


# ------------------------------ top-level driver ------------------------------

@dataclass
class SearchConfig:
    rom_path: str
    stage1_actions: Sequence[tuple[int, int]]
    stage2_actions: Sequence[tuple[int, int]]
    warmup_actions: Sequence[tuple[int, int]] | None = None
    addr_range: range | None = None
    value_range: range | None = None
    include_6letter: bool = True
    include_8letter: bool = True
    capture_every: int = 30
    sample: int | None = None
    num_workers: int = 0          # 0 = os.cpu_count()
    thumb_dir: str = "./thumbs"
    seed: int = 0
    require_boot_safe: bool = False
    boot_check_frames: int = 60
    boot_ham_max: int = 8
    boot_hist_max: float = 0.12
    # Live-address tracing (pre-stage): skip addresses the rollout never reads
    trace_live_addrs: bool = False
    trace_probe_frames: int = 60
    trace_cache_path: str | None = None
    # Resume / checkpointing
    partial_path: str | None = None   # where to write results.partial.pkl
    resume: bool = False              # if True, skip candidates already in partial
    save_every: int = 200             # flush partial every N new results


def run_search(
    cfg: SearchConfig,
    progress_callback: Callable[[int, int, dict], None] | None = None,
) -> list[CandidateResult]:
    """Execute the full search. Returns all candidate results.

    Progress callback signature: (done, total, stats) where stats is a dict
    with keys: rate, eta_s, elapsed, n_passed_s1, n_boot_unsafe.
    """
    os.makedirs(cfg.thumb_dir, exist_ok=True)
    os.environ["GENIE_THUMB_DIR"] = os.path.abspath(cfg.thumb_dir)

    # Compute baselines once (main process)
    with RolloutRunner(cfg.rom_path, warmup_sequence=cfg.warmup_actions) as r:
        s1_base = r.run(cfg.stage1_actions, cheats=[], capture_every=cfg.capture_every)
        s2_base = r.run(cfg.stage2_actions, cheats=[], capture_every=cfg.capture_every)
    s1_hashes = dhash_stack(s1_base)
    s2_hashes, s2_hists = precompute_baseline(s2_base)

    # Optional: baseline for the stage-0 boot-safety check
    boot_seq: Sequence[tuple[int, int]] | None = None
    boot_baseline_hash = None
    boot_baseline_hist = None
    if cfg.require_boot_safe:
        boot_seq = _truncate_sequence(
            cfg.warmup_actions or cfg.stage1_actions, cfg.boot_check_frames
        )
        from scorer import _dhash_frame
        with RolloutRunner(cfg.rom_path, warmup_sequence=None) as r:
            for action, duration in boot_seq:
                for _ in range(duration):
                    r.env.step(action)
            boot_frame = r.env.screen.copy()
        boot_baseline_hash = int(_dhash_frame(boot_frame))
        boot_baseline_hist = _frame_hist(boot_frame)
        print(f"boot-safety check enabled: {cfg.boot_check_frames} frames, "
              f"ham≤{cfg.boot_ham_max}, hist≤{cfg.boot_hist_max}")

    # Optional pre-stage: live-address tracing. Either load from cache or run.
    effective_addr_range = cfg.addr_range
    if cfg.trace_live_addrs:
        live: set[int] | None = None
        if cfg.trace_cache_path and os.path.exists(cfg.trace_cache_path):
            with open(cfg.trace_cache_path) as f:
                live = set(json.load(f))
            print(f"loaded {len(live):,} live addresses from cache: "
                  f"{cfg.trace_cache_path}")
        if live is None:
            def trace_progress(done, total_addrs, stats):
                pct = 100 * done / total_addrs
                print(f"  trace: {done:,}/{total_addrs:,} ({pct:.1f}%) "
                      f"@ {stats['rate']:.1f}/s  eta {stats['eta_s']:.0f}s  "
                      f"live={stats['n_live']}", flush=True)
            live = trace_live_addresses(cfg, progress_callback=trace_progress)
            if cfg.trace_cache_path:
                os.makedirs(os.path.dirname(cfg.trace_cache_path) or ".", exist_ok=True)
                with open(cfg.trace_cache_path, "w") as f:
                    json.dump(sorted(live), f)
                print(f"cached live addresses: {cfg.trace_cache_path}")
        # Intersect with any user-supplied addr_range
        base_range = cfg.addr_range or range(0x8000, 0x10000)
        effective_addr_range = [a for a in base_range if a in live]
        print(f"effective search addresses: {len(effective_addr_range):,} "
              f"(of {len(list(base_range)):,})")

    # Enumerate candidates
    candidates = enumerate_candidates(
        cfg.rom_path,
        addr_range=effective_addr_range,
        value_range=cfg.value_range,
        include_6letter=cfg.include_6letter,
        include_8letter=cfg.include_8letter,
        sample=cfg.sample,
        seed=cfg.seed,
    )
    total = len(candidates)
    print(f"Enumerated {total:,} candidates")

    # Resume: load prior partial, skip already-evaluated candidates
    prior_results: list[CandidateResult] = []
    already_seen: set[str] = set()
    if cfg.resume and cfg.partial_path and os.path.exists(cfg.partial_path):
        prior_results = load_partial(cfg.partial_path)
        already_seen = {r.code_str for r in prior_results}
        before = len(candidates)
        candidates = [c for c in candidates if encode(c) not in already_seen]
        print(f"resuming: loaded {len(prior_results):,} prior results, "
              f"{before - len(candidates):,} candidates skipped, "
              f"{len(candidates):,} remaining")

    n_workers = cfg.num_workers or os.cpu_count() or 1
    init_args = (
        cfg.rom_path,
        list(cfg.warmup_actions) if cfg.warmup_actions else None,
        list(cfg.stage1_actions),
        list(cfg.stage2_actions),
        s2_hashes,
        s2_hists,
        s1_hashes,
        cfg.capture_every,
        list(boot_seq) if boot_seq else None,
        boot_baseline_hash,
        boot_baseline_hist,
        cfg.boot_ham_max,
        cfg.boot_hist_max,
    )

    results: list[CandidateResult] = list(prior_results)
    n_prior = len(prior_results)
    t0 = time.perf_counter()

    # Progress fires at roughly this cadence over the remaining work.
    progress_every = max(50, min(1000, max(1, len(candidates) // 40)))

    def emit_progress(new_done: int):
        done_abs = n_prior + new_done
        elapsed = time.perf_counter() - t0
        rate = new_done / elapsed if elapsed > 0 else 0.0
        remaining = len(candidates) - new_done
        eta_s = remaining / rate if rate > 0 else 0.0
        n_passed = sum(1 for r in results if r.passed_stage1)
        n_boot_unsafe = sum(1 for r in results if r.boot_safe is False)
        if progress_callback:
            progress_callback(done_abs, total, {
                "rate": rate,
                "eta_s": eta_s,
                "elapsed": elapsed,
                "n_passed_s1": n_passed,
                "n_boot_unsafe": n_boot_unsafe,
            })

    def maybe_save(new_done: int):
        if cfg.partial_path and cfg.save_every > 0 and new_done % cfg.save_every == 0:
            _atomic_save(results, cfg.partial_path)

    if not candidates:
        print("nothing to do (all candidates already evaluated)")
    elif n_workers == 1:
        # Single-process path -- easier debugging
        _worker_init(*init_args)
        for i, c in enumerate(candidates):
            results.append(_eval_one(c))
            new_done = i + 1
            if new_done % progress_every == 0:
                emit_progress(new_done)
            maybe_save(new_done)
    else:
        done_counter = [0]

        def on_search_result(res):
            results.append(res)
            done_counter[0] += 1
            new_done = done_counter[0]
            if new_done % progress_every == 0:
                emit_progress(new_done)
            maybe_save(new_done)

        _, timed_out = _run_procs_with_watchdog(
            n_workers=n_workers,
            setup_fn=_worker_init,
            setup_args=init_args,
            work_fn=_eval_one,
            tasks=candidates,
            on_result=on_search_result,
            stagnation_timeout=60.0, label="search",
        )
        if timed_out:
            n_lost = len(candidates) - done_counter[0]
            print(f"  search: watchdog killed pool — {n_lost:,} "
                  f"candidates unfinished; re-run with --resume to "
                  f"pick them up")

    # Final partial save so a re-run with --resume sees 100% done
    if cfg.partial_path:
        _atomic_save(results, cfg.partial_path)

    dt = time.perf_counter() - t0
    n_passed = sum(1 for r in results if r.passed_stage1)
    new_count = len(candidates)
    print(f"Searched {new_count:,} candidates in {dt:.1f}s "
          f"({(new_count/dt) if dt > 0 else 0:.1f}/s); "
          f"{n_passed} passed stage-1 filter "
          f"({len(results):,} total including resumed)")
    return results


def rank_interesting(results: Sequence[CandidateResult]) -> list[CandidateResult]:
    """Rank stage-2 survivors by a combined interestingness score.

    We want middle-band distances -- not null, not crashed. Score:
        (s2.hist_mean) * 1.0 + (s2.hamming_mean / 64) * 0.5
    Heavily penalize things classified as 'likely_crash'.
    """
    survivors = [r for r in results if r.passed_stage1 and r.s2 is not None]

    def key(r: CandidateResult) -> float:
        s2 = r.s2 or {}
        score = s2.get("hist_mean", 0.0) + 0.5 * (s2.get("hamming_mean", 0.0) / 64.0)
        if s2.get("bucket") == "likely_crash":
            score *= 0.1
        elif s2.get("bucket") == "null":
            score *= 0.1
        return -score

    return sorted(survivors, key=key)


def save_results(results: Sequence[CandidateResult], path: str) -> None:
    """Pickle the full result list for later inspection."""
    with open(path, "wb") as f:
        pickle.dump([r.as_dict() for r in results], f)


def _atomic_save(results: Sequence[CandidateResult], path: str) -> None:
    """Write pickle via temp+rename so a crash can't corrupt the file."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump([r.as_dict() for r in results], f)
    os.replace(tmp, path)


def load_partial(path: str) -> list[CandidateResult]:
    """Load a partial-results pickle back into CandidateResult dataclasses."""
    with open(path, "rb") as f:
        loaded = pickle.load(f)
    return [CandidateResult(**d) for d in loaded]
