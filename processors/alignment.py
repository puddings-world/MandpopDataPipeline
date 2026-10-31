"""Alignment checks between instrumental/vocals and LRC lyrics.

Two layers of verification:

  Layer 1  Fast VAD-based offset check (seconds per song)
           Detect when vocals first appear in vocals.wav, compare with
           the first lyric line's timestamp in lyrics.lrc. A sustained
           offset means the YouTube version has an intro / header that
           the LRC was not tagged for. Auto-fixable by shifting LRC.

  Layer 2  Whisper-based drift check (minutes per song, optional)
           Run faster-whisper ASR on vocals.wav, fuzzy-match transcribed
           segments against LRC lines, measure time drift across the
           whole song. Catches remixes / wrong-version / mid-song cuts
           that Layer 1 cannot detect.

The module exposes pure functions; pipeline.py wires them into the
`audit` command and into the per-song gate inside `add_song`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import numpy as np
import soundfile as sf


# ─────────────────────────────────────────────
# Thresholds (seconds)
# ─────────────────────────────────────────────

OK_OFFSET_SEC = 0.5            # |offset| below this: alignment is fine
# Mandarin pop MVs sometimes open with a 40-60 s instrumental intro / voice-
# over before the first sung line. Layer 2 (WhisperX) is the safety net
# against shifting an LRC that doesn't belong, so we err on the side of
# attempting the shift rather than declaring misaligned too early.
AUTO_FIX_OFFSET_SEC = 60.0     # below this: auto-shift LRC; above: misaligned
# Layer 2 thresholds tuned for Whisper-on-singing-voice noise floor.
# Empirically:
#   - real correct songs match 30-80% of LRC lines and show median |drift|
#     of 1-3 s (Whisper segment-start bias).
#   - real wrong-LRC / wrong-version songs match 0-5% of lines.
#   - Whisper occasionally fails to transcribe a whole song's verses
#     (sustained vowels, melisma) — match rate can dip to 10-15% even
#     for correct songs, so we don't flag those.
# We err strongly on the side of permissiveness: a single high-confidence
# signal (match rate or median drift) flips the verdict.
DRIFT_OK_MEDIAN_SEC = 10.0     # median |drift| ceiling
DRIFT_OK_MAD_SEC = 8.0         # median absolute deviation ceiling
DRIFT_MIN_MATCH_RATIO = 0.06   # below this ratio of matched lines → wrong_version

# VAD parameters
VAD_FRAME_MS = 30              # analysis window
VAD_HOP_MS = 10                # hop between windows
VAD_MIN_SUSTAIN_MS = 200       # how long energy must stay above threshold
VAD_REL_THRESHOLD = 0.08       # frac of song-wide max RMS to count as "voice"
VAD_ABS_FLOOR_DBFS = -45.0     # absolute dBFS floor (noise gate)


# ─────────────────────────────────────────────
# LRC parsing / writing
# ─────────────────────────────────────────────

# Matches [mm:ss], [mm:ss.xx], [mm:ss.xxx] timestamps.
_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]")

# Lines that look like credits / production metadata even though they're
# timestamped. Common Chinese LRC convention puts these in the first ~30 s
# of the file with their own [mm:ss] tags. Without filtering them out, the
# alignment check picks one of these as the "first lyric line" and reports
# a huge spurious offset.
_CREDIT_KEYWORDS = (
    "作词", "作詞", "作曲", "编曲", "編曲", "填词", "填詞",
    "词", "詞", "曲",
    "制作", "製作", "制作人", "製作人", "监制", "監製", "出品", "发行", "發行",
    "录音", "錄音", "录音师", "錄音師", "录音室", "錄音室",
    "混音", "母带", "母帶", "工程", "工程师", "工程師", "编辑", "編輯",
    "演唱", "和声", "和聲", "和音",
    "吉他", "贝斯", "貝斯", "鼓", "钢琴", "鋼琴", "键盘", "鍵盤",
    "长笛", "長笛", "弦乐", "弦樂", "二胡", "笛", "萨克斯", "薩克斯",
    "口琴", "手鼓", "打击乐", "打擊樂", "管乐", "管樂",
    # Authorization / copyright / OP notices that NetEase increasingly
    # injects as the first timestamped line (so they hide from naive
    # header-tag filters):
    "未经", "未經",   # 未经著作权人书面许可…
    "已通过", "已通過",  # 已通过腾讯音乐启明星…
    "翻唱授权", "翻唱授權",
    "版权", "版權", "版权所有", "版權所有",
    "声明", "聲明",
    "by", "lyric", "lyrics", "composer", "arranger", "producer",
    "album", "artist", "title", "offset",
    # English production credits commonly seen in Mandarin LRCs.
    "production", "coordination", "arrangement", "arranged",
    "recorded", "recording", "engineered", "mixed", "mixing",
    "mastered", "mastering", "remastered",
    "drums", "bass", "guitar", "guitars", "piano", "keyboards", "keyboard",
    "programming", "strings", "vocals", "vocal", "choir",
    "conducted", "conductor", "performed", "performer",
    "saxophone", "violin", "cello", "viola",
    "copyright", "all rights reserved", "authorized",
)

# Pattern A — explicit credit keywords at the start of the line, with or
# without a colon (e.g. "作词 : 林夕", "by:某某").
_KW_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(k) for k in _CREDIT_KEYWORDS) + r")\s*[:：]?\s*",
    re.IGNORECASE,
)

# Pattern B — short Chinese keyword + colon + value (catches credit lines
# whose keyword we didn't enumerate above, e.g. "弦樂編寫 : 某某" or
# "主唱录音室 : 白金录音室 Platinum Studio"). The value side allows
# internal whitespace since credits often look like "First Last Name" or
# "工作室 X & Y". Real lyrics with colons would need to start with a 1-6
# char pure-Chinese token + colon, which is extremely rare.
_CREDIT_FMT_RE = re.compile(
    r"^\s*[一-鿿]{1,8}(?:\s*/\s*[一-鿿]{1,8})?\s*[:：]\s*.+$"
)

# Pattern C — title line, e.g. "歌名 - 周杰伦 (Jay Chou)". Lyrics rarely
# contain " - " with surrounding spaces, so this is a safe signature.
_TITLE_LINE_RE = re.compile(r"^[^\s].*?\s+-\s+\S")


def _is_meta_line(text: str) -> bool:
    if not text:
        return True
    if _KW_RE.match(text):
        return True
    if _CREDIT_FMT_RE.match(text):
        return True
    if _TITLE_LINE_RE.match(text):
        return True
    return False


def _ts_to_sec(m: re.Match) -> float:
    mm = int(m.group(1))
    ss = int(m.group(2))
    frac_raw = m.group(3) or "0"
    # LRC fractional digits can be 2 (centi) or 3 (milli); pad to 3.
    frac = int(frac_raw.ljust(3, "0")[:3]) / 1000.0
    return mm * 60 + ss + frac


def _sec_to_ts(t: float) -> str:
    if t < 0:
        t = 0.0
    mm = int(t // 60)
    ss = t - mm * 60
    return f"[{mm:02d}:{ss:05.2f}]"


@dataclass
class LrcLine:
    time_sec: float
    text: str
    is_meta: bool


def parse_lrc(lrc_text: str) -> list[LrcLine]:
    """Parse LRC text into [(time_sec, text, is_meta)]."""
    lines: list[LrcLine] = []
    for raw in lrc_text.splitlines():
        # A single source line can carry multiple timestamps; expand them.
        stamps = list(_TIMESTAMP_RE.finditer(raw))
        if not stamps:
            continue
        text = _TIMESTAMP_RE.sub("", raw).strip()
        is_meta = _is_meta_line(text)
        for m in stamps:
            lines.append(LrcLine(_ts_to_sec(m), text, is_meta))
    lines.sort(key=lambda l: l.time_sec)
    return lines


def first_lyric_time(lrc_text: str) -> float | None:
    """Return the timestamp (seconds) of the first non-metadata lyric line."""
    for line in parse_lrc(lrc_text):
        if not line.is_meta and line.text:
            return line.time_sec
    return None


def shift_lrc(lrc_text: str, offset_sec: float) -> str:
    """Shift every timestamp in `lrc_text` by `offset_sec`.

    Positive offset → push timestamps later (vocals appear later than LRC says).
    Negative offset → pull timestamps earlier.
    Lines that would land before 0 get clamped to 0.
    """
    def repl(m: re.Match) -> str:
        return _sec_to_ts(_ts_to_sec(m) + offset_sec)
    return _TIMESTAMP_RE.sub(repl, lrc_text)


# ─────────────────────────────────────────────
# Layer 1: VAD-based first-vocal-onset detection
# ─────────────────────────────────────────────

def detect_vocal_onset(vocals_path: Path) -> float | None:
    """Return the time (seconds) when sustained vocal energy first appears.

    Uses RMS energy on the already-isolated Demucs vocals.wav. Because the
    stem is dominated by silence outside of vocal phrases, a simple energy
    threshold + minimum sustain duration is enough to find the first phrase
    onset reliably.

    Returns None if the file cannot be read.
    """
    audio, sr = sf.read(str(vocals_path), always_2d=True)
    if audio.size == 0:
        return None
    # Mono mix.
    mono = audio.mean(axis=1).astype(np.float32)

    frame = max(1, int(sr * VAD_FRAME_MS / 1000))
    hop = max(1, int(sr * VAD_HOP_MS / 1000))

    # Pad so the last frame is full.
    pad = (-(mono.size - frame) % hop) if mono.size > frame else 0
    if pad:
        mono = np.pad(mono, (0, pad))
    n_frames = 1 + (mono.size - frame) // hop

    # Strided view → frames matrix.
    starts = np.arange(n_frames) * hop
    idx = starts[:, None] + np.arange(frame)[None, :]
    frames = mono[idx]
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)

    if rms.size == 0:
        return None

    # Threshold: max(relative · peak, absolute floor).
    peak = rms.max()
    if peak < 1e-5:
        return None
    abs_floor = 10 ** (VAD_ABS_FLOOR_DBFS / 20.0)
    threshold = max(VAD_REL_THRESHOLD * peak, abs_floor)

    voiced = rms > threshold
    sustain_frames = max(1, VAD_MIN_SUSTAIN_MS // VAD_HOP_MS)

    # First index where `sustain_frames` consecutive frames are voiced.
    if voiced.size < sustain_frames:
        return None
    # Rolling-and trick using cumulative sum of bool.
    csum = np.concatenate([[0], np.cumsum(voiced.astype(np.int32))])
    win = csum[sustain_frames:] - csum[:-sustain_frames]
    hits = np.where(win == sustain_frames)[0]
    if hits.size == 0:
        return None
    onset_frame = int(hits[0])
    return onset_frame * hop / sr


# ─────────────────────────────────────────────
# Layer 1: combined offset measurement
# ─────────────────────────────────────────────

@dataclass
class Layer1Result:
    vocal_onset_sec: float | None
    first_lyric_sec: float | None
    offset_sec: float | None        # vocal_onset - first_lyric (positive = LRC early)
    verdict: str                    # "ok" | "fixable" | "misaligned" | "no_data"
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def layer1_check(vocals_path: Path, lrc_path: Path) -> Layer1Result:
    """Run Layer 1 alignment check for one song."""
    if not vocals_path.exists():
        return Layer1Result(None, None, None, "no_data",
                            f"vocals.wav missing: {vocals_path.name}")
    if not lrc_path.exists():
        return Layer1Result(None, None, None, "no_data",
                            f"lyrics.lrc missing: {lrc_path.name}")

    lrc_text = lrc_path.read_text(encoding="utf-8")
    first_lyric = first_lyric_time(lrc_text)
    onset = detect_vocal_onset(vocals_path)

    if first_lyric is None:
        return Layer1Result(onset, None, None, "no_data",
                            "no non-metadata line in LRC")
    if onset is None:
        return Layer1Result(None, first_lyric, None, "no_data",
                            "could not detect vocal onset")

    offset = onset - first_lyric
    abs_off = abs(offset)
    if abs_off <= OK_OFFSET_SEC:
        verdict = "ok"
    elif abs_off <= AUTO_FIX_OFFSET_SEC:
        verdict = "fixable"
    else:
        verdict = "misaligned"
    return Layer1Result(onset, first_lyric, offset, verdict)


def apply_layer1_fix(lrc_path: Path, offset_sec: float,
                     backup_suffix: str = ".orig") -> Path:
    """Apply offset correction to lyrics.lrc, backing up the original.

    Returns the path of the backup file. Idempotent for the backup file:
    if `lyrics.lrc.orig` already exists, it is preserved (so repeated
    audits don't overwrite the true original).
    """
    backup = lrc_path.with_suffix(lrc_path.suffix + backup_suffix)
    if not backup.exists():
        backup.write_bytes(lrc_path.read_bytes())
    original = lrc_path.read_text(encoding="utf-8")
    lrc_path.write_text(shift_lrc(original, offset_sec), encoding="utf-8")
    return backup


# ─────────────────────────────────────────────
# Layer 2: Whisper drift check
# ─────────────────────────────────────────────

@dataclass
class Layer2Result:
    matched_lines: int
    total_lyric_lines: int
    mean_abs_drift_sec: float | None
    std_drift_sec: float | None
    max_abs_drift_sec: float | None
    verdict: str                    # "ok" | "drift" | "wrong_version" | "skipped"
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


try:
    import zhconv as _zhconv
except ImportError:
    _zhconv = None


def _normalize_cn(text: str) -> str:
    """Forgiving signature for fuzzy line matching.

    Whisper output and LRC text disagree freely on simplified ↔ traditional.
    We canonicalize everything to simplified via zhconv (Wikipedia tables)
    when available, else fall back to a small hand-curated translation
    table covering the most common Mandarin pop characters. Then strip
    punctuation/whitespace.
    """
    if _zhconv is not None:
        text = _zhconv.convert(text, "zh-cn")
    else:
        pairs = str.maketrans({
            "後": "后", "麼": "么", "說": "说", "聽": "听", "為": "为",
            "誰": "谁", "讓": "让", "見": "见", "頭": "头", "風": "风",
            "離": "离", "夢": "梦", "愛": "爱", "個": "个", "點": "点",
            "時": "时", "間": "间", "還": "还", "過": "过", "裡": "里",
            "對": "对", "會": "会", "來": "来", "這": "这", "兩": "两",
            "覺": "觉", "燈": "灯", "電": "电", "樣": "样", "經": "经",
            "處": "处", "張": "张", "輝": "辉", "遠": "远", "黃": "黄",
            "藍": "蓝", "紅": "红", "綠": "绿", "雲": "云", "雨": "雨",
        })
        text = text.translate(pairs)
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


def _ngram_set(s: str, n: int = 2) -> set[str]:
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _similarity(a: str, b: str) -> float:
    """Bigram Jaccard on normalized Chinese text. Cheap and good enough."""
    a, b = _normalize_cn(a), _normalize_cn(b)
    if not a or not b:
        return 0.0
    A, B = _ngram_set(a), _ngram_set(b)
    inter = len(A & B)
    union = len(A | B) or 1
    return inter / union


class WhisperXContext:
    """Lazily-loaded WhisperX models, kept alive across many songs.

    Loading the Whisper + wav2vec2 pair takes ~30 s on a 3070 and would
    dominate runtime if redone per song. The audit driver keeps one
    instance for the whole run.
    """

    def __init__(self, device: str = "cuda",
                 model_size: str = "medium",
                 compute_type: str = "float16"):
        self.device = device
        self.model_size = model_size
        self.compute_type = compute_type
        self.asr_model = None
        self.align_model = None
        self.align_meta = None
        self._load_failed = False

    def ensure_loaded(self):
        if self._load_failed or self.asr_model is not None:
            return
        try:
            import whisperx
        except ImportError:
            self._load_failed = True
            return
        try:
            self.asr_model = whisperx.load_model(
                self.model_size, device=self.device,
                compute_type=self.compute_type, language="zh")
        except Exception:
            self.device = "cpu"
            self.compute_type = "int8"
            self.asr_model = whisperx.load_model(
                self.model_size, device="cpu",
                compute_type="int8", language="zh")
        try:
            self.align_model, self.align_meta = whisperx.load_align_model(
                language_code="zh", device=self.device)
        except Exception:
            self.align_model = None
            self.align_meta = None


def _transcribe_whisperx(vocals_path: Path, device: str,
                         model_size: str, compute_type: str,
                         ctx: "WhisperXContext | None" = None,
                         use_align: bool = False
                         ) -> tuple[list[tuple[float, str]], str] | None:
    """Run WhisperX on vocals.wav and return (word_or_segment_list, backend).

    With word-level forced alignment available, returns a list of
    (start_sec, single_char_or_word_text) for every recognized token.
    LRC lines are then matched by sliding a time window and concatenating
    nearby words — much more robust than matching whole segments which
    WhisperX likes to merge across many lyric lines.

    Falls back to (segment_start, segment_text) tuples if the alignment
    pass is unavailable.
    """
    try:
        import whisperx
    except ImportError:
        return None

    if ctx is None:
        ctx = WhisperXContext(device=device, model_size=model_size,
                              compute_type=compute_type)
    ctx.ensure_loaded()
    if ctx.asr_model is None:
        return None

    audio = whisperx.load_audio(str(vocals_path))
    asr = ctx.asr_model.transcribe(audio, batch_size=16, language="zh")

    raw_segments = asr.get("segments", []) or []
    if not raw_segments:
        return [], "whisperx-asr-empty"

    # By default we skip the wav2vec2 alignment pass — empirically it is
    # slow on long singing audio (often hangs). For drift detection at
    # ~1s tolerance, Whisper's own segment-level timestamps are enough,
    # and Layer 2's substring-based matcher works directly off them.
    if not use_align or ctx.align_model is None:
        out = [(float(s.get("start", 0.0)), (s.get("text") or "").strip())
               for s in raw_segments if (s.get("text") or "").strip()]
        return out, "whisperx-segments"

    try:
        aligned = whisperx.align(raw_segments, ctx.align_model, ctx.align_meta,
                                 audio, ctx.device,
                                 return_char_alignments=False)
        out: list[tuple[float, str]] = []
        for seg in aligned.get("segments", []):
            for w in (seg.get("words") or []):
                start = w.get("start")
                text = (w.get("word") or "").strip()
                if start is not None and text:
                    out.append((float(start), text))
            # Fall back to segment-level if a segment has no word timings
            # (rare, but happens for very short utterances).
            if not (seg.get("words") or []):
                s = seg.get("start")
                t = (seg.get("text") or "").strip()
                if s is not None and t:
                    out.append((float(s), t))
        if not out:
            # No words came through — emit segments instead.
            out = [(float(s.get("start", 0.0)), (s.get("text") or "").strip())
                   for s in aligned.get("segments", [])
                   if (s.get("text") or "").strip()]
        out.sort(key=lambda x: x[0])
        return out, "whisperx-aligned"
    except Exception as e:
        out = [(float(s.get("start", 0.0)), (s.get("text") or "").strip())
               for s in raw_segments if (s.get("text") or "").strip()]
        return out, f"whisperx-no-align ({type(e).__name__})"


def _transcribe_faster_whisper(vocals_path: Path, device: str,
                               model_size: str, compute_type: str
                               ) -> tuple[list[tuple[float, str]], str] | None:
    """Fallback transcriber if whisperx is unavailable.

    Same return shape as `_transcribe_whisperx`. Segment-level start times
    only — drift resolution is ~0.5–1 s instead of ~100 ms.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(
        str(vocals_path), language="zh", beam_size=1, vad_filter=True,
        condition_on_previous_text=False,
    )
    out = [(float(s.start), s.text.strip()) for s in segments
           if (s.text or "").strip()]
    return out, "faster-whisper"


def layer2_check(vocals_path: Path, lrc_path: Path,
                 model_size: str = "medium",
                 device: str = "cuda",
                 compute_type: str = "float16",
                 min_match_sim: float = 0.4,
                 ctx: WhisperXContext | None = None) -> Layer2Result:
    """Compare ASR-aligned vocal timing against LRC line timing.

    Uses WhisperX (Whisper transcription + wav2vec2 word-level forced
    alignment) when available; falls back to faster-whisper segment
    timestamps otherwise. Returns a "skipped" verdict (rather than
    raising) if neither is installed.
    """
    if not vocals_path.exists() or not lrc_path.exists():
        return Layer2Result(0, 0, None, None, None, "skipped",
                            "missing vocals.wav or lyrics.lrc")

    lyric_lines = [l for l in parse_lrc(lrc_path.read_text(encoding="utf-8"))
                   if not l.is_meta and l.text]
    if not lyric_lines:
        return Layer2Result(0, 0, None, None, None, "skipped",
                            "no lyric lines to compare")

    result = _transcribe_whisperx(vocals_path, device, model_size,
                                  compute_type, ctx=ctx)
    if result is None:
        result = _transcribe_faster_whisper(vocals_path, device, "small",
                                            compute_type)
    if result is None:
        return Layer2Result(0, 0, None, None, None, "skipped",
                            "neither whisperx nor faster-whisper installed")

    asr, backend = result
    if not asr:
        return Layer2Result(0, len(lyric_lines), None, None, None,
                            "wrong_version", f"{backend}: no segments")

    # Build a flat normalized character stream with a parallel time array,
    # so we can substring-match an LRC line directly and recover the
    # exact ASR time of its first character. Each ASR item (segment or
    # word) is treated uniformly: characters within an item linearly
    # interpolate between item.start and the next item's start.
    flat_chars: list[str] = []
    flat_times: list[float] = []
    for i, (start, text) in enumerate(asr):
        next_start = asr[i + 1][0] if i + 1 < len(asr) else start + 5.0
        norm = _normalize_cn(text)
        if not norm:
            continue
        span = max(next_start - start, 0.5)
        for k, ch in enumerate(norm):
            flat_chars.append(ch)
            # Distribute characters linearly across the item's time span.
            flat_times.append(start + (k / max(len(norm), 1)) * span)
    flat_text = "".join(flat_chars)

    if not flat_text:
        return Layer2Result(0, len(lyric_lines), None, None, None,
                            "wrong_version", f"{backend}: empty transcript")

    # ── Step 1: GLOBAL content-presence check ─────────────────────────
    # "Is this the right song?" — independent of how the LRC times are
    # currently shifted. Count LRC lines that occur anywhere in the flat
    # ASR transcript. This catches the case where the LRC content is
    # correct but the times happen to be mis-shifted (so the windowed
    # match below would miss them).
    global_hits = 0
    for ll in lyric_lines:
        needle = _normalize_cn(ll.text)
        if len(needle) >= 4 and needle[:min(8, len(needle))] in flat_text:
            global_hits += 1
    global_ratio = global_hits / max(1, len(lyric_lines))

    if global_ratio < DRIFT_MIN_MATCH_RATIO:
        # Almost no LRC content appears in the audio — wrong song
        # (or Whisper completely failed on this voice).
        return Layer2Result(global_hits, len(lyric_lines), None, None, None,
                            "wrong_version",
                            f"{backend}: only {global_hits}/{len(lyric_lines)} "
                            f"LRC lines present in transcript ({global_ratio:.0%})")

    # ── Step 2: TIME-WINDOWED drift analysis ──────────────────────────
    # The song is the right one; now check whether timing is consistent.
    drifts: list[float] = []
    flat_times_arr = np.array(flat_times)
    window_radius = 20.0   # accept a match within ±20 s of the LRC time
    for ll in lyric_lines:
        line_norm = _normalize_cn(ll.text)
        if len(line_norm) < 2:
            continue

        def _find_all(needle: str) -> list[int]:
            hits, k = [], 0
            while True:
                idx = flat_text.find(needle, k)
                if idx < 0:
                    break
                if abs(flat_times_arr[idx] - ll.time_sec) <= window_radius:
                    hits.append(idx)
                k = idx + 1
            return hits

        candidates = _find_all(line_norm)
        if not candidates:
            for trim in range(1, min(4, len(line_norm) - 1)):
                candidates = _find_all(line_norm[:-trim])
                if candidates:
                    break
        if not candidates:
            continue

        cand_times = flat_times_arr[candidates]
        closest = int(candidates[int(np.argmin(np.abs(cand_times - ll.time_sec)))])
        drifts.append(flat_times[closest] - ll.time_sec)

    matched = len(drifts)
    if matched < 3:
        # Content is globally present but the window-restricted match
        # is sparse — current LRC times are off. Compute a GLOBAL median
        # drift (ignoring the time window) so second-pass auto-shift has
        # a number to work with.
        global_drifts: list[float] = []
        for ll in lyric_lines:
            line_norm = _normalize_cn(ll.text)
            if len(line_norm) < 4:
                continue
            for trim in (0, 1, 2):
                needle = line_norm[:-trim] if trim else line_norm
                idx = flat_text.find(needle)
                if idx >= 0:
                    global_drifts.append(flat_times_arr[idx] - ll.time_sec)
                    break
        if len(global_drifts) >= 5:
            arr = np.array(global_drifts)
            gmed = float(np.median(arr))
            gmad = float(np.median(np.abs(arr - gmed)))
            gmax = float(np.max(np.abs(arr)))
            return Layer2Result(global_hits, len(lyric_lines),
                                gmed, gmad, gmax, "drift",
                                f"{backend}: content present ({global_ratio:.0%}); "
                                f"global median drift {gmed:+.1f}s — needs reshift")
        return Layer2Result(global_hits, len(lyric_lines), None, None, None,
                            "drift",
                            f"{backend}: content present ({global_ratio:.0%}) "
                            f"but unable to compute reshift")

    arr = np.array(drifts)
    median = float(np.median(arr))
    # Median + MAD instead of mean + std: a few wildly wrong matches
    # (refrain pulled from the wrong repetition, ASR hallucination)
    # don't blow up the signal, so we only flag drift when most matches
    # are off, not when 1-2 outliers exist.
    mad = float(np.median(np.abs(arr - median)))
    max_abs = float(np.max(np.abs(arr)))
    verdict = "ok" if (abs(median) <= DRIFT_OK_MEDIAN_SEC and
                       mad <= DRIFT_OK_MAD_SEC) else "drift"
    # We keep the field names for JSONL stability but the semantics are
    # now: `mean_abs_drift_sec` = signed median (preserves direction so
    # the second-pass auto-shifter knows which way to nudge); `std_drift_sec`
    # = MAD; `max_abs_drift_sec` = max |drift| as before.
    return Layer2Result(matched, len(lyric_lines), median, mad, max_abs,
                        verdict, (backend or "") + " (median+MAD)")


# ─────────────────────────────────────────────
# Combined audit per song
# ─────────────────────────────────────────────

@dataclass
class AuditReport:
    title: str
    layer1: Layer1Result
    layer2: Layer2Result | None
    auto_fixed_offset_sec: float | None = None
    overall: str = "unknown"        # "ok" | "fixed" | "drift" | "misaligned" | "no_data"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "layer1": self.layer1.to_dict(),
            "layer2": self.layer2.to_dict() if self.layer2 else None,
            "auto_fixed_offset_sec": self.auto_fixed_offset_sec,
            "overall": self.overall,
        }


