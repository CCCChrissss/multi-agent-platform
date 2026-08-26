# exclusion 情境的 episodic 種子案例清單

> [!NOTE]
> 本文件保留原作者的種子資料對照。資料與腳本仍在 repository，但目前 Windows 文件階段未重新執行 seed / distillation 流程；現行狀態見 [current-windows-status.md](current-windows-status.md)。

`scripts/seed_exclusion_episodic_examples.py` 寫進 `default/episodic/stt_exclusion_notify/check` 的 6 筆案例——這是
`scripts/distill_procedural.py`（P2 蒸餾器）唯一的輸入來源。案例的 `key` 看不出逐字稿內容，這份表補上這一層，蒸餾出新規則、
或要加案例時先看這裡。

所有案例都用真實 `judge_exclusion()` 服務驗證過（見各筆備註的日期），不是手寫猜測。

| id | 逐字稿 | involves_exclusion | matched_articles | 備註 |
|---|---|---|---|---|
| `seed-criminal_robbery` | 他之前因為搶劫銀行被警察開槍打傷腦部，現在完全失去自理能力需要人24小時照顧，這個長照險賠不賠 | true | 第二十七條 | 犯罪行為 + 長照請求 → 除外（第二十七條本就列犯罪行為） |
| `seed-claims_payout_timing` | 申請理賠金核准之後大概要多久時間才能撥款到我的帳戶 | false | 第十條 | 純行政程序問題，與除外責任無關 |
| `seed-drug_driving_amputation` | 他吸毒後開車撞上護欄，雙腿在意外中完全截肢，這個意外險賠不賠 | true | 第二十七條、第二十九條 | 施用毒品同時列在 27（長照/豁免）跟 29（意外失能），兩種給付都排除 |
| `seed-drunk_ride_ltc`（2026-08-11 新增） | 我朋友他去年喝酒之後騎機車摔倒，頸椎受傷，現在生活起居都要人照顧，這張保單的長期照顧保險金賠不賠 | **false** | 第二十七條、第二十九條 | 反例：酒駕只列在第二十九條（意外失能），第二十七條（長照）沒有酒駕這項，所以長照請求不除外。跟 `evals/check_cases.yaml` 的 `drunk_driving_bike` 是同一個道理，但用不同措辭/情境，避免評測案例被當成 few-shot 汙染測量 |
| `seed-drunk_driving_grade23`（2026-08-11 新增） | 他喝酒之後開車撞斷護欄，脊椎受傷評估到第二級失能，這個意外二至三級失能保險金賠不賠 | true | 第二十九條 | 跟上一筆的對照組：同樣是酒駕，但請求的給付換成意外二至三級失能（16/17），這次真的除外——目的是讓蒸餾器看到「同一原因、不同給付、不同結論」這組對比，不要只歸納出「違法行為→除外」 |
| `seed-premium_grace_period`（2026-08-11 新增） | 如果忘記繳保費有幾天的寬限期可以補繳 | false | （無） | 第二筆單純行政問題，跟 `seed-claims_payout_timing` 同類但不重複，避免這種案型在語料裡只出現一次 |
| `seed-policyholder_assault_disability23`（設計於 2026-08-11，**尚未種進 DB**） | 我朋友的保單要保人是他先生，被保險人是他自己，去年他先生跟他吵架失控打傷他的背，害他脊椎受損被鑑定到第二級失能，這張保單的意外二至三級失能保險金賠不賠 | **false** | 第二十九條 | 音檔 `samples/gen_policy_03.wav`（`say -v Meijia` 產生，跟 `gen_policy_01/02.wav` 同做法；真實 `services.stt.client.transcribe()` 轉錄驗證過內容無誤）。是**要保人**（先生）的故意行為，不是被保險人自己；第二十九條末段但書「前項第一款情形（除被保險人的故意行為外）...仍按第十六條及第十七條的約定給付」救回這筆給付。設計細節見 [exclusion-actor-distinction-demo.md](exclusion-actor-distinction-demo.md) |
| `seed-policyholder_family_assault_disability3`（設計於 2026-08-11，**尚未種進 DB**） | 我表姐的保單要保人是她媽媽，之前她們家因為遺產糾紛，她媽媽情緒失控拿東西砸傷她的脊椎，害她被鑑定到第三級失能，這個意外二至三級失能保險金賠不賠 | **false** | 第二十九條 | 音檔 `samples/gen_policy_04.wav`，同上做法與驗證。跟上一筆邏輯完全相同（要保人故意行為 → 不影響意外二至三級失能給付），只換場景/關係/失能等級，刻意不做真假對照——demo 用途優先求好懂，取捨說明見 [exclusion-actor-distinction-demo.md](exclusion-actor-distinction-demo.md) §2 |

## 為什麼要加後三筆

P2 蒸餾器（2026-08-10）讀了前 3 筆種子案例後，只歸納出一條規則：「涉及違法行為（吸毒、犯罪）就優先檢查除外責任」。這條規則後來
被 `review_memory.py` 的 baseline/candidate 對照證實對 `evals/check_cases.yaml` 的 `drunk_driving_bike` 沒有實際幫助而 reject——
因為前 3 筆種子案例裡「違法行為」永遠對應「除外成立」，模型沒有任何反例可以歸納出真正的規則（除外要看的是命中哪一條、那一條管的
是哪個給付，不是單純看行為合不合法）。`seed-drunk_ride_ltc` / `seed-drunk_driving_grade23` 這組對照直接把這個區分餵給蒸餾器。

## 跟 `evals/check_cases.yaml` 的關係

- 這份清單的案例是**蒸餾器的輸入**（episodic few-shot 語料）。
- `evals/check_cases.yaml` 是**評測用的標準答案**（holdout / regression），不能跟這裡的案例共用逐字稿——兩者混用會讓評測分不清
  是模型真的學會了，還是剛好背過同一句話（`scripts/seed_exclusion_episodic_examples.py` 檔頭註解記錄過這個教訓）。
- 目前 `check_cases.yaml` 3 筆全部是 `split: holdout`，`regression` 還是 0 筆——要等某條 procedural 規則被人審核 approve、
  且它的 `evidence` 指到某個案例時，才會由審核者手動把該案例貼進 `check_cases.yaml` 並標成 `regression`
  （`docs/knowledge-distillation-plan.md` §5 P3 item 1）。
