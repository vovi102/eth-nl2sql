# Phương pháp đề xuất: schema/entity linking dựa trên KB cho NL2SQL trên Ethereum

Bản sửa ngày 2026-11-04, sau khi đã dựng database, duyệt hai đợt cơ sở tri thức (KB) và chạy phase 1 trên dev. Căn cứ:

- [Khảo sát](research-schema-entity-linking.md): các trích dẫn dạng §x.y trỏ vào tài liệu này.
- Các quy tắc và quyết định duyệt trong [linking-method.md](linking-method.md).

Mục 8 ghi tiến độ; mục 9 liệt kê các điểm **chưa chốt**.

## 1. Bài toán và ý tưởng chính

Các phương pháp value linking hiện nay (CHESS, CHASE-SQL, XiYan-SQL, CodeS) đều so khớp câu hỏi
với **giá trị đang có trong DB**. Với `crypto_ethereum`, giả định này sai theo ba cách (khảo sát
§6.1):

1. Tên sàn/protocol **không có trong DB**: DB chỉ có địa chỉ.
2. Symbol token có trong DB nhưng **trùng hàng nghìn lần**: 1.208 token mang symbol `USDT`, nhiều
   token giả dùng ký tự đồng dạng.
3. Tham chiếu theo lớp ("các DEX lớn") **không ứng với giá trị nào**.

Vì vậy linking được tách làm hai phần, và **không dùng LLM** ở bước linking:

1. **Entity linking qua KB**, theo khuôn entity linking cổ điển (ReFinED, §3.4): bảng alias → sinh
   ứng viên → xếp hạng có prior hoạt động → cho phép NIL.
2. **Property/schema linking bằng từ điển nghĩa property**, đóng vai "evidence" kiểu BIRD.

Kết quả linking được đưa vào prompt dưới dạng **evidence có placeholder** (`{E1}`). Model chỉ viết
tên placeholder, bước hậu xử lý thay bằng danh sách địa chỉ thật. Model sinh SQL (Qwen2.5-Coder
1,5B/3B) chỉ được gọi một lần.

## 2. Tài nguyên linking trong database

Mọi tài nguyên nằm trong `data/db/ethereum.duckdb`, được dựng bằng `eth-nl2sql build-db`. Schema
`link` nạp lại được bằng `load-knowledge-base` sau mỗi đợt duyệt.

| Đối tượng | Nội dung | Dùng ở bước |
|---|---|---|
| `main.*` (6 bảng) | Dữ liệu tháng 9/2026, tên và giá trị như BigQuery. Gold SQL chỉ chạm schema này | B8 (chạy SQL) |
| `link.entity_aliases` | Alias → đích (thực thể / địa chỉ / token / lớp), kèm `alias_norm` đã chuẩn hoá, ngôn ngữ, loại alias | B2 |
| `link.entities` | Thực thể đã duyệt: `canonical_id` (sau khi gộp `same_as`), `parent_id`, `kind`, `classes`, `address_set` (`closed`/`open`) | B3, B4 |
| `link.entity_addresses` | Địa chỉ của thực thể, vai trò, lớp ở mức địa chỉ, `is_contract` (bằng chứng từ BigQuery) | B3, B4, B8 |
| `link.entity_classes` | 21 lớp (dex, bridge, mixer, cex, lending, ...) với tên EN/VI | B2, B4 |
| `link.token_stats` | Mỗi token: số ví khác nhau gọi trực tiếp contract (`callers`), số lượt gọi, số transfer, `decimals` | B3 (prior), B5 (decimals) |
| `meta.columns` | Mô tả chính thức của BigQuery cho 69 cột, 2 giá trị ví dụ mỗi cột | B6, B7 |
| `link.property_senses`, `link.property_phrases` | 36 nghĩa (biểu thức SQL, loại thực thể áp dụng, đơn vị, hint cho evidence) và 115 cụm từ VI/EN trỏ tới nghĩa; nguồn `data/schema/property_*.csv` đã duyệt. Biểu thức được DuckDB bind trên bảng thật mỗi lần nạp | B5 |

Nguồn của KB: tên địa chỉ lấy từ eth-labels (dữ liệu của Etherscan, đã ghim commit), mọi thông tin
khác lấy từ BigQuery. Chỉ dòng `auto` hoặc `approved` có thực thể đích đã duyệt mới được nạp vào
`link` (`linking-method.md` §5).

## 3. Bốn đặc thù blockchain và cách giải

| Đặc thù | Cách giải | Dữ liệu |
|---|---|---|
| **Một thực thể, nhiều địa chỉ** | Thực thể ánh xạ tới **tập địa chỉ**. Chỉ hỏi ở mức cả thực thể khi tập là **đóng** (contract của một protocol, ví dụ Tornado.Cash 63 địa chỉ). Sàn có tập **mở** (ví nạp tiền sinh liên tục) nên chỉ hỏi qua **từng ví có tên** ("Coinbase 10"). Tag chung kiểu "MEV Bot: 0x…" là **nhóm** (`kind = group`): địa chỉ dùng cho lớp, tên nhóm không trỏ tới thực thể | `entities.address_set`, `entity_addresses` |
| **Nhiều alias** | Alias từ nguồn (`nameTag`, tiền tố tag, symbol/name token) cộng alias soạn tay: viết tắt (TC, CCTP), tên cũ (MakerDAO, ParaSwap), tên mô tả (the mixer), tiếng Việt (máy trộn Tornado). Có **nhập nhằng có chủ đích** giữa tên protocol/công ty và token ("Uniswap" → protocol hoặc UNI; "Tether" → công ty hoặc USDT), được phân giải ở B3. Tag trùng do Etherscan đổi số được xử lý bằng quy tắc tag duy nhất (`linking-method.md` §3, quy tắc 5) | `entity_aliases` |
| **Tham chiếu theo lớp** ("các DEX lớn") | Lớp → thành viên → tập địa chỉ. Thành viên là thực thể có lớp ở mức thực thể (duyệt tay); lớp ở mức địa chỉ (nhãn eth-labels) chỉ dùng cho thực thể nhóm. Thực thể con gộp lên cha (Uniswap V2/V3/V4 → Uniswap). Lớp chỉ chứa thành viên không gây tranh cãi (ví dụ Revolut không thuộc `cex`; L2 không thuộc `bridge`). "Lớn/major" = top-5 theo lượt được gọi trong tháng 9 | `entity_classes`, `entities.classes`, `entity_addresses.classes` |
| **Property nhập nhằng** ("giá trị", "phí") | Từ điển nghĩa property; **loại của thực thể đã link quyết định nghĩa**: token → `token_transfers.value / 10^decimals`; ETH → `transactions.value / 1e18`; contract → "số lượt gọi" là `transactions.to_address`. Entity linking và schema linking vì vậy làm cùng nhau, không tách rời | `property_senses`, `property_phrases`, `token_stats.decimals` |

## 4. Pipeline

```
câu hỏi (VI/EN)
  │ B1 Chuẩn hoá
  │ B2 Phát hiện mention + sinh ứng viên       ← link.entity_aliases
  │ B3 Phân giải (prior, kiểu, NIL)             ← link.entities, entity_addresses, token_stats
  │ B4 Mở rộng lớp ("DEX lớn" → top-N)          ← link.entity_classes + hoạt động trong main
  │ B5 Nghĩa property → cột/biểu thức           ← link.property_senses, property_phrases
  │ B6 Chọn schema                              ← meta.columns
  │ B7 Dựng prompt: schema + evidence + placeholder
  ▼
LLM (phase 1: prompting; phase 2: LoRA, cùng prompt)
  │ B8 Thay placeholder → literal, kiểm tra SQLGlot, chạy DuckDB (schema main)
  ▼
kết quả
```

