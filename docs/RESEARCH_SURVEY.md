# Research survey — MandpopDataset

> Living literature + methods survey for the project. Scope: what a Mandarin-pop
> "audio + aligned lyrics + notes" dataset can uniquely enable, the SOTA to build
> on, and the open-source resources to use. Companion to `docs/RELEASE_SPEC.md`
> (distribution) and `docs/DATASET_CARD.md` (the dataset itself).
>
> **Positioning: the Mandarin-pop analogue of DALI.** DALI proved the model —
> ship synchronized audio+lyrics+notes as *alignment annotations + audio
> pointers*, not audio. We do the same for Mandarin pop and add what DALI lacks:
> tonal-language handling, character/pinyin alignment, separated stems, and tone
> (倒字) labels.

Last integrated: 2026-07. Sections marked "▶ living" grow over time.

---

## 1. DALI — the reference we are modelling on

**Canonical papers.** Meseguer-Brocal, Cohen-Hadria & Peeters, *"DALI: a large
Dataset of synchronized Audio, LyrIcs and notes, automatically created using
teacher-student machine learning paradigm"* (ISMIR 2018, arXiv:1906.10606);
extended as *"Creating DALI…"* (TISMIR 3(1), 2020, DOI 10.5334/tismir.30, open
access). Affiliations: IRCAM + Télécom Paris.

**Spec.**
- Size: v1 = 5,358 songs; v2 = 7,756 (v2 ≈ 488 h audio, ~247 h with vocals).
- Each song: time-aligned vocal-melody notes + lyrics in a **4-level hierarchy —
  notes → words → lines → paragraphs**. Every element carries start/end time,
  a note frequency range (Hz), text, and up/down hierarchy indices. v2 adds
  per-word **phoneme** sequences (English-centric CMU G2P).
- Metadata: artist, title, genre (~63), language (~32), album, YouTube link(s),
  release date, and a per-song **NCC confidence score** users can filter on.
- Languages: overwhelmingly Western — **English ≈ 76%**, then German, French;
  non-English ≈ 24%. Mandarin content is negligible.

**Distribution model (this is exactly our RELEASE_SPEC).** DALI ships **only
annotations + metadata (gzipped pickles), never audio**. Users fetch audio on
their own machine from the YouTube URLs via the provided code, which verifies by
MD5/duration and re-checks alignment. **Link rot is DALI's central weakness and
worsening** — dead/region-blocked/replaced IDs mean no two users reconstruct the
same audio set. Access is gated (non-commercial research agreement); `mirdata`
provides a loader.

**Method — teacher-student alignment (the core contribution).**
1. Seed = karaoke/LRC lyrics with rough timing but *no audio*.
2. **Teacher** = a Singing-Voice-Detection (SVD) CNN on 80 log-mel bands
   (~14 ms/frame), trained on clean vocal-activity ground truth (Jamendo +
   MedleyDB). Outputs a frame-level singing probability p̂(t).
3. Build a binary vocal-activity signal from the karaoke line timings; align it
   to p̂(t) by maximizing **Normalized Cross-Correlation** over a global offset
   `o`: `NCC(o) = Σ_t vas(t−o)·p̂(t) / (√Σ vas² · √Σ p̂²)`. This both corrects
   global LRC drift **and confirms the audio is the right version**.
4. **Filter** to NCC ≥ 0.8 (keep only well-aligned songs).
5. **Student** = retrain SVD on the newly labeled set → cleaner p̂(t) → admits
   more songs. **Iterate** v0→v1→v2 (bootstrapping).

**Quality.** SVD ≈ 93.4% ± 3.6% on held-out Jamendo; alignment precision vs a
manually annotated **~105-song** ground-truth subset; MIREX-style melody metrics.

**Subsequent work & current standing.** DALI became the de-facto large training
set for lyrics alignment (ALA) and automatic lyrics transcription (ALT), ~2018–
2023. Notable follow-ups: Schulze-Forster et al. (TASLP 2021) phoneme alignment +
text-informed separation and the **MUSDB18-lyrics** extension; lyric-informed
source separation; multimodal singing language ID. Evaluation standard moved to
CC-licensed **JamendoLyrics** (MIREX ALA benchmark) and **Jam-ALT** (ISMIR 2024);
training now often pairs DALI v2 with **MulJam v2** (Jamendo-based). As of
2024–2026 DALI is regarded as **pioneering but aging** — hampered by YouTube link
rot and noisy machine alignment; the field leans to self-hostable CC sets and
Whisper-era pipelines. It remains a foundational reference, not a SOTA benchmark.