def audit_song(song_dir: Path, run_layer2: bool = False,
               auto_fix: bool = True,
               whisper_device: str = "cuda",
               whisper_ctx: WhisperXContext | None = None) -> AuditReport:
    """Run Layer 1 (and optionally Layer 2) on a single song folder.

    When auto_fix is on and Layer 2 reports a tight, large drift (median
    > 5 s with MAD < 4 s), apply an additional shift equal to the Layer 2
    median and re-check. This rescues songs where VAD picked the wrong
    onset (e.g. Demucs leaked instrumental hum into the vocal stem) so
    Layer 1's shift was systematically short.
    """
    from processors.encode import find_stem
    title = song_dir.name
    # Accept either vocals.wav (fresh from Demucs) or vocals.flac (after
    # compact). soundfile / WhisperX both read FLAC natively.
    vocals = find_stem(song_dir, "vocals") or (song_dir / "vocals.wav")
    lrc = song_dir / "lyrics.lrc"

    l1 = layer1_check(vocals, lrc)
    fixed = None
    if auto_fix and l1.verdict == "fixable" and l1.offset_sec is not None:
        apply_layer1_fix(lrc, l1.offset_sec)
        fixed = l1.offset_sec
        l1 = layer1_check(vocals, lrc)

    l2 = None
    if run_layer2 and l1.verdict in ("ok", "fixable"):
        l2 = layer2_check(vocals, lrc, device=whisper_device, ctx=whisper_ctx)

        # Layer-2 second-pass: a tight large drift means Layer 1's VAD
        # onset was off (Demucs leaked instrumental hum, MV intro voice-
        # over, etc.). The signed median is now stored in
        # mean_abs_drift_sec, so we can shift directly.
        #
        # We allow looser MAD when the song is unambiguously the right
        # one (high global content match — matched_lines is the global
        # hit count in the new code path). For ≥10 LRC lines present in
        # the transcript we trust the median even if individual matches
        # spread (typical when a song has many repeated refrains).
        if (auto_fix and l2.verdict == "drift" and l2.matched_lines >= 3 and
                l2.mean_abs_drift_sec is not None and
                abs(l2.mean_abs_drift_sec) > 5.0):
            tight = (l2.std_drift_sec is None or l2.std_drift_sec < 4.0)
            high_confidence = l2.matched_lines >= 10
            if tight or high_confidence:
                extra = l2.mean_abs_drift_sec
                apply_layer1_fix(lrc, extra)
                fixed = (fixed or 0.0) + extra
                l1 = layer1_check(vocals, lrc)
                l2 = layer2_check(vocals, lrc, device=whisper_device,
                                  ctx=whisper_ctx)

    overall = _overall_verdict(l1, l2, fixed)
    return AuditReport(title=title, layer1=l1, layer2=l2,
                       auto_fixed_offset_sec=fixed, overall=overall)