| Bước | Cách làm | Căn cứ |
|---|---|---|
| B1 Chuẩn hoá | Cùng hàm với `alias_norm` của KB (`normalize_alias`): NFKC, chữ thường, bỏ dấu tiếng Việt (đ → d), dấu câu thành khoảng trắng. **Giữ** ký hiệu Unicode lạ, để token giả `꒤5DT` không bị gộp với `USDT` | §6.5; NFKC là đề xuất kỹ thuật, chưa có số liệu |
| B2 Mention + ứng viên | Regex trước cho địa chỉ `0x…` (40 hex), tx hash, số block, ngày. Sau đó duyệt n-gram 1–5 từ: khớp chính xác `alias_norm`; nếu không có thì khớp mờ (n-gram ký tự + edit distance, ví dụ `rapidfuzz`). Mention dài nhất thắng khi chồng nhau. Bỏ các từ phân loại tiếng Việt ("sàn", "ví", "đồng", "token") khi tra, nhưng giữ lại làm gợi ý kiểu cho B3 | Khuôn index rẻ + xếp hạng lại của CHESS/CHASE-SQL (§3.2), tra trên **bảng alias** thay vì giá trị DB; bỏ bước LLM trích keyword vì từ vựng thực thể là tập đóng (§6.4) |
| B3 Phân giải | Điểm = điểm khớp chuỗi + prior + điểm tương thích kiểu. Prior: token dùng log(`token_stats.callers`) (USDT thật có 3,38 triệu ví gọi, token giả 0–2); thực thể dùng hoạt động tháng 9. Kiểu lấy từ gợi ý trong câu ("token" → token; "sàn", "protocol" → thực thể). Thực thể trả về `canonical_id`. Dưới ngưỡng thì NIL | Prior P̂(e\|m) và NIL của ReFinED (§3.4); caller-count ở `linking-method.md` §4 |
| B4 Mở rộng lớp | Lớp → thực thể thành viên (gộp lên thực thể cha) → tập địa chỉ. "Lớn/major" = top-N theo số giao dịch tháng 9 có `to_address` thuộc thành viên, N = 5 nếu câu không ghi số, nêu rõ trong evidence ([linking-labels.md](linking-labels.md) §2) | Chưa có phương pháp text-to-SQL nào làm việc này; dạng "vagueness" của AMBROSIA (§3.5) |
| B5 Nghĩa property | Từ điển cụm từ → biểu thức cột, có điều kiện theo loại thực thể (mục 3, dòng 4) | Evidence BIRD: CodeS-15B 47,91 → 58,47 khi có EK (§6.2); vai trò cột của ExSL |
| B6 Chọn schema | Mặc định: toàn bộ 6 bảng (vừa context). Biến thể: chỉ các bảng mà B3/B5 chạm tới. So sánh bằng ablation | Lọc schema có lợi cho model nhỏ, nhưng thí nghiệm dùng schema BIRD lớn hơn nhiều (§6.3) |
| B7 Prompt | DDL kèm mô tả cột của BigQuery (`meta.columns`, kèm bản VI khi có), 2 giá trị ví dụ mỗi cột, khối evidence (mục 5) | Giá trị đại diện của CodeS (§3.2); làm giàu câu hỏi của E-SQL: 1,5B từ 11,21 lên 36,90 EX (§6.2) |
| B8 Hậu xử lý | Thay `{E1}` bằng `('0x…','0x…')`; parse bằng SQLGlot trong tập con DuckDB∩Postgres; chạy trên `main`; lỗi cú pháp thì cho sửa lại 1 lần | Ràng buộc đã chốt (`thesis-approach`) |

## 5. Evidence có placeholder

Ví dụ câu hỏi: *"Tornado Cash nhận bao nhiêu ETH trong tuần đầu tháng 9?"*

```
-- Evidence
-- {E1} = Tornado.Cash (protocol, mixer): tập đóng 63 địa chỉ contract. Dùng: to_address IN {E1}
-- "nhận bao nhiêu ETH" = SUM(transactions.value) / 1e18, chỉ giao dịch thành công (receipt_status = 1)
-- "tuần đầu tháng 9" = block_timestamp >= '2026-09-01' AND block_timestamp < '2026-09-08'
```

Model sinh: `SELECT SUM(value)/1e18 FROM transactions WHERE to_address IN {E1} AND ...`. B8 thay
`{E1}` bằng danh sách literal, nên SQL cuối cùng **chỉ chạm bảng `main`**, và chạy được cả trên
DuckDB lẫn BigQuery. Trên database hiện tại, câu này cho 17.112,6 ETH, chạy trong 0,3 giây.

Lý do chọn placeholder thay vì chép địa chỉ vào prompt:

- **Độ dài.** Đo bằng tokenizer của Qwen (2026-11-02): bản chép literal của prompt dài tới
  364.591 token, và 40/161 prompt dev vượt context 8192 của GPU 4 GB. Bản placeholder dài nhất
  4.440 token.
- **Độ chính xác.** Model nhỏ chép sai 1 ký tự hex thì SQL vẫn chạy nhưng sai đáp án. Lỗi này im
  lặng và khó phát hiện.
- **Tách lỗi.** Lỗi linking (chọn sai `{E1}`) và lỗi sinh SQL (dùng sai `{E1}`) đo riêng được.

Token có một địa chỉ cũng đi qua placeholder để định dạng thống nhất; evidence của token ghi kèm
`decimals` lấy từ `token_stats`. Địa chỉ người dùng gõ sẵn trong câu hỏi thì giữ nguyên dạng
literal.

Đây là đề xuất của luận văn, chưa có số liệu trong các nguồn đã đọc. Cần chạy ablation so với cách
chép literal (mục 7).

**Số đo trên dev (2026-11-01).** Placeholder trung bình 117 địa chỉ, lớn nhất 4.608
(`class_token_received`: top-5 giao thức cho vay). Bản chép literal của prompt lớn nhất dài 435
nghìn ký tự, vượt xa context của model 1,5B/3B. Vì vậy ablation 4 (mục 7) chỉ chạy được trên các
câu vừa context; số câu bị loại phải báo cáo cùng kết quả.

### Định dạng prompt (B6/B7, cài đặt 2026-11-01)

`build-prompts` nhận bản ghi linking (của bộ linking, hoặc gold khi chạy linking oracle) và ghi
mỗi câu một dòng vào `data/prompts/`. Mỗi dòng có `decision`:

- `sql`: kèm `messages` (system + user, dùng chat template của model) và `tables`.
- `unsupported` hoặc `nil`: không gọi model; `answer` báo dữ liệu không trả lời được, hoặc không
  biết tên đó.

Thành phần của prompt:

- **System:** viết một câu SQL DuckDB. Không ghi tên schema; giờ là UTC; không lọc token/thực thể
  theo symbol hay tên; không dùng QUALIFY/EXCLUDE/hàm list; chép đúng `{E1}` vào chỗ danh sách
  địa chỉ. Trả lời bằng một khối ```sql. Quy tắc về chiều `from_address`/`to_address` đã thử và
  bị bỏ (mục 8).
- **Schema:** `CREATE TABLE` của cả 6 bảng. Mỗi cột kèm mô tả của BigQuery và 2 giá trị ví dụ
  (`meta.columns`), khoảng 10,4 nghìn ký tự. Với `--schema linked` (B6), chỉ giữ bảng mà mọi nghĩa
  property cùng dùng được, khoảng 2,2–4,1 nghìn ký tự.
- **Evidence:** mỗi dòng một ý, viết bằng tiếng Anh, theo thứ tự sau:
  - **Bảng chính:** giao các bảng của mọi nghĩa property. "Người dùng của USDT" thì
    `distinct_senders` ∩ `of_token` = `token_transfers`.
  - **Mention:** tên, loại, số địa chỉ. Với lớp thì thêm N và tên các thành viên; với token thì
    thêm `decimals`.
  - **Nghĩa property:** biểu thức SQL, trong đó `{E}` đã thay bằng placeholder của mention phù
    hợp, `{decimals}` đã thay bằng số. Địa chỉ gõ sẵn trong câu thì so bằng `= '0x…'`. Nghĩa ngầm
    ghi là "(implied: …)". Sau biểu thức, trong ngoặc: đơn vị (với measure) và `evidence_hint`
    của nghĩa nếu có (cột thêm 2026-11-03, mục 8). Hiện có hai hint: `contract_called` →
    "from_address is the caller; list or count callers by from_address"; `token_amount` →
    "per transfer; SUM it for a total".
  - **Thời gian:** viết dưới dạng vị từ SQL; bảng `blocks` thì dùng cột `timestamp`.
  - **Cảnh báo tập mở**, nếu có.
- **Question:** câu hỏi gốc.

Prompt dài 10,7–11,2 nghìn ký tự. Đo bằng tokenizer của Qwen2.5-Coder, tính cả chat template
(`count-prompt-tokens`, 2026-11-02), trên 161 prompt `sql` của dev:

| Biến thể | min | median | max | Vượt 8192 − 512 |
|---|---:|---:|---:|---:|
| Mặc định (schema đủ, evidence, placeholder) | 4.253 | 4.356 | 4.440 | 0 |
| `--schema linked` | 873 | 1.426 | 1.547 | 0 |
| `--literal` | 4.213 | 4.452 | 364.591 | 40 |
| `--no-evidence` (177 câu) | 4.165 | 4.176 | 4.214 | 0 |

Trên dev, prompt dựng từ gold
và từ bộ linking chỉ khác nhau ở đoạn trích cụm property (28/177 câu). Lý do là chấm property
chỉ so `sense_id`, nên span của cụm vẫn có thể lệch.

## 6. Phase 2: LoRA

Kế hoạch chi tiết (lập 2026-11-04, chưa thực hiện): [phase2-plan.md](phase2-plan.md). Quyết
định 14–23 ở mục 9.

- **Dữ liệu.** Khoảng 30 template **train-only** mới; chữ ký mỗi variant (tập sense, loại
  mention, khung SQL) phải khác mọi template dev và test. Sinh 2.000 câu bằng bộ sinh của
  benchmark (seed 20261104), cùng tỉ lệ nhóm với benchmark, VI/EN 1:1, không có câu NIL hay
  unsupported. Dev giữ nguyên để chọn checkpoint.
- **Định dạng.** Câu hỏi đi qua **chính bộ linking**, prompt dựng bằng `PromptBuilder` với định
  dạng mục 5; đích là khối ```sql còn placeholder. Chỉ giữ câu mà bộ linking cho đúng placeholder
  và gold SQL với địa chỉ của bộ linking ra cùng kết quả (lọc L1). Nghĩa property được phép lệch.
- **Huấn luyện.** Trên Kaggle T4: chạy 1.5B trước làm pilot, rồi 3B. LoRA fp16 trên mọi lớp
  linear, r = 16, lr 2e-4, 2 epoch, chuỗi tối đa 6.144 token, dùng TRL + PEFT.
- **Đánh giá.** Adapter được chuyển sang GGUF và nạp bằng `--lora` vào **đúng file Q4_K_M của
  phase 1**; `generate-sql`/`evaluate-sql` giữ nguyên (prefix cố định, kiểm tra placeholder, hỏi
  lại). Có thêm một ablation lượng tử hoá fp16 trên dev.
- **Rò rỉ.** Kiểm tra tự động trên template, chữ ký và câu chữ, tính từ code, không đọc
  `test.jsonl`. Thực thể, token, lớp và thời gian được phép trùng với test, và EX test được báo
  cáo riêng cho thực thể đã thấy và chưa thấy khi train.

## 7. Đánh giá

**Benchmark.** Câu hỏi chỉ nhắc tới:

- thực thể có tập địa chỉ **đóng**;
- ví/contract có tên riêng;
- token;
- lớp đã duyệt.

Không có câu "tổng theo cả sàn", vì biết trước đáp án sẽ thấp hơn thực tế (`linking-method.md`
§2). Mỗi câu kèm **nhãn linking gold** (id thực thể/token/lớp, nghĩa property) để đo linking
riêng.

**Đo riêng từng tầng:**

| Tầng | Chỉ số |
|---|---|
| Entity linking | Precision/recall/F1 mức mention; accuracy@1 và recall@5 mức thực thể; accuracy trên câu NIL |
| Property/schema | Recall chặt (đủ mọi cột cần), tỉ lệ cột thừa |
| Toàn hệ thống | EX, chia theo 4 đặc thù blockchain và theo ngôn ngữ VI/EN |

**B8 và cách chấm EX (cài đặt 2026-11-01).** Dự đoán là JSON Lines
`{question_id, decision, sql}`; `sql` còn placeholder. B8 gồm các bước:

