"""
Source separation — multi-method dispatch.

Two engines today:
  * Demucs htdemucs_ft (4-stem; we sum drums+bass+other → instrumental)
  * audio-separator (UVR-style RoFormer / MDX 2-stem models)

Both engines share one contract: write vocals.{wav|flac} +
instrumental.{wav|flac} + meta.json into the target out_dir.

Canonical Phase 1 stems live at the song root
(`songs/{title}/{vocals,instrumental}.wav`). Alternative methods land in
`songs/{title}/separations/{method}/` for non-destructive A/B comparison
ahead of any promotion decision.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

DEMUCS_MODEL = "htdemucs_ft"   # fine-tuned, best vocal isolation among Demucs variants
DEFAULT_METHOD = "htdemucs_ft"


# Registry: method id → engine config.
# `engine` picks the runtime; `model` is the engine-specific model identifier
# (Demucs name, or the audio-separator weights filename it will auto-download).
_METHODS: dict[str, dict] = {
    "htdemucs_ft": {
        "engine": "demucs",
        "model": "htdemucs_ft",
        "description": "Demucs htdemucs_ft (Phase 1 baseline, 4-stem)",
    },
    "bs_roformer": {
        "engine": "audio_separator",
        "model": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        "description": "BS-RoFormer (UVR community weights, 2024 SOTA vocals)",
    },
    "mel_band_roformer": {
        "engine": "audio_separator",
        "model": "mel_band_roformer_kim_ft_unwa.ckpt",
        "description": "Mel-Band RoFormer Kim FT (alternative SOTA vocals)",
    },
    "mdx23c": {
        "engine": "audio_separator",
        "model": "MDX23C-8KFFT-InstVoc_HQ.ckpt",
        "description": "MDX23C-8K InstVoc HQ (older, lighter than RoFormer)",
    },
    "inst_becruily": {
        "engine": "audio_separator",
        "model": "mel_band_roformer_instrumental_becruily.ckpt",
        "description": "Mel-Band RoFormer Instrumental by becruily (all vocals out, fullness-oriented)",
    },
    # Karaoke models remove only the LEAD vocal; backing vocals / harmonies
    # stay in instrumental.*, and vocals.* holds the lead vocal alone.
    "karaoke_aufr33": {
        "engine": "audio_separator",
        "model": "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt",
        "description": "Mel-Band RoFormer Karaoke by aufr33/viperx (lead vocal out, keeps harmonies)",
    },
    "karaoke_gabox_v2": {
        "engine": "audio_separator",
        "model": "mel_band_roformer_karaoke_gabox_v2.ckpt",
        "description": "Mel-Band RoFormer Karaoke V2 by Gabox (lead vocal out, keeps harmonies)",
    },
    "karaoke_becruily": {
        "engine": "audio_separator",
        "model": "mel_band_roformer_karaoke_becruily.ckpt",
        "description": "Mel-Band RoFormer Karaoke by becruily (lead vocal out, keeps harmonies)",
    },
}

PROJECT_DIR = Path(__file__).resolve().parent.parent
SEPARATOR_VENV = PROJECT_DIR / "tools" / "venvs" / "separator"
SEPARATOR_MODEL_DIR = PROJECT_DIR / "tools" / "models" / "audio-separator"


def list_methods() -> dict[str, str]:
    """Return {method_id: description} for every registered method."""
    return {k: v["description"] for k, v in _METHODS.items()}


def separate_to_dir(
    input_audio: Path,
    out_dir: Path,
    method: str = DEFAULT_METHOD,
    device: str = "cuda",
    output_format: str = "wav",
) -> dict:
    """Run the named separation method on `input_audio`.

    Writes into `out_dir`:
      * vocals.<output_format>
      * instrumental.<output_format>
      * meta.json  — {method, engine, model, runtime_sec, separated_at, ...}

    Returns the meta dict.
    """
    if method not in _METHODS:
        raise ValueError(
            f"Unknown method '{method}'. Available: {sorted(_METHODS)}"
        )

    input_audio = Path(input_audio).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not input_audio.exists():
        raise FileNotFoundError(f"Input audio not found: {input_audio}")

    cfg = _METHODS[method]
    engine = cfg["engine"]
    model = cfg["model"]

    t0 = time.time()
    if engine == "demucs":
        _separate_demucs(input_audio, out_dir, model=model, device=device,
                         output_format=output_format)
    elif engine == "audio_separator":
        _separate_audio_separator(input_audio, out_dir, model=model,
                                  device=device, output_format=output_format)
    else:
        raise RuntimeError(f"Unknown engine: {engine}")
    runtime = time.time() - t0

    meta = {
        "method": method,
        "engine": engine,
        "model": model,
        "device": device,
        "output_format": output_format,
        "source": input_audio.name,
        "runtime_sec": round(runtime, 1),
        "separated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return meta


# ─────────────────────────────────────────────
# Backward-compat wrapper (used by `pipeline.py add`)
# ─────────────────────────────────────────────

def separate_vocals(
    input_audio: Path,
    output_dir: Path,
    model: str = DEMUCS_MODEL,
    device: str = "cpu",
) -> Path:
    """Phase 1 canonical separation: htdemucs_ft → song-root vocals/instrumental.wav.

    Kept stable for `pipeline.py add` callers. New code that needs method
    choice should call `separate_to_dir()` directly.
    """
    separate_to_dir(input_audio, output_dir, method="htdemucs_ft",
                    device=device, output_format="wav")
    return Path(output_dir) / "instrumental.wav"


# ─────────────────────────────────────────────
# Engine: Demucs (4-stem, sum drums+bass+other)
# ─────────────────────────────────────────────

def _separate_demucs(
    input_audio: Path,
    out_dir: Path,
    model: str = DEMUCS_MODEL,
    device: str = "cuda",
    output_format: str = "wav",
) -> None:
    """Run Demucs via its CLI module, then merge non-vocal stems → instrumental."""
    demucs_out = out_dir / "_demucs_raw"
    demucs_out.mkdir(exist_ok=True)

    print(f"  [Demucs] model={model} device={device}")
    cmd = [
        sys.executable, "-m", "demucs.separate",
        "-n", model,
        "-d", device,
        "--out", str(demucs_out),
        str(input_audio),
    ]
    # Windows: force UTF-8 — Demucs echoes the source filename (Chinese
    # titles common here) to stderr; cp950 decoder crashes otherwise.
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        print(f"  [Demucs] stdout: {(result.stdout or '')[-500:]}")
        print(f"  [Demucs] stderr: {(result.stderr or '')[-500:]}")
        raise RuntimeError("Demucs failed — check that `demucs` is installed")

    stem_dir = demucs_out / model / input_audio.stem
    if not stem_dir.exists():
        candidates = list((demucs_out / model).glob(f"{input_audio.stem}*"))
        if not candidates:
            raise RuntimeError(f"Demucs output not found: {stem_dir}")
        stem_dir = candidates[0]

    drums = stem_dir / "drums.wav"
    bass = stem_dir / "bass.wav"
    other = stem_dir / "other.wav"
    vocals = stem_dir / "vocals.wav"

    for p in (drums, bass, other):
        if not p.exists():
            raise RuntimeError(f"Missing Demucs stem: {p}")

    inst_path = out_dir / f"instrumental.{output_format}"
    voc_path = out_dir / f"vocals.{output_format}"

    print(f"  [Demucs] merging drums+bass+other → instrumental")
    _mix_stems_to_file([drums, bass, other], inst_path)
    if output_format == "wav":
        shutil.copy2(vocals, voc_path)
    else:
        audio, sr = sf.read(str(vocals), always_2d=True)
        sf.write(str(voc_path), audio, sr)

    shutil.rmtree(demucs_out, ignore_errors=True)


def _mix_stems_to_file(stem_paths: list[Path], output_path: Path) -> None:
    """Sum the given audio stems and write the result. Anti-clip to peak 0.99."""
    mixed = None
    sr = None
    for p in stem_paths:
        audio, cur_sr = sf.read(str(p), always_2d=True)
        if sr is None:
            sr = cur_sr
            mixed = np.zeros_like(audio, dtype=np.float64)
        elif cur_sr != sr:
            raise RuntimeError(f"Inconsistent sample rate: {p}")
        mixed += audio

    peak = np.max(np.abs(mixed))
    if peak > 1.0:
        mixed = mixed / peak * 0.99

    sf.write(str(output_path), mixed.astype(np.float32), sr)


# ─────────────────────────────────────────────
# Engine: audio-separator (UVR-style 2-stem)
# ─────────────────────────────────────────────

def _separate_audio_separator(
    input_audio: Path,
    out_dir: Path,
    model: str,
    device: str = "cuda",
    output_format: str = "wav",
) -> None:
    """Run a UVR-style 2-stem model (BS-RoFormer / Mel-Band RoFormer / MDX).

    Uses the audio-separator package, which auto-downloads weights and
    handles GPU/CPU dispatch behind one API. It runs in a subprocess under
    the isolated separator venv (it needs numpy 2.x, the main env does not
    allow that). Output filenames vary by model, so we rename whatever it
    produces back to our standard vocals.<ext> / instrumental.<ext> contract.
    """
    python = _separator_python()
    if python is None:
        raise RuntimeError(
            "audio-separator venv not found. Create it with:\n"
            "  python setup_env.py --separator"
        )

    print(f"  [audio-separator] model={model} device={device}")
    SEPARATOR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    worker = Path(__file__).with_name("_separator_worker.py")
    cmd = [python, str(worker), str(input_audio), str(out_dir), model,
           output_format.upper(), str(SEPARATOR_MODEL_DIR)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    marker = next((ln for ln in (result.stdout or "").splitlines()
                   if ln.startswith("@@WRITTEN@@")), None)
    if result.returncode != 0 or marker is None:
        print(f"  [audio-separator] stderr: {(result.stderr or '')[-800:]}")
        raise RuntimeError("audio-separator failed")
    written = json.loads(marker[len("@@WRITTEN@@"):])

    # `written` is a list of filenames relative to out_dir. The canonical
    # UVR naming is "<stem>_(Vocals).<ext>" / "<stem>_(Instrumental).<ext>",
    # but some models use "no_vocals" or "music". Match permissively.
    voc_target = out_dir / f"vocals.{output_format}"
    inst_target = out_dir / f"instrumental.{output_format}"
    voc_target.unlink(missing_ok=True)
    inst_target.unlink(missing_ok=True)

    # Classify by the "_(Stem)" tag only — the model filename that follows
    # it can itself contain "instrumental"/"vocals" (e.g. *_instrumental_becruily).
    for fname in written:
        path = (out_dir / fname).resolve()
        if not path.exists():
            continue
        m = re.search(r"_\(([^)]+)\)", path.name)
        tag = (m.group(1) if m else path.name).lower()
        if any(t in tag for t in ("instrumental", "no vocals", "no_vocals",
                                  "music", "other", "karaoke")):
            path.rename(inst_target)
        elif "vocal" in tag:
            path.rename(voc_target)

    if not voc_target.exists() or not inst_target.exists():
        raise RuntimeError(
            f"audio-separator did not produce both stems. Written: {written}"
        )


def _separator_python() -> str | None:
    """Interpreter that has audio-separator: $SEPARATOR_PYTHON, else the venv."""
    override = os.environ.get("SEPARATOR_PYTHON")
    if override:
        return override
    for rel in ("Scripts/python.exe", "bin/python"):
        p = SEPARATOR_VENV / rel
        if p.exists():
            return str(p)
    return None
