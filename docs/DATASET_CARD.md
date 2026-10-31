# Dataset card — MandpopDataset

> A reproducible, alignment-verified corpus of Mandarin/Chinese pop songs with
> separated stems and time-aligned lyrics, distributed as a **recipe** (pipeline
> + pointers + non-reconstructive annotations), not as audio. See
> `docs/RELEASE_SPEC.md` for the distribution model.
>
> **Positioning:** the Mandarin-pop analogue of **DALI** (Meseguer-Brocal,
> Cohen-Hadria & Peeters, ISMIR 2018 / TISMIR 2020) — a synchronized
> audio+lyrics+notes resource shipped as alignments + pointers. Our
> differentiators: Mandarin/tonal-language content, character/pinyin-level
> alignment, separated vocal/instrumental stems, and tone (倒字) labels —
> none of which DALI (overwhelmingly Western/English) provides.

## At a glance

- **Domain:** Mandarin / Chinese-language popular music (any era; sung or not).
- **Languages:** Mandarin Chinese (primary), some English/other in lyrics.
- **Modalities shipped:** pointers + line-aligned lyrics (LRC) + metadata +
  (rolling out) melody MIDI, tone labels, MER​T/codec embeddings, audio features.
- **Audio:** NOT distributed. Regenerated locally from pointers via the pipeline.
- **Stems:** vocals + instrumental via Demucs `htdemucs_ft`, 44.1 kHz stereo,
  regenerated locally.
- **License posture:** code permissive (see repo LICENSE); annotations/features
  released as non-reconstructive derivatives; source recordings remain the
  rightsholders'.

## Current statistics (update on each release)

- Discovered candidates (raw index): **~15,100** (`raw_dataset.jsonl`;
  chart/era discovery, mostly awaiting processing).
- Fully processed through the pipeline: **174**.
- Alignment-verified (in `processed_dataset.jsonl`): **139**
  — 99 `ok` + 40 `fixed` (auto-shifted then re-verified).
- Held in raw only: 28 `misaligned` + 7 `drift` (wrong version / wrong LRC).
- Verified subset audio ≈ 9–10 hours (est., 44.1 kHz stereo stems).

> Scale note: at ~140 verified songs this is a **benchmark / analysis-scale**
> resource, not a pretraining corpus (foundation models train on 10k–160k h).
> The verified count grows as discovered candidates pass the alignment gate.

## Per-song fields

| Field | Source | Shipped |
|---|---|---|
| `title`, `artist`, `lyricist`, `composer`, `album` | crawlers + LRC + YouTube title + NetEase/QQ API | ✅ |
| `youtube_url`, `video_id`, `source_sha256` | crawl + hashing | ✅ |
| `discovered_from`, `netease_id` | provenance | ✅ |
| `lyrics.lrc` (line-level, gate-verified) | NetEase → QQ → YouTube subs fallback | ✅ |
| `alignment` verdict + offset / drift metrics | `processors/alignment.py` | ✅ |
| `melody.mid`, `reduction.mid` | Basic Pitch / ByteDance transcription | ✅ (rolling out) |
| `features.json` (BPM/key/LUFS/structure) | `pipeline.py features` | ✅ (rolling out) |
| per-syllable tone labels + F0 contour | tone analysis pass | ✅ (planned) |
| MERT / DAC embeddings | representation pass | ✅ (planned) |
| `vocals.*`, `instrumental.*`, `original.mp3` | pipeline output | ❌ local only |

## Collection & verification process

1. **Discover / select** a song (chart crawl or curated list) → `youtube_url`.
2. **Download** audio (`yt-dlp`) → **separate** stems (Demucs `htdemucs_ft`).
3. **Fetch lyrics** with timestamps: NetEase → QQ Music → YouTube subs fallback.
4. **Alignment gate** (`processors/alignment.py`):
   - *Layer 1 (VAD):* RMS voice-onset vs first lyric line. `|offset|≤0.5s` → `ok`;
     `0.5–30s` → auto-shift the whole LRC, re-verify → `fixed`; `>30s` →
     `misaligned` (stays raw).
   - *Layer 2 (WhisperX):* word-level forced alignment, per-line drift vs LRC;
     high drift → `drift`, low match rate → `wrong_version`.
5. **Promote** to `processed_dataset.jsonl` only if `status=done` ∧
   `alignment∈{ok,fixed}` ∧ `lyrics=lrc` ∧ `m4a=True`.

The gate itself — a verified-alignment flag on real pop recordings — is part of
the contribution (a candidate Mandarin sung-lyric alignment benchmark).

## Intended uses

- **Representation learning:** probing/fine-tuning MERT/MusicFM/CLAP on Mandarin
  pop (genre/era/artist/key/tempo classification, lyric–audio retrieval).
- **Tone-melody (倒字) analysis:** real sung F0 vs lexical tone, aligned to lyrics.
- **Sung-Mandarin lyric-alignment benchmarking.**
- **Generative fine-tuning / LoRA:** conditioning data (lyrics + stems + melody)
  for open song models (ACE-Step, YuE, DiffRhythm) and SVS/SVC (DiffSinger, RVC).
- **Source-separation / restoration** research on real pop stems (regenerated).

## Limitations & biases

- **Selection bias:** chart-/curator-driven; not a uniform sample of Mandarin pop.
- **Separation artifacts:** stems are `htdemucs_ft` (2022) only; artifacts set a
  noise floor for vocal/instrumental research. Alternative separators staged but
  not yet promoted.
- **Alignment depth:** LRC is line-level; word/char-level alignment is in
  progress (Layer 2 WhisperX historically blocked by a numpy/transformers env
  conflict — run AMT/alignment in an isolated env).
- **Metadata gaps:** no `year`/`genre` field yet for most records; discovered
  candidates carry only `title`/`artist`/provenance until processed.
- **Reproducibility risk:** sources can be taken down; content hashes make this
  detectable, not preventable.

## Ethical / legal

- Audio is not redistributed; only non-reconstructive derivatives + a rebuild
  recipe (see `docs/RELEASE_SPEC.md`).
- YouTube ToS and per-model weight licenses are the user's responsibility;
  documented in the README.

## Citation

TODO on first public release (title, author, year, URL, version/commit).
