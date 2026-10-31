# 音樂建模研究路線圖

> 在 MandpopDataset 累積的資料（curated instrumentals / vocals stems / lyrics）上做的長期建模研究。
>
> 從 StreetPerformerMaster 的 `RESEARCH_LONG_TERM.md` 移過來。該專案是**消費者**（街頭播放 / 翻唱錄音），本專案是**資料層 + 模型層**。
>
> 章節編號保留原 roadmap 編號（§11、§16.3、§17.2–§17.6）方便 cross-reference。

> 一句話：列在這裡的研究都需要本資料集**累積到一定量 + 算力 + 多年研究時間**。

---

## 為什麼放在 MandpopDataset

- 這些研究都依賴**累積到足夠量的乾淨華語流行 vocal / instrumental stems**——這正是本專案 `htdemucs_ft` pipeline 的長期產出。
- 跟 StreetPerformerMaster 拆開：那邊是「街頭表演 / 翻唱錄音」工具鏈，本專案是資料 + 模型。
- 跨專案使用：訓練好的模型最終回流到消費者專案（StreetPerformerMaster 的 studio 修音、未來 VocalStudio 的混音）。

### 拆出來源摘要

| 章節 | 從哪裡搬來 | 原拆出原因 |
|---|---|---|
| §11 SVS / RVC / 聲音合成 | StreetPerformerMaster RESEARCH_LONG_TERM.md | 倫理 + 法律灰區、不解決街頭表演者直接痛點 |
| §16.3 自訓音樂修復 diffusion | 同上 | RTX 3070 + 個人短期做不到，且依賴 curated dataset |
| §17.2–§17.6 自訓 vocal restoration diffusion | 同上 | 同 §16.3，PhD-scale undertaking |

回到消費者專案的研究路線圖 →
- StreetPerformerMaster `RESEARCH_ROADMAP.md`（短期可評估工具）
- StreetPerformerMaster `RESEARCH_LONG_TERM.md`（其他長期研究：§13 Differentiable DSP、§14 AI 即時生成）

---

## §11. 歌聲合成 / 修音 (Singing Voice Synthesis & Correction)

> 從譜或歌詞直接合成歌聲、或把錄好的歌聲對 pitch / 變換音色。對「產和聲軌」「修音」「demo 合唱」有直接用途。StreetPerformerMaster §5 人聲處理講的是傳統方法，這裡是 2023+ 的 AI 方法。

| 工具 / 模型 | 用途 | 狀態 |
|------|-----|------|
| **DiffSinger** | Diffusion-based SVS，音質自然 | 📋 待測 |
| **NNSVS** | Neural network 歌聲合成框架，日系 Vocaloid 社群常用 | 📋 待研究 |
| **VISinger2** | VITS 架構的 end-to-end SVS | 📋 待研究 |
| **RVC v2 / RVC WebUI** | 短 training 就能 clone 的音色轉換 | 📋 研究中（倫理注意） |
| **So-VITS-SVC 5.0** | SVS + 音色轉換 | 📋 評估中 |
| **OpenVoice v2** | Zero-shot voice cloning（TTS + SVS 混用） | 📋 待研究 |
| **NSF-HiFiGAN** | 歌聲專用的 neural vocoder | 📋 參考 |
| **F5-TTS / E2 TTS** | 最新 flow matching TTS，可改 SVS | 📋 待研究 |

**研究問題**：
- [ ] 用自己聲音 fine-tune RVC 模型 → 產 3 部和聲的 workflow
- [ ] 錄完歌後用 DiffSinger re-synth 修音 vs Melodyne 手動的品質差距
- [ ] 華語歌聲 SVS 目前有沒有好用的 pretrained model（vs 日語生態）
- [ ] **倫理紅線**：什麼場景下用 AI 聲音合成是 OK 的（自己的聲、公有領域、明確授權）

### 對 MandpopDataset 的依賴
- 需要 vocals stem 大量累積（pipeline `htdemucs_ft` 預設輸出）
- 需要 lyrics.lrc 對齊（作為 phoneme conditioning）
- Jay Chou 等 NetEase 缺歌詞的歌曲，要靠 QQ Music / WhisperX fallback 才能用

---

## §16.3 訓練自己的音樂 diffusion 修復模型 ⭐

> §16 章其餘部分（§16.1 既有模型評估、§16.2 音樂專用方向、§16.4 快速折衷）仍在 StreetPerformerMaster RESEARCH_ROADMAP.md。

**為什麼**：現有模型都不是針對「華語流行伴奏」訓練的。如果有專屬模型可以：
- 知道華語流行的音色特徵（吉他 / 鼓 / 鋼琴 / 弦樂常見配置）
- 知道哪些伴奏「應該長什麼樣」
- 可以從低品質還原到高品質