### 1.1 What our Mandarin-DALI must add (differentiators)

| DALI | MandpopDataset (Mandarin-DALI) |
|---|---|
| English ≈ 76%, tonal langs ~0 | Mandarin pop, tonal-language first |
| Pitch treated as language-neutral melody | **Tone-aware**: lexical tone contour vs sung melody (倒字 labels) |
| word/phoneme (English G2P) | **character ↔ pinyin ↔ syllable/phoneme** layers (Chinese has no word spaces → segmentation + Mandarin G2P) |
| No stems distributed | **Separated vocal/instrumental stems** (Demucs) regenerated locally |
| Teacher = SVD-CNN + NCC | Gate = **VAD (Layer 1) + WhisperX word-align (Layer 2)** — same idea, real stems |
| Link rot, no integrity check | **content hash per source** (RELEASE_SPEC) → rot is *detectable* |

**Lessons adopted from DALI:** (a) NCC-style alignment doubles as
wrong-version detection — worth adding to our gate; (b) teacher-student
bootstrapping is how they scaled to thousands — our gate can iterate the same
way as the corpus grows; (c) their link-rot pain validates our content-hash
pinning; (d) adopt a DALI-like annotation hierarchy, extended with pinyin/tone.

---

## 2. Field landscape / SOTA ▶ living

### 2.1 Representation learning / SSL (embeddings, MIR)
MERT (M-A-P, 2023; dual acoustic+musical teacher; HF, Apache sizes) · MusicFM
(2023, MIT) · CLAP (LAION, 2023, Apache; text↔audio) · MULE (2022, research) ·
Jukebox features (2020) · wav2vec2/HuBERT/WavLM (speech SSL reused for vocals).
Benchmark: MARBLE.

### 2.2 Neural audio codecs (tokenizers / substrate)
DAC (2023, **MIT**, 44.1 kHz, best general) · EnCodec (2022, MIT) · X-Codec/
X-Codec2 (2024–25; used by YuE) · Mimi (2024, Apache) · SNAC / WavTokenizer
(2024, MIT). These define the token vocabulary generative LMs predict over.

### 2.3 Generative — audio
AR token LM: **YuE** (2025, Apache, Mandarin-capable) · MusicGen/MAGNeT (2023–24,
code MIT / weights CC-BY-NC) · LeVo (Tencent, 2025). Latent diffusion / DiT:
**DiffRhythm / DiffRhythm 2** (2025, Apache) · **ACE-Step** (2025, Apache;
official LoRA) · Stable Audio Open (Stability community license) · AudioLDM2 /
Tango2 / Mustango (CC-BY-NC). Spectrogram-image: Riffusion (MIT). Flow matching:
MusicFlow, DiffRhythm 2, F5-TTS.

### 2.4 Generative — symbolic
Representations: MIDI-like → **REMI** (2020) → **CP / Compound Word** (2021) →
Octuple (MusicBERT). Models: Pop Music Transformer, Compound Word Transformer,
MMT (MIT), Museformer, FIGARO, Aria (Apache), Anticipatory Music Transformer
(Apache), SongComposer (2024, Apache, Chinese). Microsoft **Muzic** (MIT):
MusicBERT, **DeepRapper** (Chinese rap), **ReLyMe** (tone-melody harmony),
TeleMelody, MeloForm, MuseCoco, GETMusic, CLaMP. Mandarin-relevant corpus:
**POP909** (Mandarin pop piano arrangements).

### 2.5 Singing synthesis / conversion (SVS/SVC)
SVS (score→voice): DiffSinger (MIT), VISinger2, NNSVS. SVC (voice→voice): RVC
(MIT), So-VITS-SVC (AGPL), Seed-VC (2024). Vocoders: BigVGAN, HiFi-GAN, Vocos
(all MIT). Mandarin SVS corpora: Opencpop (~5 h), M4Singer (~30 h), OpenSinger,
GTSinger — all studio a-cappella, research-only.

### 2.6 Lyrics alignment / transcription (our core lineage)
WhisperX (word-level forced align; our Layer 2) · Montreal Forced Aligner ·
torchaudio CTC aligner · whisper-timestamped (AGPL) · NUS AutoLyrixAlign.
Benchmarks: JamendoLyrics, Jam-ALT (2024). AMT for the notes layer: Basic Pitch
(Apache; our main engine), ByteDance piano (Apache), MT3 (Apache, multi-instr).

---

## 3. Concepts glossary ▶ living

- **REMI** — beat-based MIDI tokenization (Bar/Position/Tempo/Chord/Note-On/
  Velocity/Duration); models learn rhythm from explicit positions.
