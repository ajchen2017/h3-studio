# H3 Studio — Claude 行為準則

MiniMax H3 影片生成控制台。Backend: FastAPI port 8790（`backend/main.py`，同時用
StaticFiles 直接 serve `frontend/index.html`，沒有前端 build step）。ComfyUI 跑在
8188，跟 minimax music 專案**共用同一個 ComfyUI 實例**（模型檔案在 F 槽 HDD，透過
`extra_model_paths.yaml` 指到 `F:\ComfyUI\models`）。

## 已知陷阱

**ComfyUI 記憶體洩漏（2026-08-24 已修）**：async weight offloading / pinned
memory 這兩個 ComfyUI 內建功能（預設開啟）會在多次生成之間洩漏記憶體，兩次分別從
27GB 長到 51GB，最後把 Windows commit charge 打到 92-99%，導致
`HostBuffer.read_file_slice failed` 崩潰。已在 `start_comfyui.bat` 加
`--disable-async-offload --disable-pinned-memory` 關掉；`backend/comfy_client.py`
的 `ensure_comfyui_memory_healthy()` 也會在每次生成前檢查 ComfyUI 行程的
commit 記憶體（`psutil.Process.memory_info().pagefile`），超過 15GB 就自動
重啟 ComfyUI 再繼續，當作雙重保險。找 ComfyUI 行程時路徑要用
`.lower()` 比對——這個資料夾在磁碟上有時是 `ComfyUI` 有時是 `comfyUI`，大小寫
不一致。

**MiniMax H3 的 prompt 格式，官方規格是這樣（`backend/prompt_rewriter.py`）**：
- 官方文件：[VIDEO_PROMPT_WRITING_GUIDE_ref_en.md](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md)
- **六個段落**，不是五個：`subject_definitions` → `summary` → **`retention_analysis`**
  → `detailed_description` → `overall_soundscape` → `non_diegetic_music`。
  h3-studio 原本的模板漏掉 `retention_analysis`——這段是告訴模型「參考圖片要怎麼
  被保留/轉換」，沒有這段時，模型碰到參考圖片會直接整臉照抄、把其他場景細節全部
  丟掉（實測驗證過：同一個 prompt 有參考圖沒 retention_analysis → 生成變成單純
  的人像對嘴影片，場景、道具完全消失）。
- **語言規則，官方白紙黑字寫明**：六個段落全部要用**英文**寫，只有 `<d>[語言]
  對白</d>` 裡的對白/歌詞，跟畫面裡真的出現的文字，才保留原語言。實測驗證：同一個
  中文 prompt（無參考圖）生成結果場景完全跑掉；換成英文版，場景、道具、運鏡全部
  精準命中。**中文長篇場景描述目前這個模型處理不好，不是 app 的 bug。**
- `[Shot N]` 分鏡格式：第一個 shot 不用時間戳，後面的要用
  `[Shot N] At MM:SS.mmm,` 開頭。
- 運鏡指示寫進每個 shot 自己的句子裡，不要獨立成一段。

**因此 `/api/generate` 現在會自動把送出的 prompt（不管中文英文、不管有沒有
retention_analysis）丟給 Claude Haiku 重寫成官方格式**（`prompt_rewriter.py` 的
`rewrite_prompt()`），跟 minimax music 專案用 Haiku 做翻譯是同一套邏輯
（`ANTHROPIC_API_KEY` 放在 h3-studio 自己的 `backend/.env`，跟 music 專案的
key 是同一把，但各自獨立存放）。

**Haiku 這支 SDK（`anthropic==1.0.0`）沒有 `temperature` 參數**——`messages.create()`
的簽名裡完全沒有 sampling 相關的 kwarg（只有 `output_config={effort, format}`），
跟一般認知的 Anthropic API 不一樣，呼叫時傳 `temperature=` 會直接丟
`TypeError`。這代表沒辦法把這個改寫任務的隨機性調到最低——實測過同一組輸入，
**Haiku 偶爾會完全無視輸入內容、自己編一個不相干的畫面**（例如麵包店場景憑空
變成「山中武術人物展開卷軸」）。目前唯一的緩解方式是在 `prompt_rewriter.py` 的
system prompt 裡加了強力的「不准發明內容」指令，測試後大幅改善但無法保證
100% 不發生——**檢查生成結果時要留意內容是不是真的對應到原始輸入**，不能無條件
信任 Haiku 的改寫結果。

## 檔案地圖

- `backend/workflow_builder.py` + `backend/template_ref2va_api.json`：組 ComfyUI
  workflow 的節點圖，`prompt` 參數就是純文字（不管是不是已經被 Haiku 重寫過都吃）
- `backend/prompt_rewriter.py`：Chinese/不完整格式 → 官方英文六段格式
- `backend/comfy_client.py`：`ensure_comfyui_memory_healthy()`、
  `queue_via_prompt_api()`、`check_comfyui_alive()`
- `backend/postprod.py`：後製分頁（3 軌時間軸 + HunyuanVideo-Foley SFX），獨立
  APIRouter，跟主生成流程共用同一個 ComfyUI
- `backend/auth.py`：單一 admin 密碼登入，stdlib PBKDF2 + HS256 JWT
