# Proposal: export alignment detail as a first-class dataset output

> Requested by the reprise-studio consumer (lyric-MV studio). Status: proposal —
> not started. Blocked in part by the numpy 2.x ↔ transformers conflict noted in
> CLAUDE.md Phase 1.

## Motivation

Consumers currently get line-START times only (`lyrics.lrc`). reprise-studio
must infer each line's END as "next line's start, capped at 6s", which is wrong
for held notes (cut early) and pre-interlude lines (linger too long). The
pipeline already computes richer timing during audit and throws it away:

- `layer2_check` transcribes vocals (WhisperX / faster-whisper), fuzzy-matches
  every LRC line, computes per-line drift — then keeps only summary stats.
- `_transcribe_whisperx(use_align=True)` can emit word-level tokens
  (~100ms precision) but is disabled by default (wav2vec2 alignment is slow /
  hangs on long singing audio).
- Layer 1's VAD already knows the vocal-activity envelope of `vocals.wav`.

## Proposed output

Persist per-song `songs/{title}/alignment.json` during audit:

```json
{
  "backend": "whisperx-segments",
  "computed_at": "2026-07-03",
  "vocal_onset_sec": 25.19,
  "lines": [
    { "t": 25.19, "end": 29.01, "text": "空荡的街景", "matched": true, "drift_sec": 0.04 }
  ],
  "words": [
    { "t": 25.19, "end": 25.61, "text": "空" }
  ]
}
```

- `lines[].end` — when the singing of that line actually stops.
- `words[]` — optional; only when word-level alignment ran (`use_align=True`).
- Add to the processed record when present:
  `"files": { ..., "alignment": "songs/{title}/alignment.json" }`.
  Absence is fine — consumers must treat it as optional.

## Staged implementation (each stage independently useful)

1. **VAD line-ends (no ASR, unblocked today).** Reuse Layer 1's RMS-VAD /
   silence detection over `vocals.wav`: a line's `end` = start of the first
   silence interval inside `[t_i, t_{i+1})`. Seconds per song, no GPU, no
   whisperx import — sidesteps the numpy conflict entirely. Emits
   `lines[].end` only.
2. **Persist Layer-2 line matching.** When `audit --layer2` runs, write the
   per-line match/drift detail into the same file instead of discarding it
   (data is already in memory in `layer2_check`).
3. **Word-level tokens (gated).** `audit --layer2 --align` opt-in per song or
   batch; requires solving the numpy 2.x ↔ transformers conflict and the
   wav2vec2 hang. Emits `words[]` for karaoke-style per-word highlighting and
   future cover-vocal re-alignment.

## Also worth promoting upstream eventually

Section boundaries (verse/chorus/bridge) + mood/cue labels are currently
hand-curated per song in reprise-studio (`data/songs/<id>/curation.json`).
Long-term these belong here as labeled data (CLAUDE.md of reprise-studio
already states this goal). Stage 1's vocal-activity envelope plus
instrumental-stem energy is most of the signal needed for boundary suggestion.

## Consumer-side note (already shipped in reprise-studio)

Its intake adapter now runs an "alignment doctor": ffmpeg `silencedetect` on
`vocals.flac` to (a) drop credit lines before the vocal onset regardless of
wording, and (b) warn when the first lyric line precedes the vocal onset by
> 0.5s (the Layer 1 gate tolerance). Two findings from running it on 4 songs:

- Pre-lyric vocal activity (hums/adlibs) is common (一直很安靜: vocals from
  ~19.6s, first lyric line 25.19s) — Layer 1's "onset vs first lyric" equality
  assumption holds only because its meta-line filter and VAD parameters happen
  to agree; a hum-heavy song could false-positive. Stage 1's envelope would
  make this robust.
- The meta-line prefix filter misses variants (`词：`/`曲：` without the 作
  prefix slipped through on 不能說的秘密's NetEase LRC). Onset-based cutting
  is wording-independent.
