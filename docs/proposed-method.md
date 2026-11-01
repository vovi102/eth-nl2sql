# Phương pháp đề xuất: schema/entity linking dựa trên KB cho NL2SQL trên Ethereum

Bản sửa ngày 2026-11-01, sau khi đã dựng database và duyệt hai đợt cơ sở tri thức (KB). Căn cứ:

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
| `link.property_senses`, `link.property_phrases` | 36 nghĩa (biểu thức SQL, loại thực thể áp dụng, đơn vị) và 115 cụm từ VI/EN trỏ tới nghĩa; nguồn `data/schema/property_*.csv` đã duyệt. Biểu thức được DuckDB bind trên bảng thật mỗi lần nạp | B5 |

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

- **Độ dài.** 63 địa chỉ hex ước lượng hơn 1.000 token (chưa đo bằng tokenizer của Qwen), chiếm
  phần lớn ngân sách KV cache trên GPU 4 GB. Câu hỏi theo lớp (top-5 DEX) còn dài hơn.
- **Độ chính xác.** Model nhỏ chép sai 1 ký tự hex thì SQL vẫn chạy nhưng sai đáp án. Lỗi này im
  lặng và khó phát hiện.
- **Tách lỗi.** Lỗi linking (chọn sai `{E1}`) và lỗi sinh SQL (dùng sai `{E1}`) đo riêng được.

Token có một địa chỉ cũng đi qua placeholder để định dạng thống nhất; evidence của token ghi kèm
`decimals` lấy từ `token_stats`. Địa chỉ người dùng gõ sẵn trong câu hỏi thì giữ nguyên dạng
literal.

Đây là đề xuất của luận văn, chưa có số liệu trong các nguồn đã đọc. Cần chạy ablation so với cách
chép literal (mục 7).

## 6. Phase 2: LoRA

- Dữ liệu huấn luyện: cặp NL/SQL sinh từ template, đã kiểm chứng bằng thực thi, **dùng đúng định
  dạng prompt ở mục 5**. Model học cách dùng evidence và placeholder, không học thuộc địa chỉ.
- Câu hỏi huấn luyện đi qua **chính pipeline linking** (không dùng linking oracle), để model quen
  với nhiễu linking thật.
- Tách train/test theo template, như đã chốt.

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

**Ablation** (mỗi dòng bỏ hoặc thay một thành phần):

1. Không linking: chỉ schema + câu hỏi.
2. Linking bỏ prior hoạt động (chỉ khớp chuỗi): đo tác động lên token trùng symbol.
3. Bỏ khớp mờ / thêm embedding đa ngữ.
4. Chép literal địa chỉ thay vì placeholder.
5. Toàn bộ schema so với schema đã lọc.
6. **Linking oracle** (gold): cận trên. Khoảng cách oracle − pipeline là con số chính để nói
   linking còn thiếu bao nhiêu.

Mỗi cấu hình chạy cho cả phase 1 và phase 2.

## 8. Tiến độ (2026-11-01)

| Hạng mục | Trạng thái |
|---|---|
| Database `main` (6 bảng, 160 triệu dòng), kiểm chứng khớp BigQuery | Xong |
| Schema `meta`: mô tả cột BigQuery + 2 giá trị ví dụ | Xong; còn thiếu bản tiếng Việt và từ đồng nghĩa |
| KB: quy tắc nhận nhãn, sinh nháp, kiểm tra (`draft-entities`, `check-entities`) | Xong |
| KB đã duyệt | 169/1.080 thực thể (4 đợt; đợt 2 duyệt tạm thời; đợt 3–4 bổ sung thành viên các lớp được hỏi và thực thể hoạt động cao cho benchmark), 21 lớp, 43.717 địa chỉ, 47.533 alias (số dòng trong `link`) |
| `link.token_stats` (prior cho token) | Xong (95.473 token) |
| `property_senses` | Xong: 36 nghĩa, 115 cụm từ, đã duyệt và nạp vào `link` |
| Bộ linking B1–B5 | Xong (`link-question`, `evaluate-linking`; `src/eth_nl2sql/entity_linking*.py`): khớp 8 ví dụ của [linking-labels.md](linking-labels.md) §7. Dev (177 câu), sau khi sửa theo lỗi thấy trên dev nên lạc quan: mention F1 chặt 1,000 (ban đầu 0,944), accuracy@1 1,000 (0,942), property F1 0,998 (0,965); số khách quan chỉ có ở test |
| Dựng prompt B7, hậu xử lý B8 | Chưa làm; đã có hàm mở rộng placeholder thành địa chỉ (`EntityLinker.placeholder_addresses`, `substitute_placeholders`) |
| Định dạng nhãn linking (gold và đầu ra bộ linking) | Xong: [linking-labels.md](linking-labels.md) |
| Benchmark (template, nhãn gold) | Xong: 505 câu, 40 template đã duyệt, dev 177 / test 328 chia theo template (`data/benchmark/`, [benchmark.md](benchmark.md)); tập 169 thực thể sau đợt duyệt 3–4 |

## 9. Các quyết định (chốt ngày 2026-11-01)

| # | Điểm | Quyết định |
|---|---|---|
| 1 | Đưa kết quả linking vào SQL | **Placeholder** (`{E1}`), B8 thay bằng literal. Chép literal địa chỉ vào prompt chỉ chạy trong ablation (mục 7, dòng 4). Không JOIN bảng `link` trong SQL |
| 2 | `property_senses` | Claude soạn nháp (khoảng 30 nghĩa, kèm cụm từ VI/EN), người dùng duyệt, giống cách duyệt KB |
| 3 | "Lớn/major" | **N = 5** cố định, xếp hạng theo số giao dịch trong tháng 9, nêu rõ trong evidence. Benchmark có cả câu ghi số rõ ("5 DEX lớn nhất") và câu mơ hồ ("các DEX lớn") |
| 4 | Thực thể có tập địa chỉ mở khi suy luận | **Trả lời kèm cảnh báo**: chỉ tính các ví đã biết, kết quả là cận dưới. Benchmark không có loại câu này |
| 5 | Embedding đa ngữ | Không có trong cấu hình chính; chỉ chạy ablation (mục 7, dòng 3). Đưa vào cấu hình chính nếu ablation cho thấy tăng rõ |
| 6 | Số thực thể cần duyệt | Khoảng **150**: top theo hoạt động, cộng mọi thành viên của các lớp sẽ hỏi. Tập này chốt cùng lúc với thiết kế template benchmark; thực thể ngoài tập trả về NIL |