- **CP (Compound Word)** — packs same-time fields into one multi-field super-token
  predicted by parallel heads; ~½ the sequence length of REMI, cheaper.
- **Neural audio codec** — trained encoder→RVQ→decoder that turns waveform into
  discrete tokens; the "alphabet" every AR audio generator builds on.
- **DiT (Diffusion Transformer)** — diffusion with a Transformer backbone. Latent
  is **patchified** (image: split into p×p blocks; audio: split the T×C latent
  along time into length-p segments), each block linearly projected to one token;
  conditioning injected via cross-attention or adaLN. Patch size = compute knob.
- **Frozen embedding probing** — run a pretrained model (e.g. MERT) *frozen* to
  get per-song vectors, then train only a tiny linear classifier on them. Measures
  what the model already encodes; near-zero compute; interpretable; layer-wise
  probing localizes where key/timbre/genre live.
- **Tone-melody / 倒字 (dǎozì)** — when a sung melody's pitch direction contradicts
  a syllable's lexical tone, listeners hear a different word. Studied in
  ethnomusicology/linguistics (mostly Cantonese) and a young MIR thread (ReLyMe);
  empirical corpus-scale study on real Mandarin-pop vocals is essentially open.
- **Teacher-student alignment** — DALI's scaling trick: a teacher model labels
  data, a student is retrained on it, iterate to bootstrap a large aligned set.

---

## 4. Research roadmap ▶ living

Constraint: prioritize **low compute** (single A100/4090 rentable, not unlimited).
The dataset is benchmark/analysis-scale (~140 verified songs), not a pretraining
corpus.

### 4.1 First wins — near-zero compute (do these first)
1. **Tone-melody (倒字) analysis.** F0 (CREPE/pYIN) on real `vocals.wav` → align
   to LRC syllables → tone via `pypinyin` → quantify melody-contour vs tone-
   direction concordance. Pure signal processing; uniquely enabled by aligned
   real vocals (prior work uses symbolic scores). Corpus statistics across
   artists/eras/lyricists = a resource/analysis contribution.
2. **Frozen-embedding probing.** Cache MERT/MusicFM/CLAP embeddings once, train
   CPU linear probes for era/artist/genre/key/tempo + lyric↔audio retrieval.
   Novelty: a Mandarin-pop probing benchmark with layer-wise analysis.
3. **Sung-Mandarin lyric-alignment benchmark.** Compare WhisperX vs MFA vs
   wav2vec2-CTC on sung Mandarin with a hand-corrected gold subset. Under-
   benchmarked; the metrics + gold set are citable.
4. **Evaluation-metric research.** A tone-correctness metric for generated singing
   and an alignment-quality metric — both computable from F0 + lyrics, no training.

### 4.2 Headline generative results — sub-24 GB LoRA
| Rank | Base | License | LoRA VRAM | Note |
|---|---|---|---|---|
| ★ | **ACE-Step** | Apache-2.0 | <10 GB (woct0rdho fork) | official LoRA tutorial + chinese-rap-LoRA precedent; lyrics + style caption from metadata |
| ★ | **Seed-VC** | GPL-ish (check) | ~8 GB / T4 | zero-shot SVC, adapt from 1 utterance in minutes |
| | **RVC** | MIT | 8–12 GB | ~10 min clean vocals; mature |
| | **DiffSinger** | MIT | 12–16 GB | melody MIDI becomes a first-class condition; needs MFA phoneme durations |
| | MusicGen small/med | code MIT / weights CC-BY-NC | 24 GB | research-only; melody-conditioned |
| | DiffRhythm 2 | Apache-2.0 | A100 full FT | best data fit; thin FT scripts |
| | YuE 7B | Apache-2.0 | ~80 GB | top quality, worst bang-for-buck |

### 4.3 Build-on-SOTA (low compute + novelty)
- ReLyMe-style tone constraints on a small symbolic melody LM, evaluated with our
  倒字 metric on real data.
- DiffSinger per-artist Mandarin-pop fine-tune (vocals + F0 + MFA + MIDI).
- Small (≤100 M) Mandarin melody LM over DAC/X-Codec tokens, one 4090.
- Cascade symbolic→audio: Basic Pitch MIDI → MIDI-DDSP / Spectrogram-Diffusion
  renderer, fine-tune only the renderer.

---

## 5. Open-source resource catalog ▶ living

Permissive = Apache/MIT/BSD (safe to build on, incl. commercial). ⚠ = restrictive
(CC-BY-NC / copyleft / research-only); verify LICENSE + weights at ingest.

