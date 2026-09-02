# 除外責任「行為人區分」demo 設計與跑法

> [!NOTE]
> 這是原作者 demo 設計與 macOS / Bash 歷史跑法。Windows 已實際進行 actor-distinction demo 到 candidate staging／procedural review，並確認 `eval` active 與 `default` pending 是不同 tenant 的預期狀態；尚未確認 production approve 與完整回歸結果。2026-09-01 已移除所有雲端 API key，目前無法繼續需要 Gemini 的步驟。Windows 通用流程見 [knowledge-distillation-windows.md](knowledge-distillation-windows.md)，日期化狀態見 [current-windows-status.md](current-windows-status.md)。

## 0. 這份文件在做什麼

設計一組誤判案例，示範知識蒸餾閉環（[knowledge-distillation-plan.md](knowledge-distillation-plan.md)）完整跑一輪，**且完整走過 P5 之後的 episodic 審核關卡**：

> 真的誤判（不是手填正確答案）→ episodic 落地為 `pending` → 人審核（`review_episodic.py`）改正 → `active` → 蒸餾 → 驗證候選規則真的修好這兩筆 → 人審核（`review_memory.py`）→ `active` → 其中一筆升格成 regression case → procedural 規則注入 agent 後，連從沒見過的第三筆（holdout）也判對

這是 2026-08-11 原始版本的重新設計。原始版本用 `scripts/seed_exclusion_episodic_examples.py` 手動寫入**已知正確**的 episodic 內容（等於跳過了「agent 真的判錯過」這一步），而且原始版本的 §3.1 依賴一個現在已經不存在的機制（episodic few-shot 洩漏）。[knowledge-distillation-plan.md P5](knowledge-distillation-plan.md) 把 episodic 整個從 agent 可見的記憶裡拿掉、改成蒸餾器專用的原料之後，這個 demo 剛好是驗證 P5 那條審核鏈（`scripts/review_episodic.py`）第一次被真實使用的機會——不是重新設計一個新場景，是讓案例設計本身（§1/§2，沒有變）走一條更真實、也更能展示 P5 價值的路徑。

案例設計、盲點分析（§1/§2）維持原版不變。§3 保留原版已驗證過、現在仍然成立的發現（ceiling effect、蒸餾器不穩定性），移除依賴已移除機制的部分。§4 是重寫過的跑法。

## 1. 鎖定的盲點

原作者 2026-08-11 跑這個 demo 時，`default/procedural/stt_exclusion_notify/check` 唯一 active 的規則是 `pending-e9b8205f`；2026-08-31 Windows 開始重跑 demo 前沒有 procedural，但後續 review 已產生 candidate 資料，所以下列內容只代表歷史起點，不是目前資料庫聲明：

> 判斷除外責任時，必須明確區分保險給付項目（如長期照顧保險金 vs. 意外二至三級失能保險金），因為不同項目的除外規定可能完全不同。

這條規則教的是**跨條文**的區分（第二十七條 vs 第二十九條）。但第二十九條**內部**還有一層更細的但書，這條規則完全沒教到：

> 一、要保人、被保險人的故意行為。（除外原因之一）
> （...）
> 前項第一款情形（**除被保險人的故意行為外**），致被保險人成第二至三級失能程度之一時，本公司仍按第十六條及第十七條的約定給付各項保險金。

同一款「故意行為」，**要保人**做的 → 但書救回來、仍賠；**被保險人自己**做的 → 但書明文排除、不賠。這是同一條文、同一給付項目內部的行為人區分，跟現有規則講的「不同給付項目」是不同軸線——現有規則對這個盲點沒有幫助，這正是選這個盲點當 demo 素材的原因：production 的單一 active 規則完全不覆蓋它。

## 2. 兩個誤判案例設計

兩案**邏輯完全相同**（要保人的故意行為 → 不影響意外二至三級失能保險金給付），只換場景措辭，刻意不做真假對照——demo 優先求好懂，不要求觀眾同時消化兩種結論。

