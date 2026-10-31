# Schema — MandpopDataset fields

> The full attribute/metadata schema, oriented to the end goal: an AI that can
> **compose and arrange Mandarin pop**. Extends DALI (notes + lyrics) with
> chords, per-stem arrangement, tonal-language alignment, and representations.
>
> **Redistributable?** column follows `docs/RELEASE_SPEC.md`: `yes` =
> non-reconstructive derivative, safe to publish; `local` = regenerate on the
> user's machine, never shipped.

Two tracks share this schema:
- **Symbolic track** — chords/melody/arrangement MIDI + tonal alignment. Fully
  redistributable; low-compute; where tone-aware composition research lives.
- **Audio track** — stems + embeddings. Regenerated locally; feeds audio-gen
  fine-tuning.

Storage convention (non-destructive, per song), extending the current layout:
```
songs/{title}/
├── metadata.json          identity + provenance + musical attributes (below)
├── lyrics.lrc             line-level (existing)
├── lyrics.aligned.json    word/char/pinyin/tone/phoneme layers
├── transcription/         symbolic track (MIDI + analysis)
│   ├── melody.mid  bass.mid  chords.json  beats.json  structure.json
│   └── meta.json          engine/model/version/runtime per field
├── features.json          BPM/key/LUFS/tempo-curve
├── tone.json              F0 contour + per-syllable pitch + concordance
├── embeddings/            mert.npy (seq+pooled), dac.tokens (local-ish)
└── {stems, m4a}           audio track — local only
```

## 1. Identity / metadata
| field | type | source | redistributable |
|---|---|---|---|
| `title` | str | crawl | yes |
| `artist` | str | LRC / YT title / API | yes |
| `lyricist` | str | LRC / API | yes |
| `composer` | str | LRC / API | yes |
| `arranger` | str | API / manual (often missing) | yes |
| `album` | str | API | yes |
| `release_year` | int | API / MusicBrainz | yes |
| `genre` | list[str] | API / tag model | yes |
| `label` | str | API | yes |
| `language` | str | detect (zh/yue/en-mix) | yes |
| `isrc` | str | MusicBrainz (if found) | yes |

## 2. Source / provenance
| field | type | source | redistributable |
|---|---|---|---|
| `youtube_url`, `video_id` | str | crawl | yes |
| `source_sha256` | str | hash of fetched audio | yes |
| `duration_sec` | float | ffprobe | yes |
| `sample_rate` | int | ffprobe | yes |
| `discovered_from` | str | discovery provenance | yes |
| `netease_id` / `qq_id` | str | lyric API | yes |

## 3. Lyrics / alignment (DALI core, extended for Mandarin)
| field | type | source | redistributable |
|---|---|---|---|
| `lyrics_text` | str | crawl | yes |
| `lrc_lines` | list[{t, text}] | NetEase/QQ/YT | yes |
| `words` | list[{t0,t1,text}] | WhisperX | yes |
| `chars` | list[{t0,t1,char}] | char split of words | yes |
| `pinyin` | list[{char, pinyin, tone}] | `pypinyin` | yes |
| `phonemes` | list[{t0,t1,phone}] | Mandarin G2P (pypinyin→phone) | yes |
| `sections` | list[{t0,t1,label}] | structure model (intro/verse/chorus/bridge) | yes |
| `alignment_hierarchy` | notes→words→lines→sections | assembled (DALI-style) | yes |

## 4. Musical attributes (compose / arrange ground truth)
| field | type | source | redistributable |
|---|---|---|---|
| `bpm` | float | madmom / all-in-one | yes |
| `tempo_curve` | list[{t, bpm}] | beat tracker | yes |
| `key`, `mode` | str | Essentia / all-in-one | yes |
| `time_signature` | str | downbeat tracker | yes |
| `beats`, `downbeats` | list[float] | madmom / beat-this | yes |
| `chords` | list[{t0,t1,chord}] | Chordino / autochord / Omnizart | yes |
| `sections` | list[{t0,t1,func}] | all-in-one / MSAF | yes |
| `instrumentation` | list[str] | stems present + tagging | yes |
| `lufs`, `dynamics` | float | pyloudnorm | yes |

## 5. Vocal / tone analysis
| field | type | source | redistributable |
|---|---|---|---|
| `f0_contour` | list[{t, hz}] | CREPE / pYIN on vocals | yes |
| `syllable_pitch` | list[{char, hz, midi}] | F0 × char alignment | yes |
| `tone_melody_concordance` | float + per-syllable | tone dir vs melody dir | yes |
| `vibrato` | list | F0 analysis | yes |
| `singer_gender` | str | classifier / metadata | yes |

## 6. Symbolic track (arrangement MIDI)
| field | type | source | redistributable |
|---|---|---|---|
| `melody.mid` | MIDI | Basic Pitch on vocals (+ pitch-bend) | yes |
| `bass.mid` | MIDI | Basic Pitch on bass stem | yes |
| `reduction.mid` | MIDI | Basic Pitch / ByteDance on harmonic stem | yes |
| `drums` | events | ADT (ADTLib / Omnizart-drum) | yes |
| `chords.json` | list | chord recognition | yes |

## 7. Representations (audio track)
| field | type | source | redistributable |
|---|---|---|---|
| `mert_seq` | array [T×d] | MERT frozen (per-frame, per-layer) | yes |
| `mert_pooled` | array [d] | mean-pool of above | yes |
| `dac_tokens` | array | DAC / EnCodec encode | yes (non-reconstructive at token level; document) |
| `clap` | array [d] | CLAP embedding | yes |

## 8. Quality / gate
| field | type | source | redistributable |
|---|---|---|---|
| `status` | enum | pipeline | yes |
| `alignment` | enum {ok,fixed,fixable,drift,misaligned,wrong_version,no_data} | gate | yes |
| `offset_sec`, `drift_mean/std` | float | Layer 1/2 | yes |
| `ncc_score` | float | DALI-style NCC (planned) | yes |
| `processed` | bool | is_processed() | yes |

## 9. Audio (local only — never shipped)
`original.mp3`, `vocals.{wav,flac}`, `instrumental.{wav,flac}`, per-stem audio,
`instrumental.m4a`. Regenerated from `youtube_url` + `source_sha256`.

---

### Notes
- Symbolic/analysis fields are **silver** (machine-derived, transcription error).
  Keep a small **hand-corrected gold subset** for benchmarking (as DALI kept ~105).
- Every derived field records `engine`, `model`, `version` in the adjacent
  `meta.json` so a rebuild is deterministic and comparable across model upgrades.
- `dac_tokens` reconstruct a lossy approximation; document this and gate its
  release conservatively (embeddings like MERT are safer / non-invertible).
