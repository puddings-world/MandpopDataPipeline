# MandpopDataPipeline — 華語流行歌資料集建置

> 這份是給 Claude（或任何開發者）的上下文文件。

## 定位

這是一個**資料工廠**：負責下載、處理、標註華語流行歌曲，產出結構化的資料集供其他專案使用。

**本 repo 不做**：iOS app、錄音混音、影片製作。那些是消費者專案的事。

## 階段路線圖

> Phase 1 是「能用的 labeled dataset pipeline」；Phase 2 才開始做 audio quality 的精修研究。
> Phase 2 不會在 Phase 1 沒站穩前開工——資料量、對齊品質、可重現性必須先到位。

### Phase 1 — Dataset pipeline（**目前階段**）

目標：給一個歌名，自動產出進到 labeled dataset 的條目，每首歌都帶通過驗證的「instrumental + 對齊歌詞」。

```
歌名 → 找官方 MV → yt-dlp 抓音訊 → Demucs 人聲分離
     → 爬 LRC（網易雲 / QQ / YouTube subs）
     → alignment gate（VAD + WhisperX 驗證）
     → encode instrumental.m4a（消費者 app 撥放用）
     → 寫入 raw_dataset.jsonl，過 gate 後自動進 processed_dataset.jsonl
```

驗收標準：
- [x] `pipeline.py add` 一條歌全自動跑完六步
- [x] alignment gate 兩層：VAD + WhisperX，沒過不算 done
- [x] 失敗的歌標 `status=misaligned`，不會被 `export` 帶出
- [x] `instrumental.m4a` 自動產出（消費者 SPM 直接用）
- [x] 已 audit + auto-fix：152 / 157 進入 labeled（28 ok + 124 fixed）
- [ ] 5 首 misaligned 逐首 triage（其實很愛你、歐若拉、故事、想自由、慢慢喜歡你 — 真實 wrong-version / wrong-LRC）
- [ ] 全部 152 labeled 通過 Layer 2 WhisperX drift 驗證（卡在 numpy 2.x ↔ transformers import 衝突，要先解）

Phase 1 結束 = 整個資料集 alignment 全綠，並能持續自動納入新歌而保證每首都通過 gate。

### Phase 2 — Audio quality refinement（**未來研究**）

目標：在 Phase 1 累積的 stems 上做**精修 / 修復**研究，提升音質、補回壓縮損失、訓練專屬模型。

主題涵蓋（細節在 `RESEARCH_MODELING.md`）：
- §11 歌聲合成 / 修音（DiffSinger、RVC、So-VITS-SVC、F5-TTS 等）
- §16.3 自訓音樂修復 diffusion（給 instrumental 的 super-resolution / restoration）
- §17.2–§17.6 自訓 vocal restoration diffusion（給 vocals 的去壓縮、去 bleed、句尾修整）

Phase 2 啟動條件：Phase 1 至少累到 ≥ 1000 首已驗證的乾淨 stems，且 alignment gate 穩定。在那之前主要的工作仍然是擴量 + 補品質檢查。

## 消費者專案（producer–consumer 架構，規範在 README「Architecture: producer–consumer」章）

本 repo 是唯一 producer；所有 app 端都是唯讀 consumer，走四種標準介面之一。
**合約規則**：只有 pipeline 寫 `$DATASET_PATH`；consumer 只見 processed 子集；音訊不走 git；遠端一次拉一首絕不鏡像；consumer 端 schema 只增不刪；消費端產物（如錄音）要回流必須走新的 producer 進口 + gate，不得經 consumer 介面寫入。

| 介面 | 消費端 | 需要什麼 | 傳輸 |
|------|--------|---------|------|
| ① 同磁碟 | **StreetPerformerMaster**（iOS 街唱，`../StreetPerformerMaster/`） | instrumental.m4a + lyrics.lrc + setlist.jsonl（key 預設） | 兩 repo `.env` 指同一 `$DATASET_PATH` |
| ② HTTP 按需 | **Mac 錄音**（reprise-studio） | karaoke.flac（留和聲伴唱）/ instrumental.flac / vocals.flac / original.mp3 + lyrics.lrc，單首拉取 | `pipeline.py serve`（LAN、唯讀、網頁 UI + JSON API，spec 在 README） |
| ③ Python reader | 研究 / Phase 2 訓練 | processed 全部 + features.json + metrics.jsonl | `mandpop_dataset` module |
| ④ export bundle | 離線交付 / 除錯 | 依 request JSON | `pipeline.py export` |