| id | 逐字稿 | 正確答案 involves_exclusion | matched_articles | 音檔 |
|---|---|---|---|---|
| `seed-policyholder_assault_disability23` | 我朋友的保單要保人是他先生，被保險人是他自己，去年他先生跟他吵架失控打傷他的背，害他脊椎受損被鑑定到第二級失能，這張保單的意外二至三級失能保險金賠不賠 | **false** | 第二十九條 | `samples/gen_policy_03.wav` |
| `seed-policyholder_family_assault_disability3` | 我表姐的保單要保人是她媽媽，之前她們家因為遺產糾紛，她媽媽情緒失控拿東西砸傷她的脊椎，害她被鑑定到第三級失能，這個意外二至三級失能保險金賠不賠 | **false** | 第二十九條 | `samples/gen_policy_04.wav` |

音檔用 `say -v Meijia` 產生、`afconvert -f WAVE -d LEI16@22050 -c 1` 轉成跟現有 `samples/gen_policy_01/02.wav` 一致的格式（mono 22050Hz Int16 WAVE），內容已用真實 `services.stt.client.transcribe()`（breeze-asr）轉錄驗證過無誤。這兩筆案例的逐字稿記錄同步寫進 [exclusion-episodic-cases.md](exclusion-episodic-cases.md)。

**「正確答案」欄位在這個 demo 裡的角色跟以前不一樣**：以前是直接寫進 DB 的內容；現在是**審核者手上的答案卡**——`scripts/seed_actor_distinction_demo.py`（§4 步驟 1）呼叫真實 `judge_exclusion()`，把模型當下實際吐出的（很可能是錯的）判斷寫進 episodic；審核者對照這張表的正確答案，用 `scripts/review_episodic.py` 的 `e`（edit）把 `output` 改成對的，再 approve。

**已知取捨**（跟使用者確認過，接受）：這兩案都只示範「要保人做的不算除外」，語料裡沒有「被保險人自己故意行為 → 這項給付真的排除」的對照案例。`distill_procedural.py` 有可能因此把規則寫得比條文本身更寬（例如寫成「故意行為都不算除外」，漏掉「除非是被保險人自己做的」這個限定）。人審核（`review_memory.py`）時要留意這一點；demo 目的優先簡化，之後要補嚴謹度可以再加一則被保險人自己故意行為的對照案例（`docs/exclusion-episodic-cases.md` 的「為什麼要加後三筆」是同類補強的先例）。

### 2.1 Holdout 測試逐字稿（第三筆，刻意不種進 episodic）

同一條「要保人 vs 被保險人」邏輯，但**不寫進 episodic 語料**——用來驗證「procedural 規則本身有沒有貢獻」，跟 §2 兩筆刻意分開：

> 這張保單要保人是我哥哥，被保險人是我，前陣子他因為欠錢的事情跟我起爭執動手推我，我摔下樓梯導致下肢神經受損被評估到第二級失能，這個意外二至三級失能保險金賠不賠

expected `involves_exclusion: false`，matched `第二十九條`。

**為什麼不能放進 `exclusion-episodic-cases.md`**：那份文件自己的規則是「episodic 語料不能跟 eval/holdout 逐字稿共用，混用會讓評測分不清模型是真的學會還是背過那句話」。這句 holdout 從設計上就是要留在 episodic 語料**外面**的對照，才能拿來測「還沒種過這個 pattern 的新案例，procedural 規則能不能讓 gemini-cheap 答對」。§4 最後一步驗證通過的話，這句就是要貼進 `evals/check_cases.yaml` 的 `split: regression` 候選之一。

## 3. 背景：已驗證過、現在仍然成立的發現

這一節記錄原始版本 demo（P5 之前）已經實測過、且**不受 P5 影響**的發現——理由跟細節不重複貼，只講結論跟為什麼還算數。