def _overall_verdict(l1: Layer1Result, l2: Layer2Result | None,
                     auto_fixed: float | None) -> str:
    if l1.verdict == "no_data":
        return "no_data"
    if l1.verdict == "misaligned":
        return "misaligned"
    if l2 is not None:
        if l2.verdict == "wrong_version":
            return "misaligned"
        if l2.verdict == "drift":
            return "drift"
    if auto_fixed is not None:
        return "fixed"
    if l1.verdict == "fixable":
        # auto_fix was disabled but the offset is in the shiftable range.
        return "fixable"
    return "ok"


def audit_all(songs_root: Path, run_layer2: bool = False,
              auto_fix: bool = True,
              whisper_device: str = "cuda",
              progress: bool = True,
              titles: list[str] | None = None) -> Iterable[AuditReport]:
    """Yield an AuditReport per song under songs_root.

    Loads the WhisperX model exactly once for the whole run when Layer 2
    is enabled, so the per-song cost stays roughly proportional to song
    duration rather than being dominated by a 30 s model load.
    """
    song_dirs = sorted(d for d in songs_root.iterdir() if d.is_dir())
    if titles is not None:
        wanted = set(titles)
        song_dirs = [d for d in song_dirs if d.name in wanted]
    total = len(song_dirs)
    ctx = WhisperXContext(device=whisper_device) if run_layer2 else None
    for i, d in enumerate(song_dirs, 1):
        if progress:
            print(f"  [{i}/{total}] {d.name}", flush=True)
        yield audit_song(d, run_layer2=run_layer2, auto_fix=auto_fix,
                         whisper_device=whisper_device, whisper_ctx=ctx)