repo 之間只靠資料契約和 CLI 工具相連，不 import 彼此的 code，所以不用 submodule。契約寫在提供方（本 repo README），依賴寫在使用方的文件裡；consumer 一律用 `DATASET_PATH` 找資料集。完整關係圖在 README「Related repositories」。**本 repo 不放跟資料集無關的工具**：通用的 YouTube 下載／剪輯／上傳在獨立 repo [ytclip](https://github.com/GeniusPudding/ytclip)（原本的 `ytgrab.py`、`tools/yt_clip/` 已經搬過去）。

## 兩層資料模型（規範在 README「Data model & format specifications」章）

**研究資料集是本專案的主體（華語流行經典歌研究）；表演 setlist 只是附加功能。**

| 層 | 角色 | 檔案 | 收錄標準 | 讀者 |
|----|------|------|---------|------|
| **研究資料集**（廣） | **主體** | `raw_dataset.jsonl` / `processed_dataset.jsonl` + 每首歌的 `metrics.jsonl`（YT 熱度時間序列）與 `features.json`（BPM/調性/段落等音樂屬性） | 任何華語流行歌，唱不唱無所謂，持續擴充 | 結構研究 |
| **表演歌單**（窄） | 附加 | `notes.txt`（Google Doc 鏡像、手動維護的 source of truth）→ `pipeline.py ingest` 產出 `setlist.jsonl` | 只收維護者會唱的歌，帶 key_offset / gender_variant / favorite + 伴唱(instrumental.m4a)/導唱(vocals.m4a) | StreetPerformerMaster |

- `notes.txt` 每行一首歌：**Doc 每一行都進 corpus 也進 setlist**（2026-10 起：Doc 就是表演者的歌單）；`±key`/`女版男版` 是預設值，`!`＝置頂。setlist.jsonl 依選單順序：`!` 置頂、其餘依歌手分組（拼音），`／歌手` 修正歌手。`notes_inbox.txt`（YouTube 擴充功能）沒標記＝純收歌。從 setlist 移除不影響 corpus。Google Doc 筆記格式規範在 README「Daily note-taking convention」節。
- Doc 同步：`pipeline.py ingest --from-doc`（追蹤的 Doc＝`NOTES_DOC_URL` 或 `inbox_config.json`）→ notes.txt → ingest → `INBOX_POST_INGEST_CMD`（SPM 手機同步；SPM 端 sublist 以 `source: doc|manual` 跟 Doc 對帳，Doc 刪掉的歌會從手機移除）。
- `seed_songs.txt` 是歷史混用，個人 curated 角色之後由 `notes.txt` 接手。
- 所有新格式（metrics.jsonl / features.json / notes.txt / setlist.jsonl / discovered_from provenance）欄位定義**以 README 該章為準**，實作時不要另創格式。
- 實作順序：① ingest（含導唱 m4a）→ ② metrics 時間序列 → ③ features → ④ discover 多來源+provenance。

**儲存政策**：compact 後每首約 75 MB；批次加歌後要跑 `compact`（保留 FLAC stems 給 Part 2）。**資料集 > 500 GB 或磁碟剩餘 < 300 GB 時主動提醒使用者討論加空間**，在那之前放心擴充（2026-07 現況：174 首 13 GB、剩 1.2 TB ≈ 還能收 15,000 首）。

## 資料集結構

兩個 JSONL index 撐起整個資料模型：

```
$DATASET_PATH/
├── raw_dataset.jsonl           爬過的全部歌（含對齊失敗、抓錯版本的）
├── processed_dataset.jsonl     通過 alignment gate 的子集（消費者只讀這個）
├── audit_report.jsonl          最近一次 audit 的細節報告
└── songs/{歌名}/
    ├── metadata.json           tracked
    ├── lyrics.lrc              tracked（行級時間軸，可能被 auto-shift 過）
    ├── lyrics.lrc.orig         tracked（auto-shift 前的原始 LRC，只有在被改動時存在）
    ├── lyrics.txt              tracked（純文本）
    ├── instrumental.m4a        .gitignored（消費者 app 撥放用 AAC 192k，~5MB）
    ├── instrumental.{wav,flac} .gitignored（active lossless master，compact 後變 .flac）
    ├── vocals.{wav,flac}       .gitignored（active，compact 後變 .flac，~40% 原大小）
    ├── original.mp3            .gitignored（compact 後會被刪除，可從 youtube_url 重抓）
    └── separations/            .gitignored（A/B 候選池，see `pipeline.py separate`）
        └── {method}/
            ├── vocals.{wav,flac}
            ├── instrumental.{wav,flac}
            └── meta.json       method / engine / model / runtime
```

`processed ⊂ raw`，always。每次 raw 有任何變動（新增歌、audit 改 verdict），都會自動 regenerate processed_dataset.jsonl。`is_processed(rec)` = `status == "done" AND alignment ∈ {ok, fixed}`。

## 主要指令

```bash
python pipeline.py add "告白氣球"                    # 加一首歌
python pipeline.py add "告白氣球" "小幸運" "演員"     # 多首
python pipeline.py add --from songs.txt              # 從檔案批次加
python pipeline.py add --from songs.txt --lyrics-only # 只爬歌詞
python pipeline.py discover --era all --add          # 從排行榜發掘歌曲
python pipeline.py lyrics                            # 補爬歌詞
python pipeline.py status                            # 統計
python pipeline.py list --missing-lyrics             # 列出缺歌詞的
python pipeline.py sync                              # 從磁碟同步 master list
python pipeline.py audit                             # Layer 1 alignment 檢查（VAD）
python pipeline.py audit --layer2                    # + WhisperX drift 檢查
python pipeline.py list --misaligned                 # 列出對齊壞掉的歌
python pipeline.py encode                            # 把 instrumental.wav 編成 .m4a 供消費者用
python pipeline.py enrich                            # 補 artist/lyricist/composer/album（LRC + YouTube title + API）
python pipeline.py compact                           # 縮磁碟：刪 original.mp3 + 把 wav stems 無損轉 FLAC（~70% 省）
python pipeline.py separate "歌" --method bs_roformer  # 跑替代分離器（不動 active stems），see "Separation A/B" 章節
python pipeline.py separate --all --method bs_roformer # 全資料集批次跑
python pipeline.py separate --list-methods           # 列出可用方法
python pipeline.py separate --status                 # 看每首歌已經跑過哪些方法
python pipeline.py separate --ab-page                # 產生 exports/ab_compare.html 本機試聽頁（同位置切換 / 盲測）
python pipeline.py separate --review --methods a,b,c # 只聽自動偵測的分歧熱點（6 秒循環、盲測投票）→ exports/ab_review.html
python pipeline.py export --request req.json --output ./out
python pipeline.py review                            # 快篩 UI：discover 進來的歌 ←不要 →要（http://127.0.0.1:8766）
python pipeline.py add --accepted                    # 批次處理快篩接受的歌（用試聽時那支影片）
python pipeline.py ingest                            # notes.txt（Google Doc 鏡像）→ corpus + setlist
python pipeline.py ingest --dry-run                  # 只解析報告，不寫任何東西
python pipeline.py ingest --from-doc                 # 抓追蹤中的 Google Doc → ingest → 手機同步 hook
python pipeline.py karaoke                           # 伴奏換成只去主唱、保留和聲的 karaoke 版（--revert 還原）
python pipeline.py queue pause|resume|status        # 暫停 / 恢復長批次（讓出 GPU）
python pipeline.py videos                            # 稽核每首歌的來源 YouTube 影片；明顯錯的（現場/翻唱/別人唱的）自動指定，其餘在 inbox ④ 歌庫影片 手動換
python pipeline.py serve                             # 唯讀 LAN server（Mac 瀏覽器按需拉歌，零安裝）
python pipeline.py metrics                           # 全 corpus 各 append 一筆 YT 熱度快照（每週跑）
python pipeline.py features                          # 算 BPM/調性/LUFS/段落 → features.json
```

## 歌詞來源（fallback 鏈）

1. **網易雲音樂 API**（有 LRC 時間軸，華語覆蓋最高）
2. **QQ 音樂 API**（有 LRC，補網易雲缺的，尤其周杰倫）
3. **YouTube 自動字幕**（有時間軸但品質差，最後手段）

## 技術棧

- **yt-dlp**：YouTube 下載
- **Demucs htdemucs_ft**：人聲分離（需要 NVIDIA GPU）
- **requests + BeautifulSoup**：歌詞爬蟲
- **WhisperX**：Layer 2 alignment drift 檢查（Whisper transcription + wav2vec2 word-level forced alignment，~100ms 精度。沒裝就 fallback 到 faster-whisper segment-level）
- **Python 3.10+**

## Raw vs Processed dataset

| 概念 | 檔案 | 包含什麼 | 消費者讀哪個 |
|------|------|---------|---------------|
| **Raw** | `raw_dataset.jsonl` | 每一首被 `add` 過的歌，含對齊失敗 / 抓錯版本 | ❌ 內部 |
| **Processed** | `processed_dataset.jsonl` | 通過 alignment gate 的子集 | ✅ |

升級條件（`pipeline.py:is_processed`，**全部都要**）：
- `status == "done"`（Demucs 跑完、歌詞抓到）
- `alignment ∈ {"ok", "fixed"}`（Layer 1 + Layer 2 都過；`fixed` 表示自動平移後再驗）
- `lyrics == "lrc"`（要有時間軸，純文本不算）
- `m4a == True`（`instrumental.m4a` 在磁碟上）

`fixable`／`drift`／`misaligned`／`wrong_version`／`no_data` 一律留在 raw。

`processed_dataset.jsonl` 每筆的 schema：
```json
{
  "title": "...", "artist": "...", "lyricist": "...", "composer": "...",
  "album": "...", "youtube_url": "...",
  "files": {
    "instrumental": "songs/{title}/instrumental.m4a",
    "lyrics": "songs/{title}/lyrics.lrc"
  }
}
```
不含內部 audit 指標——那些只進 `raw_dataset.jsonl` 跟 `audit_report.jsonl`。

`pipeline.py export` 預設只匯 processed。要強制帶 raw 必須在 request JSON 加 `"include_raw": true`（除錯用，**不能給消費者**）。

`add`、`audit`、`sync` 結束自動 regenerate `processed_dataset.jsonl`。

## 對齊保證（alignment gate）

每首歌進來都要通過 alignment 檢查才能升上 labeled。流程在 `processors/alignment.py`：

- **Layer 1（每首幾秒，預設開）**：vocals.wav 上 RMS-VAD 找第一句人聲時間點，跟 LRC 第一行歌詞時間做差（並過濾掉 `作词/作曲/编曲/制作人/吉他/Production Coordination/Recorded` 等 Chinese + English 製作信用 metadata）。`|offset| ≤ 0.5s` → `ok`（直接 labeled）；`0.5–30s` → 自動把整個 LRC 平移修正（原檔備份成 `lyrics.lrc.orig`），重新驗證後標 `fixed`（labeled）；`> 30s` → `misaligned`（留在 raw，不進 labeled）。
- **Layer 1.5（逐句重新對時，預設開）**：`processors/lrc_align.py` 用 DP 把每句 LRC 對到 vocals 的起唱點（間隔比例懲罰＋全域 a·t+b 先驗），修「越唱字幕越落後」（加速版上傳、MV 多前奏/間奏）。只在明顯更好時才改寫（`lyrics.lrc.orig` 備份），標 `fixed`。手機同步的歌詞以 md5 比對（重新對時常常不改檔案大小）。
- **Layer 2（每首幾分鐘，要 `--layer2`）**：WhisperX（Whisper transcription + wav2vec2 word-level forced alignment，~100ms 精度）跑 vocals.wav，逐行 fuzzy match 比對 LRC 並算累積 drift。`mean |drift| > 1s` 或 `std > 1.5s` → `drift`；match rate < 20% → `wrong_version`（→ `misaligned`）。沒裝 whisperx 就 fallback 到 faster-whisper segment-level，再沒裝就 skip。
- **`pipeline.py add` 第 5/6 步自動跑 Layer 1**（第 6/6 步是 encode m4a）。新歌沒通過 Layer 1 就不會被 export。
- **`pipeline.py audit`** 對全部資料集重跑檢查並產出 `audit_report.jsonl`。

加新爬法、改 LRC 來源、改 Demucs 模型後，務必重跑 `audit --layer2` 確認沒回退。

## Separation A/B（評估替代分離器）

Phase 1 active stems 是 `htdemucs_ft`（2022）。要嘗試更新的分離器（BS-RoFormer、Mel-Band RoFormer、MDX23C）但不能影響已經在用 dataset 的消費者(SPM)——所以候選 stems 走非破壞性流程：

```
songs/{title}/
├── instrumental.wav        ← active（SPM 讀的）
├── vocals.wav              ← active
└── separations/
    ├── bs_roformer/
    │   ├── vocals.flac
    │   ├── instrumental.flac
    │   └── meta.json
    └── mel_band_roformer/
        └── ...
```

指令:
- `pipeline.py separate <title> --method bs_roformer` — 跑單首
- `pipeline.py separate --all --method bs_roformer` — 批次背景跑
- `pipeline.py separate --list-methods` — 列出方法（`bs_roformer` / `mel_band_roformer` / `mdx23c`，背後是 audio-separator 套件處理 UVR 權重下載）
- `pipeline.py separate --status` — 每首歌目前已經跑過哪些方法的矩陣表

候選 stems 預設用 `flac`（lossless 但 ~50% wav 大小，A/B 比較不需要再 compact）。Source mix 優先用 `original.mp3`；如果已 compact 掉就從 `metadata.json[youtube_url]` 重抓臨時檔，跑完刪掉——**不會**把現有 vocals+instrumental 加回去當輸入(會把舊分離器的 artifact 帶進新一輪)。

`htdemucs_ft` 是 active baseline，直接讀 `songs/{title}/{vocals,instrumental}.{wav,flac}`，不會在 `separations/` 重複一份。

audio-separator 需要 numpy 2.x（會打壞主環境的 transformers/WhisperX），所以裝在隔離的 `tools/venvs/separator/`（`--system-site-packages` 共用 torch，本地覆蓋 numpy/numba），由 `processors/_separator_worker.py` 以 subprocess 呼叫；`setup_env.py` 會建立。權重快取在 `tools/models/audio-separator/`。GitHub Releases 下載極慢時，從 HF 鏡像用同檔名預先放入並比對 sha256。Demucs path 不受影響。

Karaoke 方法（`karaoke_aufr33` / `karaoke_gabox_v2` / `karaoke_becruily`）只去主唱、**保留和聲**在 instrumental 裡；`inst_becruily` 是全部人聲去除的對照組。

**Promotion**：盲測贏家 `karaoke_becruily`。`pipeline.py karaoke` 把 instrumental.m4a / 導唱 vocals.m4a 換成 karaoke 版（舊檔備份在 `separations/htdemucs_ft/`，`--revert` 還原）；根目錄 lossless stems 仍是 htdemucs_ft（對齊用）。長批次可 `pipeline.py queue pause|resume`（每首之間檢查）；所有指令預設低優先權＋執行緒上限（`processors/resources.py`）。

## 與消費者專案的介面

兩個 repo 透過 `.env` 的 `DATASET_PATH` 指向同一個資料夾。消費者不需要 clone 本 repo，只要讀 `D:\MandpopDataset\` 裡的資料。

## 模型研究（Phase 2）

長期建模研究見 `RESEARCH_MODELING.md`，對應上面的 Phase 2：歌聲合成 / 修音、音樂修復 diffusion、vocal restoration diffusion。從 StreetPerformerMaster 的 `RESEARCH_LONG_TERM.md` 搬過來——那邊是消費者專案（街頭播放 / 翻唱錄音），本專案是資料層 + 未來模型層。

Phase 2 還沒啟動，目前所有開發精力都在 Phase 1（擴大資料量 + 強化 alignment gate）。要做 Phase 2 之前先讀 `RESEARCH_MODELING.md` 了解資料量門檻。

## Git 規範

- **commit 訊息不加 Co-Authored-By**。不要加任何 AI co-author 標記。
- commit message 用英文，簡潔描述改了什麼。
- 音訊檔（*.wav, *.mp3, *.m4a）永遠不進 git。
