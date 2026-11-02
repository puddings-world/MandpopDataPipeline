"""Keep long pipeline jobs from making the PC sluggish, and let them pause.

Low impact (applied once at pipeline start, inherited by child processes
such as the separator worker, Demucs and ffmpeg):

  * process priority below normal — the desktop and other apps win any
    contention for CPU;
  * math-library thread pools capped (OMP / MKL / OpenBLAS / NumExpr /
    torch) to MANDPOP_THREADS, default a quarter of the logical cores.
    Earlier batch runs used every core and made the machine stutter.

Set MANDPOP_PRIORITY=normal to opt out, MANDPOP_THREADS=N to change the cap.

Pause / resume: `pipeline.py queue pause` drops a flag file in the dataset;
batch loops call `wait_if_paused()` between songs, so the GPU is free once
the song in progress finishes (≈1–2 min). `pipeline.py queue resume`
removes the flag and the loops carry on where they stopped.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_THREAD_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "NUMEXPR_MAX_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")


def thread_cap() -> int:
    env = os.environ.get("MANDPOP_THREADS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return max(2, (os.cpu_count() or 4) // 4)


def apply_low_impact() -> None:
    """Lower priority and cap thread pools for this process and its children.

    Must run before numpy / torch are imported to take full effect.
    """
    if os.environ.get("MANDPOP_PRIORITY", "").lower() == "normal":
        return
    n = str(thread_cap())
    for var in _THREAD_VARS:
        os.environ.setdefault(var, n)
    try:
        import psutil
        p = psutil.Process()
        if sys.platform == "win32":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(10)
    except Exception:            # psutil missing / not permitted: thread cap still applies
        pass


def cap_torch_threads() -> None:
    """torch ignores OMP_NUM_THREADS once initialised; call after importing it."""
    try:
        import torch
        torch.set_num_threads(thread_cap())
    except Exception:
        pass


# ─── pause / resume ─────────────────────────────────────────────────────

def pause_flag(dataset: Path) -> Path:
    return Path(dataset) / ".queue_paused"


def set_paused(dataset: Path, paused: bool) -> None:
    flag = pause_flag(dataset)
    if paused:
        flag.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    else:
        flag.unlink(missing_ok=True)


def is_paused(dataset: Path) -> bool:
    return pause_flag(dataset).exists()


def wait_if_paused(dataset: Path, poll_sec: float = 10.0, sleep=time.sleep) -> bool:
    """Block while the queue is paused; True if it had to wait.

    MANDPOP_IGNORE_PAUSE=1 lets one job run while the rest are held — used
    to slip a short ingest in between batch songs without the two writing
    raw_dataset.jsonl at the same time.
    """
    if os.environ.get("MANDPOP_IGNORE_PAUSE") == "1" or not is_paused(dataset):
        return False
    print(f"  ⏸ queue paused ({pause_flag(dataset)}) — `pipeline.py queue resume` to continue",
          flush=True)
    while is_paused(dataset):
        sleep(poll_sec)
    print("  ▶ queue resumed", flush=True)
    return True