**訓練資料**：
- MandpopDataset 累積的高品質 instrumental.wav（過 `htdemucs_ft` 後的乾淨集）
- 對應的 degraded 版本（人為加 mp3 壓縮、低通、雜訊）
- pair (degraded → clean) 就是 training set

| 方法 | 描述 | 狀態 |
|------|------|------|
| **Music Diffusion Restoration**（自訓） | 從零訓 diffusion 模型專門做音樂修復 | 📋 長期目標 |
| **Fine-tune Stable Audio** | 用已有模型加 domain adaptation | 📋 較可行 |
| **Fine-tune AudioLDM2** | 同上，diffusion-based | 📋 待研究 |
| **EnCodec / DAC fine-tune** | 把 codec 在華語流行伴奏上 fine-tune，再用 codec-domain 去噪 | 📋 待研究 |
| **小模型蒸餾** | 從大模型蒸餾出能在 RTX 3070 跑的小模型 | 📋 進階 |
| **Score-based restoration** | 用預訓 score model 當 prior 做 inverse problem | 📋 待研究論文 |

**研究問題**：
- [ ] 訓練 music restoration diffusion 需要多少資料量（1000 / 10000 / 100000 首）
- [ ] RTX 3070 (8GB VRAM) 能 fine-tune 哪個尺寸的模型
- [ ] 自訓模型 vs 直接用 AudioSR / Apollo 的成本效益（我的時間 vs 結果差距）
- [ ] 用 codec-domain (EnCodec tokens) 做修復能否大幅降低算力需求
- [ ] 「人為 degrade」資料合成的 pipeline 該怎麼設計（要 cover 真實 YT 伴奏的劣化分布）

### 對 MandpopDataset 的依賴
- 需要 instrumental.wav 累積到 ≥ 1000 首高品質
- 需要對應的「品質評分」（StreetPerformerMaster RESEARCH_ROADMAP §15 的 curation pipeline）— 這部分將來可能也要併入本 repo
- 需要建立 degradation pipeline（人為破壞 clean → degraded）

---

## §17.2 – §17.6 訓練自己的 vocal restoration diffusion

> §17 章前段（§17 概述、§17.1 既有模型評估）仍在 StreetPerformerMaster RESEARCH_ROADMAP.md。

### §17.2 訓練自己的 vocal restoration diffusion ⭐

**為什麼自訓**：上面那些模型都不是針對「華語流行歌聲」訓練的。專屬模型可以：
- 認得華語子音（ㄗㄘㄙ / ㄓㄔㄕ / 鼻音）的清晰特徵
- 認得華語流行抒情常用的尾音收法、轉音、vibrato 速率
- 不會把華語的入聲 / 短促音當成 artifact 削掉

**訓練資料來源**：
- 高品質歌聲：MandpopDataset 的 vocals.wav（已 Demucs `htdemucs_ft` 抽過、curated 過的乾淨集）
- 自己累積的好錄音（少量但 distribution match）
- 對應 degraded 版本：在 clean vocal 上**人為破壞**（synthetic pair）

### §17.3 Degradation 設計（合成 training pair 的關鍵）

訓練資料的 degradation pipeline 直接決定模型能修什麼。要 cover 真實「業餘錄音 / 即興 demo」的劣化分布：

| Degradation 類型 | 對應的真實問題 | 實作方式 |
|----------------|--------------|---------|
| **尾音截斷 / fade rough** | 業餘錄音收尾不夠、唱完氣不穩 | random truncate 句尾 50-200ms + 加 amplitude noise |
| **換氣失真** | 麥克風太近、突發氣噪 | 在 voice activity 邊界注入 breath noise / pop |
| **MP3 / AAC 壓縮** | 手機錄 / 線上轉檔 | ffmpeg -b:a 64k/96k/128k 來回轉 |
| **低通 / 高頻截斷** | 廉價麥、藍芽收音 | sox lowpass 6k-12k random |
| **Vocal bleed 殘留** | Demucs 沒分乾淨 | 混入 -20 ~ -30 dB 的 instrumental |
| **Pitch drift / micro-detune** | 業餘音準小漂移 | Rubber Band ± 5-30 cents 隨機調變 |
| **Compression artifacts** | 過度 limiter | brick-wall limit + add quantization noise |
| **Reverb mismatch** | 房間共鳴錯 | 隨機 IR convolution + dry-wet 混合 |
| **DC offset / clipping** | 收音電平錯 | 加 DC + 局部 hard clip |
| **背景噪音** | 街頭 / 家裡冷氣 / 風 | 混入 DEMAND / MUSAN noise |