- **Separation:** Demucs/htdemucs_ft (MIT) · BS-RoFormer, Mel-Band RoFormer, MDX23C
  (code MIT; some weights ⚠) · audio-separator (MIT) · Spleeter, Open-Unmix (MIT).
- **Transcription (AMT):** Basic Pitch (Apache) · ByteDance piano (Apache) · MT3
  (Apache) · Omnizart (MIT) · CREPE/torchcrepe (MIT) · madmom (BSD; some models ⚠)
  · Essentia (AGPL ⚠) · librosa (ISC).
- **Codecs:** DAC (MIT) · EnCodec (MIT) · X-Codec/2 (MIT/Apache) · Mimi (Apache) ·
  SNAC, WavTokenizer (MIT) · Stable Codec (STAI non-commercial ⚠).
- **Representation:** MERT (Apache sizes; some cards CC-BY-NC ⚠) · MusicFM (MIT) ·
  CLAP (Apache) · MULE (⚠) · Jukebox (MIT) · wav2vec2/HuBERT/WavLM (MIT/Apache).
- **Gen — audio:** MusicGen/MAGNeT (code MIT / weights CC-BY-NC ⚠) · AudioLDM2
  (⚠) · Stable Audio Open (community ⚠) · Tango2, Mustango (⚠) · **YuE, DiffRhythm,
  ACE-Step, SongGen, OpenMusic/QA-MDT (Apache/MIT)** · Riffusion (MIT).
- **Gen — symbolic:** Magenta/Music Transformer (Apache) · MMT, Compound Word
  Transformer, FIGARO (MIT) · Museformer, Muzic suite (MIT) · Aria, Anticipatory
  MT, SongComposer (Apache).
- **Singing:** DiffSinger, NNSVS, VISinger2, RVC, OpenUtau, NSF-HiFiGAN (MIT) ·
  So-VITS-SVC (AGPL ⚠) · Seed-VC (GPL ⚠).
- **Vocoders:** BigVGAN, HiFi-GAN, Vocos (MIT).
- **Alignment:** WhisperX (BSD) · MFA (MIT) · torchaudio aligner (BSD) ·
  whisper-timestamped (AGPL ⚠) · AutoLyrixAlign (⚠) · DALI tools (code Apache).
- **Datasets:** MusicCaps (CC-BY-SA, IDs) · FMA (CC) · MTG-Jamendo (⚠) · MUSDB18
  (⚠) · Slakh (CC-BY) · MAESTRO (⚠) · Lakh MIDI (CC-BY) · POP909, GiantMIDI (⚠) ·
  Opencpop/M4Singer/OpenSinger/GTSinger (⚠, Mandarin) · DALI, DISCO-10M, MSD (⚠/
  metadata) · JamendoLyrics, Jam-ALT (ALA benchmarks).
- **Eval:** MARBLE · mir_eval · museval · frechet-audio-distance · SingMOS (all
  MIT/Apache).

---

## 6. References

- DALI ISMIR 2018 (PDF): http://ismir2018.ircam.fr/doc/pdfs/35_Paper.pdf
- DALI arXiv:1906.10606: https://arxiv.org/abs/1906.10606
- DALI TISMIR 2020 (DOI 10.5334/tismir.30): https://transactions.ismir.net/articles/10.5334/tismir.30
- DALI code: https://github.com/gabolsgabs/DALI
- DALI Zenodo v1 (10.5281/zenodo.2577915): https://zenodo.org/records/2577915
- DALI Zenodo v2 (10.5281/zenodo.3576083): https://zenodo.org/records/3576083
- Schulze-Forster et al. (TASLP 2021) + MUSDB18-lyrics: https://schufo.github.io/publications/2021-TASLP ; https://zenodo.org/records/3989267
- JamendoLyrics / MIREX ALA 2024: https://music-ir.org/mirex/wiki/2024:Lyrics-to-Audio_Alignment
- Jam-ALT (ISMIR 2024): https://audioshake.github.io/jam-alt/
- ALT preprocessing (DALI/MulJam/Jamendo): https://github.com/jhuang448/ALT-Preprocess/
- MERT: https://huggingface.co/papers/2306.00107
- YuE: https://github.com/multimodal-art-projection/YuE
- DiffRhythm 2: https://arxiv.org/abs/2510.22950
- ACE-Step LoRA tutorial: https://github.com/ace-step/ACE-Step/blob/main/ZH_RAP_LORA.md ; <10 GB fork: https://github.com/woct0rdho/ACE-Step
- ReLyMe: https://microsoft.github.io/muzic/relyme/
- Basic Pitch: https://github.com/spotify/basic-pitch