### 3.1 production 模型測不出東西，demo 必須用 gemini-cheap

用 `seed-policyholder_assault_disability23` 的逐字稿做過歷史比較：`gemini-strong` 對這個案例 5/5 正確——模型本身讀條文夠仔細，這個盲點對它不構成誤判，是跟 `evals/check_cases.yaml` 的 `seed-drunk_ride_ltc` 同一種 ceiling effect。`gemini-cheap` 在無記憶 baseline 下 0/5 全錯，加上當時的 `pending-e9b8205f` 規則後仍是 0/5（規則答非所問）。手寫一條「行為人區分」規則診斷性地疊上去，5/5 全對——證明記憶機制本身有效，缺的只是對的規則內容，不是模型能力上限。**這個 demo 必須明確指定 `gemini-cheap`，才能重現當時要展示的盲點。** `scripts/seed_actor_distinction_demo.py`（§4）已經內建這個 model override；目前 workflow 本身也使用 `gemini-cheap`，但 demo 仍保留明確 override 以避免設定漂移。

### 3.2（原 §3.1，**已過期，因 P5 結構性移除**）episodic few-shot 洩漏

原始版本這裡記錄過「episodic 種下去之後，`recall_episodic_few_shot(limit=3)` 會單獨造成改善，procedural 規則的貢獻要另外用乾淨的 episodic 快照隔離測」。[knowledge-distillation-plan.md P5](knowledge-distillation-plan.md) 把 `recall_episodic_few_shot()` 整個函式連同四個呼叫端都移除了，`judge_exclusion()` 現在只會讀 procedural（透過 `inject_procedural()`），完全不讀 episodic。**這代表這整節描述的風險已經不存在，也代表 §4 的驗證步驟不再需要「排除洩漏案例」這道手續**——`tenant=default` 跟 `tenant=eval` 現在的差異只在 procedural 內容，不用再另外控制 episodic 快照。這是這次重新設計順帶簡化掉的一個複雜度來源。

### 3.3 `stage_candidate_for_eval.py` 曾經有一個鏡射順序 bug（已修好，且現在整段機制都不需要了）

審核候選規則時 evidence diagnostic 曾經顯示錯誤結果，查出是 `stage()` 鏡射 episodic 到 `eval` tenant 時順序被反轉，讓 few-shot 視窗裝進不相關的舊案例。這個 bug 修好過（`reversed()`），但 P5 把 episodic 鏡射整段邏輯都移除了（`stage_candidate_for_eval.py` 現在只鏡射 procedural）——這個 bug 存在過的機制已經不在代碼裡，記錄純粹是歷史脈絡，不影響這次 demo。

### 3.4 蒸餾器不是每次都寫出一樣有效的規則——這正是人審核那關存在的理由

這套流程完整跑過一輪（案例語料相同）時，`distill_procedural.py` 兩輪讀**完全相同**的語料，寫出**不同的規則文字**：

| 輪次 | 規則文字 | 隔離測試 pass rate（gemini-cheap） |
|---|---|---|
| 有效版本 | 判斷「故意行為」除外責任時，必須確認行為人身份，若除外條款包含「但書」規定（如：除被保險人之故意行為外），則要保人或其他人的故意行為不適用於該除外責任。 | **0/5 → 5/5** |
| 無效版本 | 判斷「故意行為」的除外責任時，必須明確釐清該行為是由「要保人」還是「被保險人」所為，因條款通常設有保護被保險人之但書。 | **0/5 → 0/5**（完全沒用） |

無效版本只叫模型「去釐清是誰做的」，沒有像有效版本明講「查清楚之後該怎麼判」——對 `gemini-cheap` 來說這個間接指引不夠。這是這次重跑最需要留意的地方：**如果 `distill_procedural.py` 寫出的候選規則讀起來像無效版本那樣（只講「要查清楚」不講「查完之後怎麼判」），先別急著 approve**，用 `review_memory.py` 的 `e`（edit）功能把措辭改成有效版本那種「查完之後直接講結論」的寫法，這正是人審核（連同底下真的跑一次評測對照）存在的理由——光看規則文字合不合理看不出來，必須實測。

