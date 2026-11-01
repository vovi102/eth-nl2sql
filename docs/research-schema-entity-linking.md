# Khảo sát phương pháp schema linking và entity/value linking cho text-to-SQL (2023–2026)

Tài liệu khảo sát, viết để chọn và bảo vệ phương pháp linking của luận văn (xem
`linking-method.md`, `data-scope.md`). Các bài báo được đọc từ bản PDF trên arXiv ngày
2026-10-31; leaderboard đọc ngày 2026-11-01.

**Quy ước.**

- Mọi con số đều ghi kèm benchmark, split, metric và vị trí trong bài (bảng/mục). Số trên
  leaderboard thay đổi theo thời gian, nên chỉ đúng tại ngày đọc.
- Mục 1–5 chỉ ghi **sự kiện** lấy từ nguồn gốc. **Nhận định và gợi ý** của người khảo sát chỉ
  nằm ở mục 6 và trong các ô có nhãn "Nhận định".
- Những gì chưa kiểm chứng được từ nguồn gốc được liệt kê ở mục 7.

Viết tắt:

- **EX**: execution accuracy. **EM**: exact match. **TS**: test-suite accuracy.
- **SRR/SLR**: tỉ lệ câu hỏi mà schema linking giữ đủ *mọi* cột cần thiết (strict recall).
- **FPR**: tỉ lệ cột thừa đưa vào prompt.
- **EK**: external knowledge, tức "evidence" của BIRD.
- **dev/test**: split dev công khai / split test ẩn.

---

## 0. Tóm tắt