1. Lấy SQL từ khối ```sql đầu tiên, kể cả khối chưa đóng; nếu không có khối thì lấy đoạn từ
   SELECT/WITH.
2. Thay `{E1}` bằng địa chỉ của chính bản ghi linking đã dùng dựng prompt.
3. SQLGlot parse như DuckDB, phải là đúng một câu truy vấn. Việc parse được như Postgres chỉ ghi
   lại, không bắt buộc: ràng buộc tập con DuckDB ∩ Postgres chỉ áp cho gold.
4. Chạy trên kết nối chỉ đọc, tắt truy cập file, timeout 30 giây. Câu lỗi được hỏi lại một lần
   (`repair_messages`).

Quy ước chấm theo BIRD:

- So kết quả như **multiset** các dòng, không xét thứ tự. ORDER BY đã quyết định LIMIT giữ những
  dòng nào, nên câu top-k vẫn được kiểm; xếp các dòng hoà theo thứ tự khác không bị tính sai.
- Không xét tên cột và thứ tự cột. `ex` đòi đúng số cột của gold; `ex_extra_columns_ok` chấp
  nhận thêm cột.
- Số so với sai số tương đối 1e-6. Timestamp đúng nửa đêm bằng ngày tương ứng.
- Câu NIL/unsupported đúng khi `decision` khớp. Câu trả lời được mà hệ thống từ chối thì sai.

**Kiểm bộ chấm trên dev (2026-11-01).** Gold làm dự đoán cho EX 1,000. Biến đổi gold SQL rồi
chấm lại:

- **Giữ nghĩa, đều chấm đúng 161/161 hoặc trên mọi câu áp dụng được:** `SUM(value / 1e18)`, đổi
  tên mọi cột, đổi thứ tự cột, `date_trunc('day', …)` thay cho `CAST(… AS DATE)` (13 câu), bỏ
  ORDER BY khi không có LIMIT.
- **Đổi nghĩa:** sai hệ số chia bị bắt 72/72; placeholder lạ 137/137; thêm cột bị tính
  `extra_columns` 161/161.
- **Đổi nghĩa nhưng có câu vẫn ra cùng đáp án** (do dữ liệu, không phải lỗi bộ chấm):
  - bỏ `receipt_status = 1`: 24/35 câu vẫn đúng, vì không có giao dịch thất bại mang ETH trong
    khoảng đó;
  - bỏ điều kiện thời gian: 19/118, vì khoảng thời gian là cả tháng 9;
  - đổi `to_address` thành `from_address`: 2/76, vì bridge nhận vào và chuyển ra đúng bằng nhau.

  Benchmark lúc đó vì vậy chưa đủ phân biệt được việc quên điều kiện thành công. Đã xử lý bằng
  gắn nhãn khả năng phân biệt và ưu tiên mềm ([benchmark.md](benchmark.md) §6 bước 4–5): `tx_success`
  ngầm phân biệt được trên test từ 13/60 lên 46/60. Báo cáo EX có thêm phần `discriminating`.
Lỗi được phân loại thành `no_sql`, `unknown_placeholder`, `parse`, `not_a_query`, `execution`,
`timeout`, `wrong_result`, `extra_columns`, `abstained` và `wrong_decision`. Câu không có dự đoán
(`missing`) hoặc prompt quá dài bị bỏ qua (`skipped`) bị tính sai; `--only-predicted` bỏ hẳn
chúng khỏi phép chấm và báo số câu bị loại. Báo cáo EX chia theo ngôn ngữ, nhóm template và đặc
thù (lấy từ `manifest.json`).

**Sinh SQL (phase 1, cài đặt 2026-11-02).** `generate-sql` đọc một file prompt, gọi model qua
API chat tương thích OpenAI (`llama-server` của llama.cpp), rồi chạy B8:

- Câu có `decision` khác `sql` được ghi thẳng ra, không gọi model.
- Giải mã tham lam: `temperature` 0, `seed` 0, tối đa 512 token mỗi câu trả lời (gold SQL dài
  nhất trên dev 296 ký tự). Chat template là Jinja chính thức trong GGUF.
- Prompt dài hơn `n_ctx` − 512 thì không gửi, ghi `skipped` (quyết định 7).
- Câu lỗi được hỏi lại đúng một lần (`repair_messages`). Câu chạy được nhưng **không dùng
  placeholder mà evidence đưa ra** cũng bị hỏi lại (`missing_placeholder`): model đã tự viết
  địa chỉ thay cho `{E1}`. Trên dev, mọi gold SQL dùng đủ mọi placeholder của bản ghi, nên kiểm
  tra này không phạt câu đúng. Bật mặc định ở cả hai phase (quyết định 11);
  `--no-placeholder-check` là dòng ablation.
- **Prefix cố định** (quyết định 12): trước mỗi câu, cache prompt của `llama-server` được đưa
  về đúng các token mà mọi prompt trong file có chung (system + schema, 4.195 token với prompt
  mặc định). Mỗi câu vì vậy bắt đầu từ cùng một trạng thái, bất kể câu nào chạy trước. Lần sửa
  lại thì đi tiếp từ lần thử mà nó sửa.
- Mỗi câu ghi lại mọi lần thử (câu trả lời, SQL, loại lỗi, thời gian, số token). File
  `.meta.json` ghi SHA256 của GGUF, file prompt và file linking, `build_info` của server, chat
  template và tham số sinh.

**Ablation** (mỗi dòng bỏ hoặc thay một thành phần):

1. Không linking: chỉ schema + câu hỏi.
2. Linking bỏ prior hoạt động (chỉ khớp chuỗi): đo tác động lên token trùng symbol.
3. Bỏ khớp mờ / thêm embedding đa ngữ.
4. Chép literal địa chỉ thay vì placeholder.
5. Toàn bộ schema so với schema đã lọc.
6. **Linking oracle** (gold): cận trên. Khoảng cách oracle − pipeline là con số chính để nói
   linking còn thiếu bao nhiêu.

Mỗi cấu hình chạy cho cả phase 1 và phase 2. Ngoại lệ (quyết định 22): phase 2 không chạy
`--no-evidence`, `--literal` và `--schema linked`, vì adapter chỉ được train trên định dạng mặc
định.

## 8. Tiến độ (2026-11-08)

| Hạng mục | Trạng thái |
|---|---|
| Database `main` (6 bảng, 160 triệu dòng), kiểm chứng khớp BigQuery | Xong |
| Schema `meta`: mô tả cột BigQuery + 2 giá trị ví dụ | Xong; còn thiếu bản tiếng Việt và từ đồng nghĩa |
| KB: quy tắc nhận nhãn, sinh nháp, kiểm tra (`draft-entities`, `check-entities`) | Xong |
| KB đã duyệt | 169/1.080 thực thể (4 đợt; đợt 2 duyệt tạm thời; đợt 3–4 bổ sung thành viên các lớp được hỏi và thực thể hoạt động cao cho benchmark), 21 lớp, 43.717 địa chỉ, 47.533 alias (số dòng trong `link`) |
| `link.token_stats` (prior cho token) | Xong (95.473 token) |
| `property_senses` | Xong: 36 nghĩa, 115 cụm từ, đã duyệt và nạp vào `link`; thêm cột `evidence_hint` (2026-11-03, 2 nghĩa; người dùng duyệt câu chữ 2026-11-04) |
| Bộ linking B1–B5 | Xong (`link-question`, `evaluate-linking`; `src/eth_nl2sql/entity_linking*.py`): khớp 8 ví dụ của [linking-labels.md](linking-labels.md) §7. Dev (177 câu), sau khi sửa theo lỗi thấy trên dev nên lạc quan: mention F1 chặt 1,000 (ban đầu 0,944), accuracy@1 1,000 (0,942), property F1 0,998 (0,965). Test (328 câu, chạy một lần ở S4, 2026-11-06; `data/linking/test.report.json`): mention F1 chặt 0,982 (P 0,974, R 0,990), accuracy@1 0,990 (287/290), recall@5 0,997, NIL 1,000 (16/16), lớp 1,000 (42/42), property F1 0,967, thời gian 1,000 |
| Chọn schema B6, dựng prompt B7 | Xong (`build-prompts`, `src/eth_nl2sql/prompt_builder.py`), định dạng ở mục 5. Trên dev, mọi vị từ WHERE của gold SQL có nguyên văn trong evidence (có test) |
| Hậu xử lý B8, bộ chấm EX | Xong (`src/eth_nl2sql/sql_execution.py`, `execution_evaluation.py`, lệnh `evaluate-sql`), quy ước ở mục 7. Tự kiểm: gold làm dự đoán cho EX 1,000 trên dev (177/177) |
| Runtime model local | Xong: llama.cpp b12032 (commit `dde879b`) tự biên dịch với CUDA 12.8 cho GTX 1650 (sm_75), vì bản build sẵn cần glibc 2.38; Qwen2.5-Coder 1.5B/3B Instruct GGUF Q4_K_M chính thức, đã ghim revision và SHA256 (README). `n_ctx` 8192, mọi layer trên GPU, flash attention; 3B dùng khoảng 2,3 GB VRAM |
| Sinh SQL + B8 (`generate-sql`, `src/eth_nl2sql/sql_generation.py`) | Xong; client tách riêng để phase 2 dùng lại. Kết quả dev ở bảng dưới |
| Định dạng nhãn linking (gold và đầu ra bộ linking) | Xong: [linking-labels.md](linking-labels.md) |
| Benchmark (template, nhãn gold) | Xong: 505 câu, 40 template đã duyệt, dev 177 / test 328 chia theo template (`data/benchmark/`, [benchmark.md](benchmark.md)); tập 169 thực thể sau đợt duyệt 3–4; sinh lại 2026-11-03 để bỏ câu hoà ở `LIMIT` (thay 4 câu dev, 2 câu test) |
| Phase 2 (LoRA) | Đã lập kế hoạch và chốt quyết định 14–23 (2026-11-04, [phase2-plan.md](phase2-plan.md)). S0 xong: phase 1 được chốt trong `data/predictions/phase1-freeze.json` (SHA256 của KB, model, code, prompt/linking và 7 lần chạy dev). `property_senses.csv` đã đổi lúc 2026-11-04 19:53, sau benchmark và prompt dev, nhưng chỉ ở các cột không nạp vào `link`: link lại dev và dựng lại 5 file prompt dev cho file giống hệt từng byte. S1 xong, người dùng duyệt 2026-11-05 (quyết định 24–27): 30 template train-only (`train_templates.py`) và bộ kiểm tra rò rỉ (`template_signature.py`), 0 vi phạm ([phase2-plan.md](phase2-plan.md) §2.1, §2.5). S2 xong (2026-11-05): `data/train/`, 1.968 ví dụ (98 val), 4.277–4.624 token mỗi ví dụ, 20 thực thể giữ riêng không bị nêu tên ([phase2-plan.md](phase2-plan.md) §2.6). S3 xong (2026-11-05 22:42): đóng băng dữ liệu train và cấu hình huấn luyện (`data/train/freeze.json`). S4 xong (2026-11-06): test phase 1, kết quả ở bảng dưới. S5 xong (2026-11-08, sau khi thấy test phase 1): LoRA cho 1.5B trên Kaggle (2 epoch, 4,46 giờ trên một T4), chọn checkpoint `1.5b-e2`, EX dev 0,960 (kết quả ở dưới; [phase2-plan.md](phase2-plan.md) §4, §5.6). S6 xong (2026-11-08): LoRA cho 3B (7,79 giờ trên một T4), chọn `3b-e2`, EX dev 0,977 ([phase2-plan.md](phase2-plan.md) §5.7). S7 xong (2026-11-08): ablation lượng tử hoá trên dev ([phase2-plan.md](phase2-plan.md) §5.4). Chưa chạy S8 (test phase 2) |

**Kết quả phase 1 trên dev (2026-11-04).** Benchmark sau khi bỏ câu hoà ở `LIMIT`
([benchmark.md](benchmark.md) §6, bước 6). Zero-shot, giải mã tham lam, kiểm tra placeholder và
prefix cố định. Dev đã được nhìn khi sửa linking, khi chọn quyết định 11–13 và khi viết hai
`evidence_hint`, nên các số này lạc quan; số khách quan chỉ có ở test.

| Cấu hình | EX (177) | EX câu trả lời được (161) | Giây/câu `sql` | Hỏi lại / sửa được |
|---|---|---|---|---|
| **3B, mặc định** (linking dự đoán, schema đủ) | **0,898** | **0,888** | 4,2 | 36 / 25 |
| 3B, `--no-placeholder-check` | 0,768 | 0,745 | 3,7 | 10 / 0 |
| 3B, `--schema linked` (B6) | 0,904 | 0,894 | 8,2 | 34 / 27 |
| 3B, linking oracle (gold) | 0,881 | 0,870 | 4,1 | 35 / 21 |
| 3B, `--no-evidence` | 0,090 | 0,099 | 5,5 | 75 / 22 |
| 3B, `--literal` (137 câu vừa context, bỏ 40) | 112/137 | 96/121 | 8,4 | 17 / 0 |
| 3B mặc định, trên đúng 137 câu đó | 123/137 | | | |
| 1.5B, mặc định | 0,599 | 0,559 | 3,3 | 89 / 25 |

Các bước dẫn tới cấu hình mặc định (3B, cùng linking dự đoán; các dòng trước dòng cuối tính
trên benchmark cũ, còn 4 câu hoà):

| Bước | EX (177) |
|---|---|
| Lần chạy đầu (cache thường, không kiểm tra placeholder) | 0,729 |
| + kiểm tra placeholder | 0,825 |
| + prefix cố định (không đổi EX, chỉ làm kết quả tất định) | 0,825 |
| + quy tắc from/to trong system prompt (hai cách viết, **bỏ**) | 0,718 / 0,780 |
| + `evidence_hint` lần 1: `contract_called` "from_address is the caller", `token_amount` "per transfer; SUM it for a total" | 0,847 |
| + `evidence_hint` lần 2 cho `contract_called`: "from_address is the caller; list or count callers by from_address" | 0,887 |
| Benchmark bỏ 4 câu hoà (4 câu `entity_top_callers` bốc lại; câu trả lời của 173 câu còn lại không đổi) | **0,898** |

- **Kiểm tra placeholder.** Không có nó, model hay tự viết địa chỉ thay cho `{E1}` (địa chỉ USDT
  lấy từ ví dụ giá trị của cột `token_address`, hoặc địa chỉ bịa), hoặc lọc theo
  `name`/`symbol`; EX giảm 23 câu.
- **Quy tắc from/to trong system prompt bị bỏ.** Hai cách viết (nguyên văn ở
  `data/predictions/rejected-direction-rule/RULES.txt`) sửa được 3–5 câu `entity_top_callers`.
  Nhưng chúng làm số câu tự viết địa chỉ thay cho `{E1}` tăng từ 21 lên 70–78, và làm sai thêm
  các template khác. Có vẻ model 3B rất nhạy với một dòng nói về địa chỉ trong system prompt.
- **`evidence_hint`.** Ghi chú gắn vào nghĩa property chỉ hiện trong câu có nghĩa đó, nên không
  ảnh hưởng tới câu khác. Hint của `token_amount` sửa hết lỗi thiếu `SUM`
  (`address_token_received` từ 3/13 lên 6/13). Hint lần 1 của `contract_called` gần như không
  có tác dụng (1/14 → 2/14). Hint lần 2 đưa `entity_top_callers` lên 11/14: cả 14 câu đều nhóm
  theo `from_address`. Đổi lại, 2 câu "tương tác với X" của `entity_distinct_callers` chuyển
  sang lọc `from_address IN {E1}`.
- **Độ nhạy với cách viết prompt.** Prompt oracle và prompt của bộ linking chỉ khác nhau ở đoạn
  trích cụm property, vậy mà 9 câu đổi kết quả (3 câu tốt lên, 6 câu xấu đi). Các chênh lệch
  0,881 / 0,898 / 0,904 giữa oracle, mặc định và `--schema linked` vì vậy nằm trong mức dao động
  này, chưa nói được cấu hình nào tốt hơn. Linking trên dev gần như đúng hết (mục 8, bảng trên),
  nên khoảng cách oracle − pipeline chỉ có nghĩa trên test.
- **Literal.** Trên 137 câu có prompt vừa context, chép địa chỉ vào prompt cho 112 câu đúng,
  placeholder cho 123. 40 câu bị loại là các câu có lớp (28) hoặc thực thể nhiều địa chỉ (12).
  Không có placeholder thì kiểm tra placeholder không có tác dụng: 17 câu phải hỏi lại, không câu
  nào sửa được.
- **Không evidence.** EX 0,090. Model không biết địa chỉ nào ứng với tên nào, và không có bước
  từ chối, nên 16 câu không trả lời được đều sai.
- **1.5B** đạt 0,599. Lỗi chủ yếu là `execution` (49 câu); `class_eth_received` 0/14,
  `entity_top_callers` 3/14.
- **18 câu sai còn lại của 3B mặc định:**
  - `address_token_received` 7: đổi chỗ `{E1}`/`{E2}`, tự viết địa chỉ, hoặc lọc theo
    `symbol`/`name`;
  - `entity_distinct_callers` 3 (lọc `from_address IN {E1}`), `class_token_received` 3,
    `ambiguous_value_received` 2, `entity_fees_paid` 1, `entity_top_callers` 1 (SQL không parse
    được), `token_distinct_senders` 1.
- **Câu hoà ở `LIMIT` đã bỏ khỏi benchmark.** Trên benchmark cũ, 4/26 câu top-k của dev có hoà
  ở hạng k. Gold phá hoà bằng `from_address ASC`, nhưng câu hỏi không nói điều đó. SQL của model
  không phá hoà, nên DuckDB (chạy song song) trả về một dòng tuỳ ý ở hạng k. Cùng một SQL có thể
  lúc được chấm đúng, lúc bị chấm sai: chấm lại hai lần chạy có câu trả lời giống hệt nhau thì
  q00036 và q00037 đổi kết quả. Người dùng chọn sửa benchmark (2026-11-03): các câu này được bốc
  lại thành câu không hoà ([benchmark.md](benchmark.md) §6, bước 6).
- **Tính tất định.** Lần chạy đầu (cache thường) cho 6/161 câu trả lời đầu khác nhau giữa hai
  lần chạy. Với prefix cố định, chạy lại, chạy bản không kiểm tra và chạy theo thứ tự đảo
  ngược đều cho câu trả lời giống hệt nhau. `llama-server` chạy với `--cache-ram 0`: cache prompt
  trên RAM (mặc định tới 8 GB) làm máy 7 GB bị OOM; tắt nó không đổi kết quả.
- **Thời gian.** 3B mất khoảng 4 s/câu với schema đủ, vì 4.195 token prefix nằm sẵn trong cache.
  `--schema linked` có prompt ngắn hơn, nhưng prefix chung chỉ là system prompt, nên mỗi câu
  phải xử lý khoảng 1,4 nghìn token và mất 8,2 s.

**Kết quả phase 1 trên test (2026-11-06, S4).** Chạy một lần bằng `scripts/phase1_test.sh`
(233 phút, không bước nào lỗi), sau khi đóng băng phase 2 ở S3. Trước khi đọc test, script chạy
lại dev với cấu hình mặc định: 177/177 câu trả lời giống hệt bản đã chốt. Mọi tham số như trên
dev. Nguồn: `data/predictions/test-phase1.summary.json`, các file `test.*.ex.json`,
`test.*.ex-details.jsonl` và `test.*.jsonl.meta.json`. Kiểm định McNemar chính xác (hai phía)
tính trên từng câu.

| Cấu hình | EX test (328) | EX câu trả lời được (296) | Giây/câu `sql` | Hỏi lại / sửa được | EX dev (177) |
|---|---|---|---|---|---|
| **3B, mặc định** | **0,768** | **0,743** | 4,7 | 63 / 43 | 0,898 |
| 3B, `--no-placeholder-check` | 0,665 | 0,628 | 4,3 | 21 / 5 | 0,768 |
| 3B, `--schema linked` (B6) | 0,802 | 0,780 | 9,7 | 54 / 39 | 0,904 |
| 3B, linking oracle (gold) | 0,790 | 0,767 | 4,6 | 59 / 46 | 0,881 |
| 3B, `--no-evidence` | 0,015 | 0,017 | 6,0 | 143 / 42 | 0,090 |
| 3B, `--literal` (261 câu vừa context, bỏ 67) | 180/261 | 148/229 | 9,7 | 34 / 3 | 112/137 |
| 3B mặc định, trên đúng 261 câu đó | 198/261 | 166/229 | | | 123/137 |
| 1.5B, mặc định | 0,613 | 0,571 | 3,4 | 117 / 35 | 0,599 |

Chia theo nhóm, ngôn ngữ và `discriminating` (số câu đúng / tổng):

| Phần | 3B mặc định | 3B oracle | 1.5B |
|---|---|---|---|
| entity | 62/70 | 64/70 | 45/70 |
| address | 23/39 | 23/39 | 26/39 |
| token | 31/52 | 32/52 | 22/52 |
| class | 29/42 | 31/42 | 12/42 |
| ambiguous | 35/45 | 37/45 | 27/45 |
| plain | 40/48 | 40/48 | 37/48 |
| nil, unsupported | 32/32 | 32/32 | 32/32 |
| VI | 127/169 | 129/169 | 90/169 |
| EN | 125/159 | 130/159 | 111/159 |
| phân biệt được: hướng | 140/185 | 145/185 | 104/185 |
| phân biệt được: trạng thái | 61/72 | 65/72 | 65/72 |
| phân biệt được: thời gian | 150/195 | 153/195 | 116/195 |

- **3B trên test thấp hơn dev 0,13.** Có hai lý do. Thứ nhất, dev đã được nhìn khi chỉnh nên lạc
  quan. Thứ hai, test có những template cần kỹ năng mà dev không có. Theo cách chia gần/xa của
  phase 2 ([phase2-plan.md](phase2-plan.md) §5.5), 13 template gần (180 câu) đạt 157 (0,872),
  gần mức dev, còn 9 template xa (116 câu) chỉ đạt 63 (0,543). Oracle: 158 và 69; 1.5B: 117 và
  52. 1.5B gần như không đổi (dev 0,599, test 0,613); mọi bước chỉnh trên dev đều đo bằng 3B.
- **Oracle − pipeline = 0,021** (7 câu: oracle đúng mà pipeline sai 9 câu, ngược lại 2 câu;
  p = 0,065). Cả 9 câu đều có linking khác gold. 6/9 câu do hai lỗi linking:
  - Câu EN của `class_top_contracts` kết thúc bằng "… called the most? Rank by transaction
    count.". Bộ linking đọc chữ "Rank" viết hoa đầu câu thành tên riêng không có trong KB
    (mention NIL), nên hệ thống từ chối 5 câu trả lời được; oracle đúng 3 trong 5 câu đó. Đây là
    cùng loại lỗi với "Name" và "Xếp" thấy ở S1 ([phase2-plan.md](phase2-plan.md) §2.5). NIL
    accuracy vẫn là 1,000 vì chỉ tính trên câu NIL thật; lỗi này làm giảm precision của mention
    (0,974).
  - "reverted transactions" (`entity_failed_calls`): thiếu nghĩa `tx_count` ở 5/14 câu. 3 câu
    trong số đó model đếm `COUNT(DISTINCT from_address)`.

  Một lỗi linking khác không làm đổi EX: với "Lượt chuyển X lớn nhất" (`token_largest_transfer`),
  bộ linking thừa nghĩa `transfer_count` ở 13/13 câu, nhưng oracle cũng đúng 0/13.

  Chia theo việc nghĩa property của bộ linking có khớp gold không: trên 259 câu khớp, 3B đạt
  0,803 và oracle 0,807; trên 37 câu lệch, 3B đạt 0,324 và oracle 0,486.
- **Kiểm tra placeholder** sửa được 34 câu và không làm sai câu nào (0,665 → 0,768;
  p < 0,0001). Trên dev con số này là 23 câu.
- **`--schema linked` cao hơn mặc định 11 câu** (20 câu tốt lên, 9 câu xấu đi; p = 0,061), chưa
  đủ để kết luận. Cấu hình mặc định đã đóng băng nên giữ nguyên. 6/20 câu tốt lên là
  `address_eth_received`. Với schema đủ, 3B (cả bản oracle) viết
  `to_address IN (SELECT address FROM contracts WHERE name = '…')` cho tên kiểu "Uniswap V2:
  Router 2". Bảng `contracts` không có cột `name`, và lần hỏi lại lặp đúng câu cũ. Schema rút
  gọn không có bảng `contracts` nên lỗi này không xảy ra. 1.5B không mắc lỗi này (13/13).
- **Template yếu nhất của 3B mặc định:**
  - template xa:
    - `token_largest_transfer` 0/13 (0/13 ở mọi cấu hình);
    - `address_largest_eth_sent` 3/13;
    - `class_top_contracts` 3/14 (5 câu bị từ chối vì "Rank");
    - `daily_avg_gas_price` 4/12. 8 câu sai đều thuộc kiểu "giá gas trung bình mỗi ngày" /
      "average gas price per day". 7 câu có `GROUP BY` theo ngày nhưng không chọn cột ngày, nên
      kết quả thiếu một cột so với gold; 1 câu không nhóm theo ngày. Các câu kiểu "theo ngày" /
      "daily" đều đúng;
    - `token_top_receivers` 5/13;
  - template gần:
    - `address_eth_received` 7/13 (lỗi `contracts.name` ở trên);
    - `ambiguous_users` 8/15 (7 câu "người dùng của X" lọc `from_address IN {E1}` thay vì
      `to_address`).
- **Lỗi gold ở hai template "lớn nhất".** Gold của `address_largest_eth_sent` và
  `token_largest_transfer` trả về mã giao dịch (`hash`, hoặc `transaction_hash` + `log_index`)
  kèm số lượng. 16/26 câu hỏi "giao dịch nào", nên với các câu này gold đúng. 10 câu còn lại chỉ
  hỏi số lượng ("What was the largest ETH amount …", "… chuyển bao nhiêu lượng token?"). Vì `ex`
  đòi đủ các cột của gold, một câu trả lời chỉ có đúng số lượng vẫn bị chấm sai. Đo phụ: chạy
  lại SQL dự đoán và xem kết quả có một cột bằng số lượng của gold không. 3B mặc định đúng thêm
  5/10 câu (EX sẽ là 0,784); oracle thêm 4, `--schema linked` thêm 5, 1.5B thêm 5. Benchmark
  giữ nguyên; số chính thức là EX ở bảng trên. Dev không có template "lớn nhất", nên lỗi này
  không lộ ra trước test.
- **Literal.** 67 câu bị bỏ vì prompt vượt context (37 câu theo lớp, 25 thực thể, 5 nhập nhằng).
  Trên 261 câu còn lại, literal đúng 180 câu, placeholder đúng 198 câu. Có 23 câu literal lỗi
  parse.
- **Không evidence.** EX 0,015, như trên dev: model không biết địa chỉ nào ứng với tên nào.
- **1.5B** đạt 0,613. Khoảng cách giữa hai ngôn ngữ lớn: VI 90/169 (0,533), EN 111/159 (0,698);
  với 3B là 127/169 và 125/159. Trong số câu sai, 58 câu sai kết quả và 54 câu lỗi `execution`
  (3B chỉ có 13 câu lỗi `execution`). Nhóm lớp chỉ đúng 12/42.
- Theo [phase2-plan.md](phase2-plan.md) §7, mọi thay đổi kế hoạch phase 2 từ đây phải ghi rõ là
  "sau khi thấy test phase 1".

**Kết quả phase 2 trên dev (S5 và S6, 2026-11-08, sau khi thấy test phase 1).** LoRA r16 trên
Qwen2.5-Coder-1.5B-Instruct và 3B-Instruct, train 2 epoch trên 1.870 câu của `data/train/` bằng
một T4 của Kaggle (1.5B: 65,9 s/bước, 4,46 giờ; 3B: 116,0 s/bước, 7,79 giờ). Adapter được
chuyển sang GGUF và chạy bằng `llama-server --lora` trên đúng file Q4_K_M của phase 1. Prompt,
bộ linking và `generate-sql` giữ nguyên.

Trước khi chấm, adapter của mỗi model được nạp ở hệ số 0: cả 177/177 câu trả lời giống hệt phase
1 của model đó. Vậy mọi khác biệt ở dưới đến từ adapter, không từ runtime.

Nguồn: `data/predictions/dev-phase2-{1.5b,3b}.summary.json` và các file
`dev.*.{1.5b,3b}-{e1,e2}.ex.json` (**đo**); chi tiết ở [phase2-plan.md](phase2-plan.md) §5.6,
§5.7.

| Cấu hình | EX dev (177) | Template gần (123) | Template xa (38) | VI / EN | Giây/câu `sql` |
|---|---|---|---|---|---|
| 1.5B phase 1 | 0,599 | 67 | 23 | 50/91, 56/86 | 3,3 |
| 1.5B + LoRA, epoch 1 | 0,955 | 123 | 30 | 83/91, 86/86 | 2,6 |
| **1.5B + LoRA, epoch 2 (chọn)** | **0,960** | 123 | 31 | 84/91, 86/86 | 2,6 |
| 3B phase 1 | 0,898 | 106 | 37 | 81/91, 78/86 | 4,2 |
| 3B + LoRA, epoch 1 | 0,977 | 123 | 34 | 87/91, 86/86 | 4,3 |
| **3B + LoRA, epoch 2 (chọn)** | **0,977** | 123 | 34 | 87/91, 86/86 | 4,2 |

- **1.5B:** so với 1.5B phase 1, e2 đúng thêm 64 câu và không sai thêm câu nào (McNemar
  p ≈ 1e-19). So với 3B phase 1, e2 đúng thêm 18 câu và sai 7 câu mà 3B đúng (p = 0,043).
- **3B:** e1 và e2 hoà (173), nên chọn e2 theo luật đã chốt. So với 3B phase 1, e2 đúng thêm 18
  câu và sai 4 câu (p = 0,0043). So với 1.5B + e2: 4 câu tốt hơn, 1 câu xấu hơn (p = 0,375). Mức
  chênh +3 câu nằm trong ngưỡng ±9 câu của tiêu chí dừng, nên trên dev chưa phân biệt được hai
  model sau fine-tune.
- Con số dev này lạc quan:
  - template train được viết khi đã thấy dev (biến thể gần được phép, quyết định 14);
  - dev được dùng để chọn checkpoint;
  - mức tăng nằm gần như hết ở 9 template dev có template train gần (1.5B: 67 → 123/123; 3B:
    106 → 123/123).

  Phép đo đáng tin là test (S8).
- 7 câu 1.5B + e2 còn sai đều là câu tiếng Việt thuộc hai template xa.
  - 4 câu "Liệt kê 5 ví gọi X nhiều nhất …" nhóm theo `token_address`: model chép khung SQL của
    template train `entity_top_tokens_received` ("Liệt kê 5 token mà X nhận nhiều nhất …").
  - 3 câu "5 fee recipient nào … nhiều nhất" dùng `LIMIT 1`, giống phase 1.

  3B phase 1 đúng cả 7 câu.
- 4 câu 3B + e2 còn sai cũng là câu "Liệt kê 5 ví gọi X nhiều nhất …" tiếng Việt. Model nhóm theo
  `to_address`, trong khi 3B phase 1 đúng cả 4 câu. Đây là các câu duy nhất 3B xấu đi so với phase
  1: ở template xa này, adapter làm mất một kỹ năng mà model gốc đã có. Template xa của 3B vì vậy
  giảm từ 37 xuống 34/38.
- 20 thực thể giữ riêng khỏi train: 19/19 câu dev nhắc tới chúng đều đúng với cả hai model
  (phase 1: 1.5B 7/19, 3B 17/19).
- Không còn câu nào thiếu placeholder ở lần thử đầu (phase 1: 1.5B 40 câu, 3B 26 câu), nên hỏi
  lại gần như không còn tác dụng.

**Ablation lượng tử hoá (S7, 2026-11-08, sau khi thấy test phase 1).** 3B fp16 (transformers trên
một T4 của Kaggle) so với Q4_K_M (llama-server ở local). Cả hai dùng cùng prompt và cùng B8; dev,
một lần thử, không kiểm tra placeholder. Nguồn: `data/predictions/dev-phase2-quant.summary.json`
(**đo**); chi tiết ở [phase2-plan.md](phase2-plan.md) §5.4.

| | Q4_K_M | fp16 |
|---|---|---|
| 3B base | 136 (0,768) | 94 (0,531) |
| 3B + `3b-e2` | 173 (0,977) | 168 (0,949) |

- Với base, Q4_K_M đúng nhiều hơn fp16 42 câu (p < 0,0001). fp16 base viết địa chỉ cụ thể thay
  cho placeholder ở 72/161 câu, Q4_K_M ở 36 câu; 36 trong 44 câu fp16 sai thêm thuộc kiểu này.
  Giả thuyết, chưa kiểm: prompt phase 1 được chỉnh trên dev bằng chính 3B Q4_K_M.
- Với adapter, hai runtime cho cùng SQL ở 155/161 câu. 5 câu chênh (p = 0,063) đều thuộc template
  xa `entity_top_callers`, nơi fp16 + LoRA nhóm theo `to_address` cả ở câu tiếng Anh.
- Vậy chạy adapter học trên fp16 bằng runtime Q4_K_M không làm mất EX trên dev. Lỗi mà adapter
  đưa vào template xa có thật ở fp16 (5/14 câu đúng) và Q4_K_M chỉ che bớt (10/14).

## 9. Các quyết định (chốt ngày 2026-11-01, bổ sung 2026-11-03 và 2026-11-04)

| # | Điểm | Quyết định |
|---|---|---|
| 1 | Đưa kết quả linking vào SQL | **Placeholder** (`{E1}`), B8 thay bằng literal. Chép literal địa chỉ vào prompt chỉ chạy trong ablation (mục 7, dòng 4). Không JOIN bảng `link` trong SQL |
| 2 | `property_senses` | Claude soạn nháp (khoảng 30 nghĩa, kèm cụm từ VI/EN), người dùng duyệt, giống cách duyệt KB |
| 3 | "Lớn/major" | **N = 5** cố định, xếp hạng theo số giao dịch trong tháng 9, nêu rõ trong evidence. Benchmark có cả câu ghi số rõ ("5 DEX lớn nhất") và câu mơ hồ ("các DEX lớn") |
| 4 | Thực thể có tập địa chỉ mở khi suy luận | **Trả lời kèm cảnh báo**: chỉ tính các ví đã biết, kết quả là cận dưới. Benchmark không có loại câu này |
| 5 | Embedding đa ngữ | Không có trong cấu hình chính; chỉ chạy ablation (mục 7, dòng 3). Đưa vào cấu hình chính nếu ablation cho thấy tăng rõ |
| 6 | Số thực thể cần duyệt | Khoảng **150**: top theo hoạt động, cộng mọi thành viên của các lớp sẽ hỏi. Tập này chốt cùng lúc với thiết kế template benchmark; thực thể ngoài tập trả về NIL |
| 7 | Ablation chép literal (mục 7, dòng 4) | Chỉ chạy trên các câu có prompt vừa context; báo cáo số câu bị loại cùng kết quả. Với `n_ctx` 8192 trên dev: 121/161 câu vừa, bị loại 40 (`generate-sql` ghi `skipped`, chấm bằng `evaluate-sql --only-predicted`) |
| 8 | Ngôn ngữ của system và evidence | Tiếng Anh, câu hỏi giữ nguyên VI/EN. Evidence tiếng Việt chỉ là ablation nếu còn thời gian |
| 9 | Zero-shot hay few-shot | Phase 1 chạy zero-shot trước; few-shot (ví dụ lấy từ dev) là cấu hình phụ |
| 10 | Độ dài prompt | Đo bằng tokenizer của Qwen khi dựng runtime; quá dài thì bỏ ví dụ giá trị của các cột hash trước. Đã đo (2026-11-02): dài nhất 4.440 token, vừa `n_ctx` 8192, nên chưa bỏ |
| 11 | Kiểm tra placeholder (2026-11-03) | Bật mặc định ở cả hai phase: câu chạy được nhưng không dùng placeholder mà evidence đưa ra thì hỏi lại một lần. `--no-placeholder-check` là một dòng ablation |
| 12 | Cache prompt (2026-11-03) | **Prefix cố định**: trước mỗi câu, đưa cache về đúng phần chung của mọi prompt trong file. Giữ tốc độ của cache (4–5 s/câu) mà kết quả không phụ thuộc thứ tự câu |
| 13 | Lỗi chiều gửi/nhận và thiếu `SUM` (2026-11-03) | Không thêm quy tắc vào system prompt: đã thử, làm EX giảm. Người dùng chọn sửa qua `property_senses`: thêm cột `evidence_hint` (ghi chú tiếng Anh sau biểu thức trong evidence) cho `contract_called` và `token_amount`; nhãn gold, bộ linking và template không đổi. Cách viết hint đã chọn sau 2 lần thử trên dev |
| 14 | Nguồn dữ liệu train phase 2 (2026-11-04) | **Template train-only mới** (khoảng 30, người dùng duyệt), chữ ký variant khác mọi template dev/test; variant chỉ khác chiều/trạng thái/loại mention thì đánh dấu `near_dev`/`near_test` và báo cáo. Dev giữ nguyên để chọn checkpoint ([phase2-plan.md](phase2-plan.md) §2.1) |
| 15 | Trùng thực thể/token/lớp/thời gian giữa train và test (2026-11-04) | Cho phép; chặn trùng câu hỏi và chữ ký. Lúc đánh giá cuối, chia EX test theo thực thể đã thấy/chưa thấy khi train |
| 16 | Kích thước và thành phần dữ liệu train (2026-11-04) | 2.000 câu trước khi lọc; tỉ lệ nhóm theo benchmark (bỏ NIL/unsupported), VI/EN 1:1; **không** đưa câu NIL/unsupported vào train (model không được gọi với các câu này) |
| 17 | Nhiễu linking trong dữ liệu train (2026-11-04) | **L1**: giữ câu khi bộ linking ra `sql`, placeholder trùng gold (đích, thứ tự, `top_n`) và gold SQL với địa chỉ của bộ linking ra cùng kết quả; nghĩa property được phép lệch. Không sửa từ điển hay KB cho phase 2 |
| 18 | Model và huấn luyện (2026-11-04) | 1.5B trước (pilot), rồi 3B. LoRA trên fp16 (QLoRA nếu OOM), mọi lớp linear, r = 16, α = 32, dropout 0,05, lr 2e-4 cosine, warmup 5%, 2 epoch, batch hiệu dụng 16, chuỗi 6.144; TRL + PEFT ghim phiên bản, trên Kaggle T4 |
| 19 | Định dạng đích (2026-11-04) | Chỉ khối ```sql còn placeholder, như phase 1; không có ví dụ sửa lỗi. Vòng hỏi lại lúc suy luận giữ nguyên |
| 20 | Cách đánh giá phase 2 (2026-11-04) | **E1**: adapter → GGUF (`convert_lora_to_gguf.py`), `llama-server --lora` trên đúng file Q4_K_M của phase 1, cùng `generate-sql`/`evaluate-sql`. Kiểm tra trước: adapter hệ số 0 phải cho câu trả lời giống hệt phase 1. Ablation lượng tử hoá: fp16 (transformers, Kaggle) so với Q4_K_M, cùng tắt hỏi lại, chỉ trên dev |
| 21 | Thứ tự chạy test (2026-11-04) | Chốt phase 1; viết, duyệt và **đóng băng** dữ liệu train và cấu hình phase 2 (ghi SHA256); rồi chạy test phase 1 một lần, trước khi huấn luyện. Mọi thay đổi sau đó phải ghi là "sau khi thấy test phase 1" |
| 22 | Ablation của phase 2 (2026-11-04) | **R1**: mặc định (1.5B, 3B), linking oracle, `--no-placeholder-check`, lượng tử hoá (dev). Không chạy `--no-evidence`/`--literal`/`--schema linked` cho phase 2 |
| 23 | Tiêu chí dừng (2026-11-04) | Mỗi model tối đa 2 lần huấn luyện đầy đủ; lần 2 chỉ sửa lỗi đường ống/dữ liệu đã chứng minh, không chỉnh siêu tham số theo dev. Chênh lệch dưới khoảng ±9 câu dev không tính là khác biệt. Chọn checkpoint bằng EX dev giữa epoch 1 và 2 (bằng nhau thì chọn epoch 2). Báo cáo kể cả khi kết quả âm |
| 24 | Danh mục template train (2026-11-05) | Duyệt 30 template, 2.000 câu ([phase2-plan.md](phase2-plan.md) §2.1); sang S2 |
| 25 | Quy tắc kiểm tra rò rỉ (2026-11-05) | Quy tắc chặt: ngoài trùng id, trùng chữ ký và trùng câu chữ, còn là vi phạm khi cùng mention và cùng tập sense nhưng khác SQL, khi cùng mention và cùng SQL nhưng khác sense, và khi câu chữ giống một câu **test** từ 0,8 trở lên. Sense định danh theo (bảng, biểu thức SQL); khung SQL ở dạng chuẩn ([phase2-plan.md](phase2-plan.md) §2.1, §3) |
| 26 | Template train gần template test (2026-11-05) | Giữ 9 template train gần test (612 câu, chủ yếu bản "theo ngày" của câu tổng). Báo EX test chia theo template gần/xa, ghi chú kỹ năng train còn thiếu cho template xa ([phase2-plan.md](phase2-plan.md) §5.5) |
| 27 | Thực thể giữ riêng (2026-11-05) | 20 thực thể không bao giờ được nêu tên trong prompt train, để R8 có nhóm "chưa thấy" sạch. Rút theo seed trong số thực thể không nằm trong top-5 của lớp nào (evidence liệt kê thành viên lớp theo tên), không có cha/con trong pool, và không có tên là từ thông dụng (stopword của bộ linking). Bỏ khỏi giá trị điền mọi thực thể, ví/contract và token có tên chứa tên của chúng; câu nào evidence hay câu hỏi vẫn nêu tên chúng thì bị loại. Lần rút đầu (ngẫu nhiên trong 99) để lộ tên 17/20 thực thể trong 309/1.960 prompt train, nên người dùng chọn rút lại (2026-11-05). Điều chỉnh quyết định 15 |