## 4. Demo 跑法（重新設計版，每一步都是你自己在終端機跑的指令）

> [!IMPORTANT]
> 以下是原作者的 macOS / Bash 指令，保留原有內容。Windows 不要直接貼到 PowerShell；先依 [knowledge-distillation-windows.md](knowledge-distillation-windows.md) 設定 repository、Python 與必要服務，再把每個 `uv run python` 對應成 `.\.venv\Scripts\python.exe`。本 demo 會寫入與刪除記憶資料，正式執行前仍需另行確認。

以下原作者指令都假設 `uv run honcho start` 已經在跑。凡是需要互動輸入的步驟（3、5）會列出建議怎麼回答，但決定權在你。

**步驟 0：確認乾淨起點**（如果不是第一次跑，先重置）

```bash
uv run python -m scripts.reset_exclusion_actor_demo --dry-run   # 確認會刪什麼
uv run python -m scripts.reset_exclusion_actor_demo             # 真的清掉上一輪殘留
```

**步驟 1：讓 agent 真的判錯這兩筆案例，落地成 `pending` episodic**

```bash
uv run python -m scripts.seed_actor_distinction_demo
```

這支腳本用 `gemini-cheap` 實際呼叫 `judge_exclusion()`（§3.1 的理由），把模型當下真正吐出的判斷（預期是錯的：`involves_exclusion=true`）寫進 `default/episodic/stt_exclusion_notify/check`，`status="pending"`。輸出裡的 `WRONG`/`correct` 標記只是提示，不是斷言——模型偶爾可能剛好答對，那也是真實訊號，不用重跑。

**步驟 2（可選，確認 pending 真的看不到）**：這時 `judge_exclusion()` 完全讀不到這兩筆——P5 拔掉了 episodic few-shot，而且就算沒拔，`pending` 狀態本來就對 `recall()` 不可見。可以跳過，直接進步驟 3。

**步驟 3：人審核 episodic——對照 §2 的正確答案表，改正再核准**

```bash
uv run python -m scripts.review_episodic --scope stt_exclusion_notify/check
```

對每一筆：印出 `input`/`output`（模型原始判斷）。對照 §2 表格的正確答案：
- 如果 `output` 是錯的（`involves_exclusion` 不是 `false`，或條文/理由明顯答非所問）：選 `e`，貼入正確的 JSON（例如 `{"involves_exclusion": false, "matched_articles": ["第二十九條"], "reason": "..."}`，理由可以參考 §2 表格描述自己重寫，不用逐字照抄），再選 `a` 核准。
- 如果模型剛好答對：直接 `a` 核准即可。

兩筆都跑完後，`default/episodic/stt_exclusion_notify/check` 應該有這兩筆 `status="active"`、內容正確的案例。

**步驟 4：蒸餾**

```bash
uv run python -m scripts.distill_procedural --scope stt_exclusion_notify/check
```

蒸餾器會讀這個 scope 下**所有當下存在的** active episodic 與 active procedural，嘗試歸納出新規則。原作者 demo 當時另有 `seed_exclusion_episodic_examples.py` 的 6 筆基礎語料與 `pending-e9b8205f`；Windows 重跑時不能假設這些資料仍存在，必須先查 DB。記下指令實際印出的 `pending-<uuid>` key。

**步驟 5：人審核候選規則——確認規則文字有講「怎麼判」，不是只講「要查什麼」**

```bash
uv run python -m scripts.review_memory --scope stt_exclusion_notify/check --model gemini-cheap --key <步驟4印出的key>
```