1. **Schema linking thường có lợi cho model nhỏ, ít lợi hoặc có hại cho model mạnh.** Trên
   10% BIRD dev, mọi cách lọc schema đều làm giảm EX của ft:GPT-4o (−4,77 đến −11,57), nhưng
   lại tăng EX của Llama 3.1-8B và các model yếu khác
   ([2408.07702](https://arxiv.org/abs/2408.07702), §4.2, Bảng 2). Ngược lại, với
   Qwen-2.5-7B, schema truy hồi được tăng EX trên BIRD dev từ 37,13 lên 40,16; schema hoàn
   hảo (oracle) đạt 48,44 ([2510.14296](https://arxiv.org/abs/2510.14296), Bảng 2).
2. **Value retrieval là thành phần đóng góp ổn định.**
   - CHASE-SQL bỏ LSH value retrieval thì mất 2,92 EX
     ([2410.01943](https://arxiv.org/abs/2410.01943), Bảng 7).
   - CodeS bỏ value retriever thì mất 1,83–3,19 EX trên BIRD dev
     ([2402.16347](https://arxiv.org/abs/2402.16347), Bảng 9).
   - CHESS bỏ retrieve entity và context thì mất 4,76 EX
     ([2405.16755](https://arxiv.org/abs/2405.16755), Bảng 4).
3. **Value retrieval hiện nay có chung một khuôn**: LLM trích keyword, tra một index rẻ (LSH
   hoặc BM25), rồi xếp hạng lại bằng embedding và edit distance/LCS. Khuôn này dùng ở CHESS,
   CHASE-SQL, XiYan-SQL và CodeS.
4. **Không phương pháp nào trong số trên giải quyết bài toán "tên → tập địa chỉ".** Cả khuôn
   ở ý 3 lẫn BRIDGE/ValueNet đều so khớp chuỗi trong câu hỏi với *giá trị đang có trong DB*.
   Trong `crypto_ethereum`, tên sàn/protocol không có trong DB, còn symbol token thì trùng
   hàng nghìn lần. Bài toán này gần với **entity linking cổ điển** hơn: alias table, sinh ứng
   viên, rồi xếp hạng có prior (BLINK, ReFinED).
5. **Model nhỏ hưởng lợi rất lớn khi câu hỏi được "làm giàu" bằng item của DB.** Trên BIRD
   dev, Qwen2.5-Coder-1.5B tăng từ 11,21 lên 36,90 EX khi dùng câu hỏi do GPT-4o làm giàu
   ([2409.16751](https://arxiv.org/abs/2409.16751), Bảng 8). Đây là bằng chứng mạnh nhất cho
   cách "đưa kết quả linking vào prompt" với model cỡ của luận văn.
6. **Model 0,5B–3B sau fine-tune đã đạt mức cạnh tranh.**
   - SLM-SQL 1,5B: 67,08 EX BIRD dev, nhưng cần 64 mẫu và có merge revision
     ([2507.22478](https://arxiv.org/abs/2507.22478), Bảng 2).
   - XiYanSQL-QwenCoder-3B: 55,08 BIRD dev, số tự báo cáo trên model card
     ([HF](https://huggingface.co/XGenerationLab/XiYanSQL-QwenCoder-3B-2504)).
   - OmniSQL-7B: 63,9 BIRD dev ([2503.02240](https://arxiv.org/abs/2503.02240), Bảng 4).
7. **Các hệ thống đứng đầu leaderboard phần lớn không công bố phương pháp.** Ngày 2026-11-01,
   top BIRD test là 83,40 EX (AskData) và top Spider 2.0-Lite là 76,23. Phương pháp có bài báo
   để trích dẫn dừng ở khoảng 75,63 BIRD test (XiYan-SQL).
8. **Không tìm thấy benchmark NL2SQL riêng cho Ethereum.** Spider 2.0-Lite có 27 bài thuộc 3
   DB blockchain. Các bài này gần như luôn **cho sẵn địa chỉ contract** trong câu hỏi, tức là
   né bước entity linking (mục 4.1).

---

## 1. Phân loại bài toán

| Bài toán con | Câu hỏi cần trả lời | Ví dụ trong luận văn |
|---|---|---|
| Schema linking | Câu hỏi cần những bảng/cột nào? | "phí giao dịch" → `transactions.gas_price`, `receipt_gas_used`, ... |
| Value linking | Literal trong câu hỏi khớp với giá trị nào trong cột nào? | "status thất bại" → `receipt_status = 0` |
| Entity linking | Tên/alias trong câu hỏi trỏ tới thực thể nào trong KB, và thực thể đó ứng với những định danh nào trong DB? | "Tornado Cash" → tập địa chỉ contract |

Các khảo sát lớn
([2406.08426](https://arxiv.org/abs/2406.08426), [2408.05109](https://arxiv.org/abs/2408.05109))
gộp value linking vào bước "schema linking" hoặc "information retrieval". Ở đây hai bước được
tách riêng, vì trong dữ liệu blockchain chúng cần cơ chế khác nhau. Hai khảo sát này chỉ được
dùng cho phân loại, không dùng cho số liệu.

---

## 2. Schema linking

### 2.1 Bộ phân loại (cross-encoder hoặc extractive)

**RESDSQL** — Li và cộng sự, AAAI 2023, [2302.05965](https://arxiv.org/abs/2302.05965).

- **Ý tưởng.** Một cross-encoder dựa trên RoBERTa phân loại từng bảng/cột là liên quan hay
  không. Mô hình có thêm column-enhanced layer và dùng focal loss. Chỉ giữ top-k1 bảng và
  top-k2 cột mỗi bảng (k1 = 4, k2 = 5), sắp theo điểm, rồi đưa vào T5. Decoder sinh skeleton
  SQL trước, sau đó mới sinh SQL.
- **Số liệu.**
  - AUC trên Spider dev: bảng 0,9973, cột 0,9957 (Bảng 4).
  - RESDSQL-3B + NatSQL: Spider dev EM 80,5 / EX 84,1; Spider test EM 72,0 / EX 79,9
    (Bảng 2).
  - Thay schema đã xếp hạng bằng schema không thứ tự làm EM giảm 4,5 và EX giảm 7,8
    (Bảng 5).
- **Cần gì.** Một encoder khoảng 355M tham số để phân loại, cộng một seq2seq T5 (Base đến 3B).
  Cần dữ liệu huấn luyện có nhãn bảng/cột, suy ra được từ gold SQL.

**CodeS schema filter** — SIGMOD 2024, [2402.16347](https://arxiv.org/abs/2402.16347), §6.1.

- Dùng lại bộ phân loại của RESDSQL. Khi SFT giữ top 6 bảng × 10 cột; khi ICL giữ 5 × 6.
- AUC bảng/cột (Bảng 3): Spider 0,991/0,993; BIRD 0,966/0,943; BIRD có EK 0,976/0,957.
- Bỏ schema filter (3-shot ICL, BIRD dev, Bảng 9) thay đổi EX lần lượt −1,89 (1B), −0,59 (3B),
  +0,39 (7B), −1,57 (15B).

**ExSL (Extractive Schema Linking)** — IBM, 01/2025, [2501.17174](https://arxiv.org/abs/2501.17174).

- **Ý tưởng.** Fine-tune một LLM decoder-only (DeepSeek Coder 6.7B) nhưng **không sinh
  token**. Biểu diễn cuối của các token then chốt được dùng để dự đoán cột nào liên quan và
  **vai trò** của cột trong SQL (select, join, condition, order, group) (§3.3). Cách này cho
  điểm tin cậy cho từng cột, nên điều chỉnh được ngưỡng theo recall.
- **Số liệu.**
  - Trên BIRD, ROC AUC theo cột của ExSLf là 99,38 và PR AUC là 93,67. Phương án sinh
    (generative) chỉ đạt 94,15 và 69,18 (Bảng 4).
  - EX trên Spider dev (Bảng 5): full schema 78,7; ExSLf 82,4; schema gold theo cột 84,0.
  - EX trên BIRD dev (Bảng 6): ExSLf 63,23; schema gold 73,40.
  - Trên leaderboard BIRD có mục "ExSL + granite-34b-code" đạt 73,17 test
    ([2507.04701](https://arxiv.org/abs/2507.04701), Bảng I).

**DTS-SQL** — [2402.01117](https://arxiv.org/abs/2402.01117).

- Tách hai model 7B fine-tune riêng: một model linking cấp bảng và một model sinh SQL.
- Với DeepSeek 7B trên Spider dev (EX, Bảng 3): fine-tune trên mọi bảng đạt 82,1; hai giai
  đoạn đạt 85,5; schema hoàn hảo đạt 90,3.
- Bộ linking cấp bảng đạt precision 98,4 / recall 97,7 trên Spider dev (Bảng 5).
- BIRD: dev 55,8 / test 60,31 (theo [2405.16755](https://arxiv.org/abs/2405.16755), Bảng 1).

**MSc-SQL** — [2410.12916](https://arxiv.org/abs/2410.12916).

- Model nhỏ hơn 10B. Thêm một schema linker fine-tune (Mistral-7B) đưa generator Mistral-7B
  từ 56,4 lên 61,3 EX trên BIRD dev (Bảng 2).
- Cả pipeline có multi-sample critiquing đạt 65,6 BIRD dev và 84,7 Spider test (Bảng 1).

### 2.2 Prompting LLM để chọn schema

| Phương pháp | Cơ chế schema linking | Kết quả chính |
|---|---|---|
| DIN-SQL [2304.11015](https://arxiv.org/abs/2304.11015) | Module chain-of-thought riêng tìm tham chiếu tới schema **và giá trị điều kiện** (§4.1) | Spider test 85,3 EX; BIRD test 55,9 EX (GPT-4; abstract) |
| C3 [2307.07306](https://arxiv.org/abs/2307.07306) | "Clear Prompting": ChatGPT recall bảng trước, rồi recall cột; zero-shot | Spider test 82,3 EX (abstract) |
| DAIL-SQL [2308.15363](https://arxiv.org/abs/2308.15363) | Không phải schema linking; nghiên cứu cách biểu diễn schema và chọn ví dụ few-shot | Spider test 86,6 EX (abstract) |
| MAC-SQL [2312.11242](https://arxiv.org/abs/2312.11242) | Agent "Selector" cắt schema, **chỉ kích hoạt khi prompt schema vượt ngưỡng độ dài** (§3.2) | BIRD test 59,59 EX (GPT-4) |
| TA-SQL [2405.15307](https://arxiv.org/abs/2405.15307) | Sinh "dummy SQL" rồi trích bảng/cột từ đó (TASL) | BIRD dev 56,19 / test 59,14 (GPT-4, Bảng 2) |

### 2.3 Retrieval + selector nhiều bước

**CHESS** — Talaei và cộng sự, [2405.16755](https://arxiv.org/abs/2405.16755) v3 (11/2024).

- **Ý tưởng.** Bốn agent:
  - Information Retriever: trích keyword, retrieve entity (value) và retrieve context (mô tả
    cột qua vector DB).
  - Schema Selector: filter column, select tables, select columns.
  - Candidate Generator.
  - Unit Tester.
- **Số liệu.**
  - BIRD (Bảng 1): CHESS(IR,CG,UT) dev 68,31 / test 71,10.
  - BIRD (Bảng 2): CHESS(IR,SS,CG) với model đóng dev 65,00 / test 66,69; với model mở
    (DeepSeek Coder fine-tune + Llama-3-70B) dev 61,5.
  - Spider test 87,2 (Bảng 3).
- **Nhận xét của chính tác giả (§3.2).**
  - Khi schema nhỏ và LLM mạnh, Schema Selector có thể làm giảm EX, nên nên bỏ.
  - Với schema 4.337 cột, Schema Selector tăng khoảng 2% và tốn ít hơn 5 lần token (§4.2.2).
  - Khi tài nguyên hạn chế hoặc chỉ có model mở nhỏ, nên bỏ Unit Tester và chỉ sinh 1 ứng
    viên.
- **Chi phí.** Theo [2510.14296](https://arxiv.org/abs/2510.14296) (dẫn lại Chung và cộng sự,
  2025), CHESS trung bình gọi LLM 78 lần và dùng khoảng 339.965 token cho mỗi câu hỏi BIRD dev.
  Đây là số thứ cấp, chưa đối chiếu với nguồn gốc.

**XiYan-SQL** — bản preview [2411.08599](https://arxiv.org/abs/2411.08599); bản đầy đủ
[2507.04701](https://arxiv.org/abs/2507.04701), IEEE TKDE vol. 38, 04/2026.

- **Schema Filter (§III.B bản đầy đủ).**
  - LLM trích keyword từ câu hỏi và evidence.
  - Điểm của mỗi cột = cos(keyword, metadata cột) × cos(câu hỏi‖evidence, metadata bảng).
  - Value retrieval gồm ba bước: lấy top-k giá trị theo edit distance trong từng cột, lọc
    bằng LSH (tokenize bằng tokenizer RoBERTa), rồi lọc theo ngưỡng cosine embedding.
  - Sau đó **chọn cột lặp nhiều vòng**, sinh ra nhiều schema với các mức precision/recall
    khác nhau. Khóa chính/khóa ngoại luôn được giữ lại để không mất đường join.
- **Số liệu.**
  - Bảng IV, BIRD dev: schema S1 có precision 74,89, recall cột 83,64, recall giá trị 91,31.
    CHESS do nhóm XiYan chạy lại đạt 85,90 / 74,77 / 63,71.
  - Bỏ Schema Filter làm EX giảm 1,24 (73,34 → 72,10, Bảng III).
  - Bản preview, Bảng 7 (GPT-4o, BIRD dev): schema linking nâng precision từ 10,14 lên 74,74,
    recall giảm từ 100 xuống 95,47, EX tăng từ 57,95 lên 60,10.
  - Toàn hệ thống: BIRD dev 73,34 / test 75,63; Spider test 89,65 (Bảng I, II).

**CHASE-SQL** — Google, [2410.01943](https://arxiv.org/abs/2410.01943).

- Không có bước lọc schema riêng. Value retrieval làm như CHESS: LLM trích keyword, dùng LSH,
  rồi xếp hạng lại bằng embedding và edit distance (§3.2).
- Sinh nhiều ứng viên theo nhiều kiểu chain-of-thought, rồi một selection agent so sánh từng
  cặp.
- BIRD dev 73,01 / test 73,0 (Gemini 1.5 Pro); Spider test 87,6. Bỏ LSH làm giảm 2,92
  (Bảng 7).

**OpenSearch-SQL** — [2502.14913](https://arxiv.org/abs/2502.14913).

- Giai đoạn Extraction gồm trích entity, trích value (truy hồi bằng vector), lọc cột và "Info
  Alignment" (§3.4).
- BIRD dev 69,3 / test 72,28 (abstract).

**Rethinking Schema Linking (bidirectional retrieval)** — Nahid và cộng sự,
[2510.14296](https://arxiv.org/abs/2510.14296) v2 (01/2026).

- **Ý tưởng.** Coi schema linking là một bài toán truy hồi độc lập và kết hợp hai chiều:
  chọn bảng rồi chọn cột, và chọn cột rồi chọn bảng. Thêm tách câu hỏi con, trích keyword và
  keyphrase. Không cần sửa SQL sau khi sinh.
- **Số liệu (BIRD dev).**
  - Bảng 1: recall 91,56 / FPR 19,04 với DeepSeek; 85,45 / 33,69 với Qwen-2.5-7B. So sánh:
    CHESS 97,12 / 30,57; RSL-SQL 93,28 / 68,23.
  - Bảng 2, EX zero-shot theo ba mức schema (full / hoàn hảo / truy hồi):

    | Model | Full schema | Schema truy hồi | Schema hoàn hảo |
    |---|---|---|---|
    | GPT-4o-mini | 45,70 | 51,11 | 56,84 |
    | Qwen-2.5-7B | 37,13 | 40,16 | 48,44 |

### 2.4 Linking hai chiều và làm giàu câu hỏi

**RSL-SQL** — [2411.00073](https://arxiv.org/abs/2411.00073).

- **Ý tưởng.**
  - Forward linking: LLM chọn cột từ schema đầy đủ.
  - Backward linking: sinh SQL nháp trên schema đầy đủ, rồi trích các cột mà SQL nháp dùng.
  - Hợp hai tập để có schema rút gọn. Cuối cùng chọn giữa SQL sinh trên schema đầy đủ và SQL
    sinh trên schema rút gọn, nhằm phòng rủi ro mất cột.
- **Số liệu (Bảng III, BIRD dev, GPT-4o).**
  - Hai chiều: SRR 94,32, NSR 97,69, trung bình 13,02 cột (schema đầy đủ có 76,28 cột).
  - Chỉ forward: SRR 84,74. Chỉ backward: SRR 90,48.
  - EX BIRD dev 67,21 (GPT-4o) và 63,56 (DeepSeek); Spider test 87,9 và 87,5 (Bảng I, II).

**E-SQL** — [2409.16751](https://arxiv.org/abs/2409.16751).

- **Ý tưởng.** "Direct schema linking": viết lại câu hỏi, chèn trực tiếp bảng/cột/giá trị và
  điều kiện vào câu hỏi (Question Enrichment). Candidate Predicate Generation dùng BM25 lấy 10
  giá trị liên quan nhất cho mỗi cột và 20 câu mô tả liên quan nhất.
- **Số liệu.**
  - BIRD test 66,29 / dev 65,58 với GPT-4o; Qwen2.5-Coder-7B đạt 53,59 dev (Bảng 1).
  - Thêm module lọc schema làm **giảm** 5,54 EX (GPT-4o-mini, BIRD dev, Bảng 5).
  - Bảng 8, model nhỏ không fine-tune nhận câu hỏi do GPT-4o làm giàu (BIRD dev EX):

    | Model | Câu hỏi gốc | Câu hỏi làm giàu |
    |---|---|---|
    | DeepSeek Coder 1.3B | 20,92 | 50,84 |
    | **Qwen2.5-Coder-1.5B** | **11,21** | **36,90** |
    | DeepSeek Coder 7B v1.5 | 37,02 | 56,45 |
    | Qwen2.5-Coder-7B | 31,25 | 40,22 |

### 2.5 Hướng "không cần schema linking"

**"The Death of Schema Linking?"** — Maamari và cộng sự (Distyl AI),
[2408.07702](https://arxiv.org/abs/2408.07702).

- Hệ thống này mang tên "Distillery" trên leaderboard. Tên này chỉ thấy trong bảng so sánh của
  [2405.16755](https://arxiv.org/abs/2405.16755) (Bảng 1) và
  [2507.04701](https://arxiv.org/abs/2507.04701) (Bảng I), không có trong chính bài báo.
- **Ý tưởng.** Nếu schema vừa context thì đưa toàn bộ schema vào. Thay việc lọc bằng làm giàu
  ngữ cảnh, sinh nhiều ứng viên để chọn, và sửa lỗi.
- **Số liệu.**
  - BIRD test 71,83 (ft:GPT-4o), hạng 1 tại thời điểm công bố. Theo
    [2405.16755](https://arxiv.org/abs/2405.16755) Bảng 1, dev là 67,21.
  - Bảng 1 (trung bình 12 lần chạy): đánh đổi FPR/SLR của các cách linking, từ TCSL
    (FPR 9,79 / SLR 77,44) đến HySCSL (82,08 / 90,36).
  - Bảng 2 (10% BIRD dev): thêm TCSL làm EX của ft:GPT-4o giảm 4,77; thêm SCSL làm giảm
    11,57.
- **Kết luận của tác giả (§4.2, §5).**
  - Model càng mạnh thì lợi ích của schema linking càng giảm.
  - Với model yếu hơn (Llama 3.1-8B, Mixtral-8x22B, DeepSeek Coder-V2), schema linking vẫn
    tăng EX.

**Long context** — Chung và cộng sự (Google), [2501.12372](https://arxiv.org/abs/2501.12372).

- Với Gemini 1.5 Pro, không fine-tune và không self-consistency: BIRD dev 67,41 (Bảng 1).
- Trong ablation, **hint (evidence) có tác động lớn nhất**, sau đó là giá trị mẫu của cột,
  rồi đến self-correction (§1).
- Đưa thêm danh sách giá trị cột dài ("disambiguation") khi SQL trả về rỗng: EX tăng từ 64,80
  lên 65,51, nhưng tốn trung bình 15.755 token. Đưa schema đã lọc đạt 65,84 với khoảng 3.931
  token (Bảng 5).

### 2.6 Schema rất lớn và nhiều DB (Spider 2.0)

- **Spider 2.0** (ICLR 2025, [2411.07763](https://arxiv.org/abs/2411.07763)).
  - 632 bài từ workflow doanh nghiệp. DB lưu trên BigQuery, Snowflake, SQLite..., thường hơn
    1.000 cột.
  - Bản Lite có 547 bài.
  - Agent dùng o1-preview chỉ giải được 21,3% (abstract).
- **ReFoRCE** ([2502.00675](https://arxiv.org/abs/2502.00675)).
  - Nén thông tin DB (gom bảng theo pattern, schema linking bằng LLM), bỏ phiếu đa số, và
    **column exploration**: chạy lặp các truy vấn nhỏ (`SELECT DISTINCT ... LIMIT`) để xác
    định giá trị và cột mơ hồ.
  - Spider 2.0-Snow 35,83; Spider 2.0-Lite 36,56 (v5, abstract).
- **LinkAlign** ([2503.18596](https://arxiv.org/abs/2503.18596)).
  - Hai bước: chọn đúng DB trong nhiều DB, rồi định vị bảng/cột.
  - Spider 2.0-Lite 33,09 với DeepSeek-R1 (Bảng 1).
- **AutoLink** ([2511.17190](https://arxiv.org/abs/2511.17190), mã nguồn
  [github.com/wzy416/AutoLink](https://github.com/wzy416/AutoLink)).
  - Agent mở rộng dần tập schema mà không đưa schema đầy đủ vào prompt.
  - SRR 97,4 trên BIRD dev và 91,2 trên Spider 2.0-Lite; EX 68,7 trên BIRD dev và 34,9 trên
    Spider 2.0-Lite (abstract).

### 2.7 Model mở fine-tune (phần sinh SQL đi kèm linking)

| Model | Cách linking trong prompt | Kết quả |
|---|---|---|
| CodeS 1B/3B/7B/15B ([2402.16347](https://arxiv.org/abs/2402.16347)) | Schema filter + value retriever + 2 giá trị đại diện mỗi cột + khóa chính/ngoại | SFT, BIRD dev có EK: 50,46 / 55,02 / 57,17 / 58,47 (Bảng 6). Spider dev EX: 77,9 / 83,4 / 85,4 / 84,9 (Bảng 5). Bộ nhớ fp16: 10 / 13 / 20 / 35 GB (§9.7) |
| OmniSQL 7B/14B/32B ([2503.02240](https://arxiv.org/abs/2503.02240), [repo](https://github.com/RUCKBReasoning/OmniSQL)) | CREATE TABLE kèm mô tả cột, 2 giá trị đại diện, và "question-relevant values" theo cách của CodeS (phần mô tả input của model) | OmniSQL-7B: BIRD dev 63,9, Spider test 87,9, Spider2.0-SQLite 10,4 (greedy). Base Qwen2.5-Coder-7B-Instruct: 50,9 BIRD dev. Bản LoRA 7B: 61,5 (Bảng 4) |
| XiYanSQL-QwenCoder-3B-2504 ([model card](https://huggingface.co/XGenerationLab/XiYanSQL-QwenCoder-3B-2504)) | Schema dạng M-Schema | BIRD dev 55,08; Spider test 84,10. **Số tự báo cáo**; giấy phép Apache-2.0 |
| SLM-SQL 0.5B/1.5B ([2507.22478](https://arxiv.org/abs/2507.22478)) | **Không có schema linking** (mục Limitations); SFT + GRPO + corrective self-consistency | Qwen2.5-Coder-1.5B: từ 28,40 (base) lên 67,08 BIRD dev; 0,5B đạt 56,87 (Bảng 2). Mặc định 64 mẫu SQL + 8 mẫu merge; giảm còn 16 mẫu thì mất 2,24 điểm |

---

## 3. Value linking và entity linking

### 3.1 So khớp mờ với giá trị trong DB (thế hệ trước LLM)

- **BRIDGE** — Lin và cộng sự, Findings of EMNLP 2020,
  [2012.12627](https://arxiv.org/abs/2012.12627).
  - So khớp mờ câu hỏi với "picklist" (tập giá trị) của từng trường. Giá trị khớp được chèn
    thành *anchor text* ngay sau tên cột trong chuỗi đầu vào.
  - Bỏ qua các số trong câu hỏi, vì một con số có thể chỉ là ngưỡng chứ không phải giá trị
    trong ô (đoạn "Anchor Text Selection").
  - Bỏ phần bridging làm E-SM trên Spider dev giảm 1,6, riêng nhóm extra-hard giảm từ 51,8
    xuống 42,8 (Bảng 6).
- **ValueNet** — [2006.00888](https://arxiv.org/abs/2006.00888).
  - Quy trình 4 bước: trích giá trị (NER + heuristic: chuỗi trong ngoặc kép, từ viết hoa, chữ
    cái đơn); sinh ứng viên (khoảng cách Damerau–Levenshtein, heuristic, n-gram); kiểm tra ứng
    viên bằng tra DB; mã hóa ứng viên cùng bảng/cột chứa nó.
  - Spider EX 67% khi cho sẵn giá trị đúng (ValueNet light) và 62% khi phải tự tìm giá trị.

### 3.2 Index rẻ rồi xếp hạng lại (thế hệ LLM)

| Phương pháp | Bước tìm ứng viên | Bước xếp hạng lại | Bằng chứng định lượng |
|---|---|---|---|
| CodeS ([2402.16347](https://arxiv.org/abs/2402.16347) §6.2) | BM25 (Lucene) trên mọi giá trị trong DB, lấy vài trăm giá trị | Longest common substring giữa câu hỏi và giá trị | Bỏ value retriever: −3,19 / −1,96 / −2,48 / −1,83 EX trên BIRD dev (1B / 3B / 7B / 15B, 3-shot, Bảng 9) |
| CHESS ([2405.16755](https://arxiv.org/abs/2405.16755) App. A.1) | LLM trích keyword; LSH trên các giá trị khác nhau, lấy top-10 | Embedding (text-embedding-3-small) + ngưỡng; với mỗi cặp keyword–cột giữ giá trị có edit distance nhỏ nhất | Thời gian từ 5 phút giảm còn 5 giây cho mỗi câu hỏi. Bỏ entity + context retrieval: −4,76 EX (147 câu, Bảng 4) |
| CHASE-SQL ([2410.01943](https://arxiv.org/abs/2410.01943) §3.2) | Keyword + LSH | Embedding + edit distance | Bỏ LSH: −2,92 EX trên BIRD dev (Bảng 7) |
| XiYan-SQL ([2507.04701](https://arxiv.org/abs/2507.04701) §III.B) | Edit distance lấy top-k mỗi cột, sau đó LSH | Ngưỡng cosine embedding | Recall giá trị 91,31 (S1), so với 63,71 của CHESS chạy lại (Bảng IV) |
| E-SQL ([2409.16751](https://arxiv.org/abs/2409.16751), phần CPG) | BM25 lấy 10 giá trị mỗi cột và 20 câu mô tả | LLM tự dựng predicate ứng viên | Bỏ CPG: −2,80 EX trên BIRD dev với Qwen2.5-Coder-7B (Bảng 6) |

**Giá trị đại diện** (không phụ thuộc câu hỏi) là một dạng value linking yếu nhưng rẻ. CodeS
chạy `SELECT DISTINCT col ... LIMIT 2` cho mỗi cột (§6.3). Bỏ phần này làm EX trên BIRD dev
giảm 1,50 đến 3,20 (Bảng 9).

### 3.3 Khám phá giá trị bằng cách chạy truy vấn

- ReFoRCE dùng column exploration (mục 2.6).
- Long-context dùng "disambiguation": đưa danh sách giá trị dài khi kết quả rỗng (mục 2.5).
- Cả hai chạy truy vấn thật lên DB để xem giá trị. Cách này tốn nhiều lượt gọi LLM và nhiều
  token.

### 3.4 Entity linking cổ điển (KB và alias)

- **BLINK** — Wu và cộng sự, [1911.03814](https://arxiv.org/abs/1911.03814).
  - Bi-encoder truy hồi ứng viên trong không gian dense, sau đó cross-encoder xếp hạng lại.
    Mỗi thực thể chỉ cần một đoạn mô tả ngắn, nên hoạt động được với thực thể chưa từng thấy
    khi huấn luyện (zero-shot).
  - Unnormalized accuracy trên Zero-shot EL đạt 63,03 với bản large (Bảng 2).
  - Truy hồi trong 5,9 triệu ứng viên mất khoảng 2 ms (abstract).
  - Bài nêu rõ phương pháp trước đó dựa vào "frequency information, alias tables and
    TF-IDF" (phần related work).
- **ReFinED** — Amazon, [2207.04108](https://arxiv.org/abs/2207.04108).
  - Trong một lần forward làm cả ba việc: phát hiện mention, gán kiểu thực thể chi tiết, và
    phân giải thực thể.
  - Điểm cuối kết hợp điểm kiểu, điểm mô tả và **prior toàn cục P̂(e|m)**, tức độ phổ biến của
    thực thể e khi gặp mention m. Prior được thêm vào để xử lý ngữ cảnh ngắn như câu hỏi.
  - Có ứng viên **NIL** với điểm 0, nghĩa là không có thực thể nào đúng (phần mô tả bước entity disambiguation).
  - Ứng viên lấy top-30 theo prior.
  - Bỏ prior làm micro-F1 trung bình giảm từ 88,6 xuống 87,2 (Bảng 1).
  - Nhanh hơn các hệ thống cạnh tranh hơn 60 lần (abstract).

### 3.5 Câu hỏi mơ hồ

- **AmbiQT** ([2310.13659](https://arxiv.org/abs/2310.13659)).
  - Hơn 3.000 câu hỏi, mỗi câu có 2 SQL hợp lệ do mơ hồ từ vựng (tên cột/bảng chồng nhau) hoặc
    mơ hồ cấu trúc.
  - Các hệ thống text-to-SQL, kể cả hệ dùng LLM, còn xa mới sinh được đủ các cách hiểu.
- **AMBROSIA** (NeurIPS 2024 D&B, [2406.19073](https://arxiv.org/abs/2406.19073)).
  - Ba loại mơ hồ: scope, attachment và **vagueness**. Mơ hồ vẫn còn ngay cả khi đã cho schema.
  - Các LLM mạnh nhất vẫn khó nhận ra và diễn giải các câu mơ hồ (abstract).

---

## 4. Blockchain/crypto, model nhỏ và tiếng Việt

### 4.1 Text-to-SQL trên dữ liệu blockchain

**Không tìm thấy** benchmark hay bài báo text-to-SQL nào dành riêng cho Ethereum, Dune hoặc dữ
liệu on-chain. Các truy vấn WebSearch ngày 2026-10-31 với từ khóa "text-to-SQL Ethereum",
"blockchain", "on-chain", "Dune" chỉ trả về bài text-to-SQL tổng quát. Điều này **không chứng
minh** rằng chưa có công trình như vậy.

Spider 2.0 có phần crypto. Đếm trong `spider2-lite/spider2-lite.jsonl`, nhánh `main` của
[xlang-ai/Spider2](https://github.com/xlang-ai/Spider2), ngày 2026-11-01 (theo trường `db`):

- `CRYPTO`: 20 bài (Ethereum, Bitcoin, Ethereum Classic, Dash, Zilliqa, ...).
- `ETHEREUM_BLOCKCHAIN`: 3 bài.
- `GOOG_BLOCKCHAIN`: 4 bài.
- Tổng cộng **27/547** bài.

Đặc điểm của các bài này: token và địa chỉ hầu như luôn được **cho sẵn bằng địa chỉ hex**.

- `sf_bq083` ghi rõ địa chỉ USDC `0xa0b8…eb48`.
- `sf_bq342` cho địa chỉ token, địa chỉ người gửi và người nhận.
- Chỉ `sf_bq187` gọi token bằng symbol (`'BNB'`) mà không kèm địa chỉ.
- Một số bài kèm tài liệu ngoài ("external_knowledge"), ví dụ `ethereum_logs_and_events_overview.md`.

Nói cách khác, benchmark lớn nhất có dữ liệu `crypto_ethereum` gần như không kiểm tra entity
linking. Đây là khoảng trống mà luận văn có thể lấp.

### 4.2 Model nhỏ và phần cứng hạn chế

- **Ước lượng bộ nhớ.** CodeS báo bộ nhớ fp16 khi suy luận là 10/13/20/35 GB cho
  1B/3B/7B/15B ([2402.16347](https://arxiv.org/abs/2402.16347) §9.7). Không bài nào trong
  khảo sát báo kết quả trên GPU 4 GB. Với GTX 1650, cần lượng tử hóa; độ chính xác sau lượng
  tử hóa **chưa có số liệu trong các nguồn đã đọc**.
- **Model nhỏ có hưởng lợi từ linking không.** Các bằng chứng độc lập cùng chỉ một hướng:
  - [2408.07702](https://arxiv.org/abs/2408.07702) §4.2: model yếu được lợi từ schema linking.
  - [2510.14296](https://arxiv.org/abs/2510.14296) Bảng 2: Qwen-2.5-7B +3,03 EX, nhưng vẫn
    cách oracle 8,28 điểm.
  - [2410.12916](https://arxiv.org/abs/2410.12916) Bảng 2: +4,9 EX nhờ linker fine-tune.
  - [2402.01117](https://arxiv.org/abs/2402.01117) Bảng 3: +3,4 EX trên Spider dev.
  - [2409.16751](https://arxiv.org/abs/2409.16751) Bảng 8: +25,69 EX với Qwen2.5-Coder-1.5B
    khi câu hỏi được làm giàu.
- **Chi phí pipeline.** Các pipeline nhiều agent (CHESS, CHASE-SQL, XiYan-SQL) gọi LLM nhiều
  lần và sinh nhiều ứng viên. SLM-SQL đạt 67,08 nhờ 64 mẫu cộng mô hình merge
  ([2507.22478](https://arxiv.org/abs/2507.22478) §3).
- **LoRA và full fine-tune.** OmniSQL ghi nhận LoRA kém full fine-tune ở 7B (BIRD dev 61,5 so
  với 63,9), và khoảng cách thu hẹp ở 14B ([2503.02240](https://arxiv.org/abs/2503.02240),
  Bảng 4 và đoạn phân tích "Smaller models benefit more from full fine-tuning").

### 4.3 Tiếng Việt

- **ViText2SQL** (Findings of EMNLP 2020, [2010.01891](https://arxiv.org/abs/2010.01891),
  VinAI).
  - Khoảng 10K cặp câu hỏi–SQL, dịch tay từ Spider.
  - Các kết luận (abstract):
    - Tách từ tiếng Việt giúp cả hai baseline (EditSQL, IRNet).
    - Điểm NPMI có ích cho schema linking.
    - PhoBERT tốt hơn XLM-R.
  - Giấy phép chỉ cho nghiên cứu/giáo dục.
- **ViR²** ([VJSTE](https://vietnamscience.vjst.vn/index.php/vjste/article/view/1364)).
  - Chọn ví dụ few-shot cho tiếng Việt.
  - 23,33 EM trên ViText2SQL. Số này chỉ đọc từ trang tóm tắt; chưa đọc toàn văn, chưa rõ LLM
    sử dụng.
- **Chưa tìm thấy** công trình value/entity linking nào cho câu hỏi song ngữ Việt–Anh.

---

## 5. Bảng so sánh tổng hợp

Ký hiệu: L = cần LLM lớn qua API hoặc từ 70B trở lên; S = chạy được với model nhỏ hoặc không
cần LLM; FT = cần fine-tune.

| Phương pháp | Năm | Loại | Cần gì | Số tốt nhất (nguồn) | Áp dụng cho luận văn |
|---|---|---|---|---|---|
| RESDSQL | 2023 | Schema: cross-encoder | Encoder khoảng 355M + nhãn từ gold SQL (FT) | Spider test EX 79,9 (Bảng 2) | Rẻ, chạy CPU được; nhưng schema của luận văn chỉ có 6 bảng |
| CodeS filter + retriever | 2024 | Schema + value | Classifier (FT) + BM25 + LCS (S) | BIRD dev 58,47 có EK (15B, Bảng 6) | Value retriever hợp với cột chữ (symbol, name) |
| DIN-SQL / C3 / MAC-SQL | 2023 | Schema: prompting | L | Spider test 85,3 / 82,3; BIRD test 59,59 | Model 1,5–3B khó tự làm bước CoT linking |
| TA-SQL | 2024 | Schema: dummy SQL | L | BIRD test 59,14 | Như trên |
| CHESS | 2024 | Retrieval + selector | Nhiều lượt gọi LLM, LSH, embedding | BIRD test 71,10 (Bảng 1) | Mượn phần IR (LSH + embedding); bỏ các agent |
| RSL-SQL | 2024 | Hai chiều | L, 1–2 lượt + chọn | SRR 94,32; BIRD dev 67,21 | Backward linking cần SQL nháp đủ tốt |
| E-SQL | 2024 | Làm giàu câu hỏi | L để làm giàu; S để sinh | BIRD test 66,29; 1,5B từ 11,21 lên 36,90 khi làm giàu | **Rất hợp**: KB làm giàu câu hỏi một cách tất định |
| Death of SL / Distillery | 2024 | Bỏ linking | ft:GPT-4o | BIRD test 71,83 | Kết luận không áp dụng cho model nhỏ (chính bài nói vậy) |
| CHASE-SQL | 2024 | Value: LSH | L, nhiều ứng viên | BIRD test 73,0 | Mượn LSH |
| XiYan-SQL | 2024–26 | Multi-path + chọn cột lặp | Embedding, LSH, LLM, nhiều generator (FT) | BIRD test 75,63 | Ý tưởng "nhiều schema theo các mức recall" |
| ExSL | 2025 | Extractive, dự đoán vai trò cột | Decoder 6,7B (FT) | BIRD dev EX 63,23 | Ý tưởng "vai trò cột" khớp với bảng nghĩa property |
| Bidirectional retrieval | 2025–26 | Truy hồi hai chiều | LLM vừa | Qwen-7B từ 37,13 lên 40,16 BIRD dev | Có số cho model 7B |
| OmniSQL | 2025 | Model FT | 7B+ | BIRD dev 63,9 (7B) | Định dạng prompt đáng mượn khi LoRA |
| SLM-SQL | 2025 | Model FT 0,5–1,5B | SFT + RL, 64 mẫu | BIRD dev 67,08 (1,5B) | Cùng base model, nhưng 64 mẫu quá nặng cho GTX 1650 |
| XiYanSQL-QwenCoder-3B | 2025 | Model FT | 3B | BIRD dev 55,08 (tự báo cáo) | Baseline/điểm xuất phát tiềm năng cho cỡ 3B |
| LinkAlign / AutoLink / ReFoRCE | 2025 | Schema lớn, nhiều DB | L | Spider 2.0-Lite 33,09 / 34,9 / 36,56 | Ít liên quan, vì schema của luận văn nhỏ |
| BRIDGE / ValueNet | 2020 | Value: fuzzy match | S | Spider: −1,6 E-SM khi bỏ / EX 62% | Tiền thân của value retriever; bỏ qua số |
| BLINK | 2019–20 | Entity linking: bi- + cross-encoder | Encoder BERT (FT) | Zero-shot EL 63,03 | Nếu cần linking alias có ngữ cảnh |
| ReFinED | 2022 | Entity linking: kiểu + mô tả + prior P(e\|m) + NIL | Encoder (FT) | Micro-F1 TB 89,4 ED (Bảng 1) | **Mô hình khái niệm sát nhất** với KB alias + caller-count |

---

## 6. Gợi ý cho luận văn (nhận định của người khảo sát, không phải kết quả từ tài liệu)

### 6.1 Định vị phương pháp

Các phương pháp ở mục 2–3 giả định thực thể trong câu hỏi **có mặt dưới dạng chuỗi trong DB**.
Dữ liệu của luận văn vi phạm giả định này theo ba cách:

1. Tên sàn/protocol không có trong DB.
2. Symbol có trong DB nhưng trùng hàng nghìn lần và có ký tự đồng dạng.
3. Tham chiếu theo lớp không ứng với giá trị nào.

Vì vậy pipeline nên tách làm hai:

- **Entity linking qua KB.** Mượn từ entity linking cổ điển: alias table, sinh ứng viên, xếp
  hạng có prior, có NIL. ReFinED là mô hình khái niệm gần nhất. Prior P̂(e|m) của ReFinED
  tương ứng trực tiếp với tín hiệu "số ví gọi trực tiếp contract" đã chốt trong
  `linking-method.md` §4.
- **Schema/value linking kiểu text-to-SQL** cho phần còn lại.

Đây cũng là điểm mới có thể nêu trong luận văn: mục 4.1 cho thấy benchmark có dữ liệu
`crypto_ethereum` (Spider 2.0) đã cho sẵn địa chỉ trong câu hỏi.

### 6.2 Ánh xạ bốn đặc thù sang phương pháp

| Đặc thù (`linking-method.md` §2) | Phương pháp gợi ý | Căn cứ |
|---|---|---|
| **Một thực thể, nhiều địa chỉ** | KB trả về **tập địa chỉ**. Kết quả linking được đưa vào prompt dạng hint/evidence, ví dụ `Tornado.Cash = to_address IN ('0x…','0x…')`, giống cách CodeS chèn `district.a2 = 'Jesenik'` và E-SQL chèn predicate ứng viên. Model không phải "nhớ" địa chỉ | Hint/evidence có tác động lớn nhất trong ablation của [2501.12372](https://arxiv.org/abs/2501.12372) §1. Làm giàu câu hỏi tăng EX của 1,5B từ 11,21 lên 36,90 ([2409.16751](https://arxiv.org/abs/2409.16751), Bảng 8). Bỏ predicate ứng viên làm giảm 2,80 với 7B (Bảng 6) |
| **Nhiều alias** | Sinh ứng viên trên **bảng alias** (không phải trên giá trị DB) theo khuôn của CHESS/CHASE-SQL: chuẩn hóa chuỗi, tra LSH/BM25 theo n-gram ký tự, rồi xếp hạng bằng edit distance và embedding đa ngữ. Phân giải trùng bằng prior hoạt động (caller-count). Có lựa chọn NIL. Alias tiếng Việt có dấu và không dấu nằm sẵn trong bảng | LSH + rerank ([2405.16755](https://arxiv.org/abs/2405.16755) App. A.1; [2410.01943](https://arxiv.org/abs/2410.01943) Bảng 7). Prior và NIL ([2207.04108](https://arxiv.org/abs/2207.04108), phần entity disambiguation). *Chuẩn hóa ký tự đồng dạng (Unicode NFKC/confusables) là đề xuất kỹ thuật, không có số liệu trong các bài đã đọc* |
| **Tham chiếu theo lớp** ("any major DEX") | Mở rộng tất định: lớp → thành viên → tập địa chỉ; "major" là top-N theo hoạt động tháng 9. Đưa vào prompt như hint. Ghi rõ N trong gold SQL để câu hỏi không còn mơ hồ | Chưa có phương pháp text-to-SQL nào trong khảo sát xử lý trực tiếp kiểu này. Đây là dạng "vagueness" trong AMBROSIA ([2406.19073](https://arxiv.org/abs/2406.19073)), loại mà LLM xử lý kém |
| **Property mơ hồ** ("value", "fee") | Bảng nghĩa property đóng vai **evidence** kiểu BIRD: cụm từ → biểu thức cột, kèm điều kiện loại thực thể. Hai cách dùng: chèn nghĩa được chọn vào prompt, hoặc dùng nó để giới hạn tập cột (schema linking có điều kiện theo entity) | Bỏ EK làm giảm mạnh EX (CodeS Bảng 6: SFT CodeS-15B đạt 47,91 không EK và 58,47 có EK). Mơ hồ từ vựng tên cột ([2310.13659](https://arxiv.org/abs/2310.13659)). Dự đoán vai trò cột ([2501.17174](https://arxiv.org/abs/2501.17174)) |

### 6.3 Schema linking cho schema 6 bảng

- **Phase 1 (prompting).** Thử cả hai cấu hình, chạy như một ablation:
  - Toàn bộ schema 6 bảng (vừa context).
  - Schema bị giới hạn bởi kết quả entity/property linking.

  Lý do: bằng chứng cho model 1,5–7B nghiêng về phía lọc schema
  ([2408.07702](https://arxiv.org/abs/2408.07702), [2510.14296](https://arxiv.org/abs/2510.14296),
  [2410.12916](https://arxiv.org/abs/2410.12916)). Nhưng các thí nghiệm đó dùng schema BIRD có
  trung bình 76 cột, nên lợi ích với 6 bảng là một câu hỏi thực nghiệm. Cấu hình chỉ dùng
  CHESS-SS, RSL-SQL hay TA-SQL thì không đáng: các phương pháp này cần LLM mạnh và nhiều lượt
  gọi.
- **Phase 2 (LoRA).**
  - Huấn luyện với **cùng định dạng prompt có chèn hint** của entity/property linking và
    vài giá trị đại diện mỗi cột, theo kiểu CodeS/OmniSQL. Như vậy model học cách *dùng* hint
    thay vì học thuộc địa chỉ.
  - Với 1,5–3B, chênh lệch giữa LoRA và full fine-tune có thể đáng kể. OmniSQL thấy chênh lệch
    ở 7B; với cỡ nhỏ hơn chưa có số.
- **Đánh giá linking tách khỏi EX**, như RSL-SQL, ExSL và [2510.14296](https://arxiv.org/abs/2510.14296):
  - Recall/SRR, FPR cho cột.
  - Accuracy@1 và recall@k cho entity, có tính trường hợp NIL.
  - EX khi dùng linking **oracle** làm cận trên. DTS-SQL, ExSL và [2510.14296] đều báo cận
    trên kiểu này, và khoảng cách đến oracle là con số dễ diễn giải nhất.

### 6.4 Ràng buộc phần cứng (GTX 1650 4 GB, RAM 7 GB)

- Bước linking nên **không dùng LLM**: alias table + LSH/BM25 + một model embedding nhỏ đa ngữ
  chạy trên CPU, cộng tra KB tất định. LLM chỉ được gọi một lần để sinh SQL.
- Trích keyword bằng LLM (CHESS, CHASE-SQL, XiYan) có thể thay bằng so khớp n-gram trực tiếp
  với bảng alias, vì từ vựng thực thể là tập đóng.
- Các pipeline sinh 10–64 ứng viên (CHASE-SQL, XiYan-SQL, SLM-SQL) không thực tế trên phần
  cứng này. Self-consistency với số mẫu nhỏ (ví dụ 4–8) có thể là một biến thể để thử nếu
  thời gian cho phép.

### 6.5 Song ngữ Việt–Anh

- Không có kết quả linking song ngữ trong các nguồn đã đọc.
- Gợi ý:
  - Alias tiếng Việt đưa thẳng vào bảng alias (đã có trong thiết kế `aliases.csv`).
  - Khi tra cứu, chuẩn hóa cả dạng có dấu và không dấu.
  - Dùng embedding đa ngữ cho bước xếp hạng lại.
- Hiệu quả của các lựa chọn này cần được đo. Đây là một phần đóng góp thực nghiệm của luận văn.

---

## 7. Những điều chưa xác minh được hoặc cần lưu ý

1. **Leaderboard.** Đọc bằng WebFetch ngày 2026-11-01; bảng hiển thị lộn xộn nên thứ hạng và
   ngày có thể lệch.
   - [BIRD](https://bird-bench.github.io/): top test EX là AskData (AT&T) 83,40, nộp ngày
     2026-09-26.
   - Track "single trained model": Gemini-SQL2 đạt 80,04; SiriusAI-SQL-9B đạt 77,08.
   - [Spider 2.0](https://spider2-sql.github.io/): top Lite 76,23; top Snow 96,70.
   - Phần lớn các mục đứng đầu không có bài báo, nên không trích dẫn được phương pháp.
2. **"Distillery"** chỉ được xác định qua bảng của bài khác, không có trong chính
   [2408.07702](https://arxiv.org/abs/2408.07702).
3. **Bài "Extractor-Selector" và "schema linking is solved?"** trong đề bài: không tìm thấy bài
   nào có tên như vậy. Các bài gần nhất là ExSL ([2501.17174](https://arxiv.org/abs/2501.17174))
   và [2510.14296](https://arxiv.org/abs/2510.14296).
4. **Số CHESS khác nhau giữa các phiên bản.** v3 báo 68,31/71,10 (cấu hình IR,CG,UT) và
   65,00/66,69 (cấu hình IR,SS,CG). Các bài khác trích các cấu hình khác nhau.
5. **Recall của CHESS khác nhau giữa các nguồn.** RSL-SQL báo SRR 89,70; [2510.14296] báo
   recall 97,12; XiYan chạy lại được recall cột 74,77. Định nghĩa recall và cách tái lập khác
   nhau, nên **không so sánh trực tiếp** được.
6. **Số liệu tự báo cáo hoặc thứ cấp.**
   - XiYanSQL-QwenCoder-3B: số tự báo cáo trên model card.
   - ViR²: chỉ đọc trang tóm tắt.
   - Chi phí của CHESS (78 lượt gọi, khoảng 340K token) là số do bài khác dẫn lại.
7. **Venue.** Chưa xác minh venue của CHESS, CHASE-SQL, RSL-SQL, E-SQL, TA-SQL, AmbiQT và
   long-context. Bài long-context ghi "PVLDB Reference Format" nhưng chưa rõ volume. Các bài
   này chỉ được dẫn bằng số arXiv.
8. **Không có số liệu cho model lượng tử hóa** (4-bit, GGUF) trong text-to-SQL ở các nguồn đã
   đọc. Ảnh hưởng của lượng tử hóa lên EX cần tự đo.
9. **Số bài crypto trong Spider 2.0** (27/547) được đếm từ file trên nhánh `main` ngày
   2026-11-01, không lấy từ bài báo; con số có thể đổi theo phiên bản dữ liệu.
10. **Công trình trước 2023** (BRIDGE, ValueNet, BLINK, ReFinED, ViText2SQL) được đưa vào làm
    nền tảng khái niệm, không phải phương pháp hiện hành.
