# Release specification — how MandpopDataset is distributed

> **The core principle: we distribute the *recipe*, not the *dish*.**
> Copyright covers the *bytes of the original recordings*. It does **not** cover
> a reproducible pipeline, pointers to public sources, or non-reconstructive
> derived annotations. So we ship the means to rebuild the audio locally —
> never the audio itself.

This is the same model used by every copyright-safe music-AI dataset:
MusicCaps (YouTube IDs + captions), AudioSet (IDs + labels), the Million Song
Dataset (features, no audio), DISCO-10M (metadata + embeddings), and — the
closest precedent to us — **DALI** (lyric/note *alignments* + a script that
fetches the audio, never the audio).

## What we ship (public, redistributable)

| Artifact | Description | Reconstructive? |
|---|---|---|
| **Pipeline code** | The full `pipeline.py` + crawlers/processors: given a pointer, anyone regenerates the exact stems + alignment on their own machine. | — |
| **Pointers** | `youtube_url` / video ID per song, plus a `source_sha256` content hash of the fetched audio so users can verify they got the same version. | No |
| **Alignment** | `lyrics.lrc` (line-level, gate-verified), future char/word-level timings, `alignment` verdict + offset metrics. | No |
| **Symbolic** | `melody.mid` (Basic Pitch on vocals, incl. pitch-bend), `reduction.mid`. | No |
| **Features** | `features.json` (BPM/key/LUFS/structure), per-syllable tone labels, MERT/codec embeddings. | No |
| **Metadata** | title, artist, lyricist, composer, album, provenance (`discovered_from`). | No |
| **Dataset card** | `docs/DATASET_CARD.md`. | — |

None of the shipped artifacts let you reconstruct the original master
recording. Embeddings, MIDI, timings, and tags are **derived, lossy, and
non-reconstructive** — the accepted safe-harbour category.

## What we do NOT ship

- **`original.mp3`** — the source recording. Never.
- **`vocals.*` / `instrumental.*` stems** — these are *derivative works* of a
  copyrighted recording. Legally greyer than embeddings/MIDI. Users regenerate
  them locally by running the pipeline against the pointer.
- **`instrumental.m4a`** — same reasoning; app consumers read it over the local
  `$DATASET_PATH` interface, it is not part of any public release bundle.

Audio is already `.gitignore`d (see `.gitignore`); this spec makes the policy
explicit and permanent.

## Reproducibility contract

1. **Pin the source.** Every released record carries `youtube_url` + `video_id`
   + `source_sha256`. A rebuild that produces a different hash is flagged.
2. **Pin the toolchain.** Separation model (`htdemucs_ft`), transcription model
   (Basic Pitch ICASSP-2022), and their versions are recorded per song in
   `metadata.json` / `meta.json`, so a rebuild is deterministic up to model
   nondeterminism.
3. **Handle rot.** YouTube videos disappear. We store the content hash and, where
   possible, alternate source URLs, so a taken-down song is *detectable* (rebuild
   fails the hash check) rather than silently substituted with a wrong version.
4. **One command.** `git clone` + `scripts/setup.*` + `pipeline.py add --from
   release_manifest.jsonl` reconstructs the local dataset from a release manifest.

## Release tiers (by role, since scale requirements differ)

| Tier | Contents | Role | Redistribution |
|---|---|---|---|
| **Recipe** | manifest (pointers + hashes) + pipeline code | rebuild locally | ✅ fully public |
| **Annotations** | + alignments, MIDI, tone labels, features | analysis / benchmark | ✅ public (non-reconstructive) |
| **Embeddings** | + MERT / codec features per song | representation learning | ✅ public (non-reconstructive) |
| **Stems** | vocals/instrumental audio | separation / restoration research | ❌ regenerate locally only |

## Legal caveats (state honestly in any release)

- Downloading from YouTube violates YouTube's Terms of Service (a contract
  matter, separate from copyright). Distributing a *tool* that can download
  (as `yt-dlp` itself is distributed) is standard practice; users run it at
  their own discretion. The README must say this plainly.
- Non-reconstructive derivatives (embeddings, MIDI, timings, tags) are the
  safest category and the basis of every precedent dataset above.
- Verify each upstream model's LICENSE before bundling its weights; code and
  weights are frequently licensed separately (e.g. permissive code, CC-BY-NC
  weights).

## Release checklist

- [ ] `release_manifest.jsonl` — one line per song: `{title, video_id,
      youtube_url, source_sha256, toolchain versions}`.
- [ ] Non-reconstructive annotation bundle (LRC, MIDI, features, tone labels).
- [ ] Optional embeddings bundle (MERT / DAC).
- [ ] `docs/DATASET_CARD.md` filled in with current stats + benchmark tasks.
- [ ] README section pointing to this spec + the ToS/licensing note.
- [ ] A `pipeline.py verify` step that re-hashes local audio against the manifest.