會印出 `evals/check_cases.yaml` 的 baseline/candidate 對照表，以及這兩筆案例各自的 evidence diagnostic（重跑判斷，看 candidate 有沒有真的把 `involves_exclusion` 從 true 改判成 false）。對照 §3.4 的兩個版本範例：
- evidence diagnostic 顯示兩筆都從 `[True, ...]` 變成 `[False, ...]`（真的修好了）→ `a` approve。
- 規則文字讀起來像「只叫模型去查清楚」但沒講「查完後怎麼判」（§3.4 無效版本那種），或 evidence diagnostic 顯示沒有變化 → 用 `e` 照 §3.4「有效版本」的寫法重打規則文字，看到 diagnostic 變化後再 `a`。

approve 之後終端機會印出兩筆 `split: regression` 的 YAML 建議格式。

**步驟 6：挑其中一筆貼進 regression 測試集**

從步驟 5 印出的兩筆建議裡挑一筆（例如 `seed-policyholder_assault_disability23`），手動貼進 [evals/check_cases.yaml](../evals/check_cases.yaml)，`expected` 欄位照 §2 表格填 `involves_exclusion: false`，`split: regression`。

**步驟 7：驗證第三筆（holdout，從沒進過 episodic 語料）現在也判對**

這是整個 demo 要證明的最終結論：procedural 規則本身有貢獻，不是死記兩筆案例。用 §2.1 的 holdout 逐字稿，比較「只有 `e9b8205f`」vs「`e9b8205f` + 新核准規則」——可以直接跑：

```bash
uv run python -m evals.run_eval --repeats 5 --model gemini-cheap
```

`--model gemini-cheap` 應明確保留，確保重跑仍使用這個 demo 要測的弱模型；若未指定，`evals/run_eval.py` 會跟隨目前 workflow YAML，而該設定未來可能改變。歷史比較中的 `gemini-strong` 在這個盲點上有 ceiling effect（§3.1），看不出「有沒有規則」的差異。如果步驟 6 已經把 holdout 貼進 `check_cases.yaml`，這支指令會直接把它跑進去、印出通過率；沒貼的話，把 §2.1 的逐字稿暫時加進 `evals/check_cases.yaml`（`split: holdout`）再跑，或參考 `scripts/review_memory.py::_load_evidence_cases`/`_run_case` 的寫法手動組一個一次性腳本呼叫 `judge_exclusion(..., tenant="default")` 五次數 pass rate。歷史預期是新規則核准前誤判 `true`、核准後判對 `false`；目前 Windows 尚未重跑，不能直接沿用這個結果。

## 5. Demo 跑完後如何重置 DB

Demo 是要重複跑給人看的，所以跑完一輪（不管進行到哪一步）要能把 DB 復原到 §2 案例還沒種進去之前。`scripts/reset_exclusion_actor_demo.py` 負責這件事：

- 刪除 §2 兩筆 episodic seed（`default` tenant；P5 之後 `eval` tenant不再有 episodic 鏡射，那個分支現在永遠是 no-op，留著只是防禦性寫法，不影響行為）
- 掃描 `default/procedural/stt_exclusion_notify/check`（不分 `pending`/`active` 狀態），刪掉任何 `evidence` 欄位包含這兩筆 case key 的規則——不管蒸餾器產生的候選最後有沒有被核准成 active，都會被抓到
- 整個清空 `eval/procedural` 跟 `eval/episodic` 這個 scope（重用 `stage_candidate_for_eval.py` 的 `_wipe()`）

**不會處理的部分**：如果 §4 步驟 6 真的把案例加進 `evals/check_cases.yaml`，那是 git 追蹤的檔案異動，不是 DB 狀態，reset 腳本刻意不碰。跑完不想留下的話，未 commit 前用 `git checkout -- evals/check_cases.yaml` 手動復原。

跑法：

```bash
uv run python -m scripts.reset_exclusion_actor_demo [--dry-run]
```

`--dry-run` 只列出會刪什麼，不真的刪，跑正式 demo 前建議先跑一次確認範圍。