**關鍵設計**：degradation 要**隨機組合 + 隨機強度**，模型才能 generalize；不能只訓單一 degradation。

| 方法 | 描述 | 狀態 |
|------|------|------|
| **Diffusion-based vocal restoration**（自訓） | 從零訓 diffusion，conditioning 在 degraded waveform | 📋 長期目標 |
| **Fine-tune AudioLDM2 / Stable Audio** | 用 vocal-only 子集做 domain adaptation | 📋 較可行 |
| **Codec-domain restoration** | 在 EnCodec / DAC token 空間做修復，算力低 | 📋 ⭐ 待研究 |
| **Flow matching restoration** | F5-TTS 那套架構改成 restoration 任務 | 📋 待研究 |
| **Diff-aware conditional vocoder** | 給 vocoder 加 degradation embedding，學「反向化」 | 📋 待研究 |

**研究問題**：
- [ ] 訓練 vocal restoration diffusion 需要多少 hours of clean vocal（10h / 100h / 1000h）
- [ ] RTX 3070 (8GB VRAM) 能 fine-tune 哪個尺寸的 vocal diffusion
- [ ] codec-domain (EnCodec tokens) 修復對「保留歌手 identity」夠不夠精細
- [ ] degradation distribution 怎麼從真實「壞錄音」估出來（domain gap 是最大風險）
- [ ] 訓練時要不要 condition 在 phoneme / F0（給模型語意先驗）vs 純 waveform-to-waveform

### §17.4 評估方法

修音模型最難的是**評估**——音準對不對量得到，但「尾音美不美」量不到。

| 指標 / 方法 | 衡量什麼 | 狀態 |
|----------|---------|------|
| **A/B blind listening test** | 整體聽感（gold standard） | 📋 自己 + 找朋友 |
| **MOS (Mean Opinion Score)** | 5 級評分，傳統 SVS 評估 | 📋 待設計問卷 |
| **MCD (Mel Cepstral Distortion)** | mel-spectrogram 距離 | 📋 立刻可做 |
| **F0 RMSE / V/UV error** | 音準 / 有聲無聲一致性 | 📋 立刻可做 |
| **Speaker embedding similarity** | identity 是否保留（resemblyzer / WavLM） | 📋 ⭐ 高價值 |
| **PESQ / STOI** | speech 標準指標，對歌聲只能參考 | 📋 待測適用性 |
| **SingMOS / VocMOS** | 專為歌聲訓的 no-reference MOS predictor | 📋 待研究 |
| **細節指標**：vibrato 平滑度、attack/release 對齊、breath naturalness | 自己定義的領域指標 | 📋 ⭐ 要設計 |

**研究問題**：
- [ ] 怎麼設計能量化「尾音收乾淨」的指標（句尾 50ms 的 spectral envelope vs reference）
- [ ] 怎麼避免修音模型「太美 → identity 跑掉」（過度增強反而像別人在唱）
- [ ] 主觀 MOS 收集流程（10 個朋友 + Google Form？）

### §17.5 與其他章節的依賴

- 上游需要 **curated vocals dataset** —— 從本專案 pipeline 反向用 Demucs 抽 vocals stem 累積；資料品質清洗的 §15 規格目前在 StreetPerformerMaster RESEARCH_ROADMAP.md，但實質產出是本專案的職責，未來可考慮把 §15 也搬過來。
- 共用 **§16.3 的 diffusion 訓練基建**（兩者都是 audio restoration diffusion，模型架構可共享 codebase）
- 跟 **§11 DiffSinger / VISinger** 互補：§11 重生成、§17 重修補；長期可整合成「先修音、需要時 re-synth」

### §17.6 倫理 / 邊界

- ✅ 修自己的歌聲、保留自己的 identity → OK
- ✅ 修朋友 / 合作藝人錄音（明確授權）→ OK
- ⚠️ 「美化到不像本人」→ 需自我設定強度上限
- ❌ 在沒有授權下增強 / 修改他人歌聲（即使是 YT 公開素材）→ 不做

---

## 跟 StreetPerformerMaster 的章節對應

| 本檔章節 | 對應 StreetPerformerMaster 章節 |
|---|---|
| §11 SVS / Voice cloning | §5 人聲處理（傳統去噪 / 對 pitch）、§17.1 既有 vocal restoration models |
| §16.3 自訓音樂修復 | §16.1 既有音訊增強、§16.2 音樂專用方向、§16.4 快速折衷 |
| §17.2-§17.6 自訓 vocal restoration | §17.1 既有 vocal restoration model 評估 |

跨檔 cross-reference 都用「§ 編號」標示，不再保留 markdown link（兩個 repo 在不同硬碟，相對路徑不可靠）。
