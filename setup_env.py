"""
MandpopDataPipeline — One-line environment setup.

    python setup_env.py

Creates .venv, installs all dependencies, verifies yt-dlp + FFmpeg,
and creates the .env file if it doesn't exist.

Cross-platform: Windows / macOS / Linux.
Uses only Python stdlib (can run before any packages are installed).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
VENV_DIR = PROJECT_DIR / ".venv"
REQUIREMENTS = PROJECT_DIR / "requirements.txt"
ENV_FILE = PROJECT_DIR / ".env"
ENV_EXAMPLE = PROJECT_DIR / ".env.example"

# Windows UTF-8 fix.
if sys.platform == "win32":
    os.system("")  # Enable VT100 escape codes on Windows.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def step(n: int, total: int, title: str):
    print(f"\n┌─ Step {n}/{total}: {title}")
    print(f"└{'─' * 50}")


def run(cmd: list[str], label: str, fatal: bool = True) -> bool:
    print(f"  Running: {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(f"  ✗ {label} failed")
        if result.stderr:
            for line in result.stderr.strip().splitlines()[-5:]:
                print(f"    {line}")
        if fatal:
            sys.exit(1)
        return False
    print(f"  ✓ {label}")
    return True


def venv_python() -> str:
    if sys.platform == "win32":
        return str(VENV_DIR / "Scripts" / "python.exe")
    return str(VENV_DIR / "bin" / "python")


# ─────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────

def check_python_version():
    step(1, 6, "Check Python version")
    v = sys.version_info
    print(f"  Python {v.major}.{v.minor}.{v.micro}")
    if v < (3, 10):
        print(f"  ✗ Python 3.10+ required, you have {v.major}.{v.minor}")
        sys.exit(1)
    print(f"  ✓ OK")


def create_venv():
    step(2, 6, "Create virtual environment")
    if VENV_DIR.exists() and Path(venv_python()).exists():
        print(f"  ✓ .venv already exists")
        return
    print(f"  Creating {VENV_DIR}...")
    venv.create(str(VENV_DIR), with_pip=True, clear=True)
    print(f"  ✓ Created")


def install_requirements():
    step(3, 6, "Install Python packages")
    pip = venv_python()
    run([pip, "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        "pip install -r requirements.txt")


def check_ffmpeg():
    step(4, 6, "Check FFmpeg")
    if shutil.which("ffmpeg"):
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        version_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
        print(f"  ✓ {version_line}")
    else:
        print(f"  ✗ FFmpeg not found")
        print(f"    Install: winget install ffmpeg  (Windows)")
        print(f"             brew install ffmpeg     (macOS)")
        print(f"             apt install ffmpeg      (Linux)")
        print(f"    Then re-run this script.")
        sys.exit(1)


def check_ytdlp():
    step(5, 6, "Check yt-dlp")
    pip = venv_python()
    result = subprocess.run(
        [pip, "-m", "yt_dlp", "--version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode == 0:
        print(f"  ✓ yt-dlp {result.stdout.strip()}")
    else:
        print(f"  ✗ yt-dlp not working. Try: {pip} -m pip install -U yt-dlp")
        sys.exit(1)


def setup_env_file():
    step(6, 6, "Setup .env")
    if ENV_FILE.exists():
        print(f"  ✓ .env already exists")
    else:
        if ENV_EXAMPLE.exists():
            shutil.copy(ENV_EXAMPLE, ENV_FILE)
            print(f"  ✓ Created .env from .env.example")
        else:
            ENV_FILE.write_text("DATASET_PATH=D:\\MandpopDataset\n", encoding="utf-8")
            print(f"  ✓ Created .env with default DATASET_PATH")
    # Show current value.
    content = ENV_FILE.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("DATASET_PATH"):
            print(f"  {line}")


def print_next_steps():
    py = venv_python()
    activate = ".venv\\Scripts\\activate" if sys.platform == "win32" else "source .venv/bin/activate"
    print(f"""
┌─ Setup complete! ─────────────────────────────
│
│  Activate the venv:
│    {activate}
│
│  Quick test:
│    python pipeline.py status
│
│  Add your first song:
│    python pipeline.py add "告白氣球"
│
│  Batch process a song list:
│    python pipeline.py batch songs.json
│
│  Edit .env to change DATASET_PATH if needed.
│
│  GPU acceleration:
│    If you have NVIDIA GPU + CUDA, Demucs runs
│    10x faster. No extra setup needed — PyTorch
│    detects CUDA automatically.
│
└────────────────────────────────────────────────
""")


SEPARATOR_VENV = PROJECT_DIR / "tools" / "venvs" / "separator"


def create_separator_venv():
    """Isolated venv for audio-separator (`pipeline.py separate`).

    audio-separator needs numpy 2.x, which breaks transformers/WhisperX in
    the main env. The venv inherits the main env's site-packages (so torch
    is shared, not re-downloaded) and overrides numpy locally.
    """
    sub = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    sep_python = SEPARATOR_VENV / sub
    if sep_python.exists():
        print("  ✓ separator venv already exists")
    else:
        base = venv_python() if Path(venv_python()).exists() else sys.executable
        run([base, "-m", "venv", "--system-site-packages", str(SEPARATOR_VENV)],
            "create tools/venvs/separator", fatal=False)
    if sep_python.exists():
        run([str(sep_python), "-m", "pip", "install", "audio-separator[gpu]"],
            "pip install audio-separator[gpu] (separator venv)", fatal=False)
        # Inherited numba/llvmlite may be built against numpy 1.x and crash
        # librosa under numpy 2 — shadow them with numpy-2 builds locally.
        run([str(sep_python), "-m", "pip", "install", "--ignore-installed",
             "numba>=0.61", "llvmlite>=0.44", "numpy==2.0.2"],
            "pip install numpy-2 numba (separator venv)", fatal=False)


def main() -> int:
    print("=" * 54)
    print("  MandpopDataPipeline — Environment Setup")
    print(f"  Platform: {platform.system()} {platform.machine()}")
    print(f"  Project:  {PROJECT_DIR}")
    print("=" * 54)

    check_python_version()
    create_venv()
    install_requirements()
    check_ffmpeg()
    check_ytdlp()
    create_separator_venv()
    setup_env_file()
    print_next_steps()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
