# Benchmark: câu hỏi, gold SQL và nhãn linking gold

Danh mục template (mục 4) được người dùng duyệt ngày 2026-11-01. Định dạng mỗi
câu theo [linking-labels.md](linking-labels.md), cộng `gold_sql`, `template_id` và
`knowledge_base_version`.

## 1. Các quyết định (2026-11-01)

| # | Điểm | Quyết định |
|---|---|---|
| 1 | Tập địa chỉ của thành viên lớp | Chỉ các thực thể trong cây mang lớp đó, trừ contract token. Đã ghi vào `linking-labels.md` §2 (quyết định c). Không có quy tắc này thì Circle đứng đầu `bridge` nhờ contract USDC. Giữ quy tắc đơn giản dù loại cả vài contract lõi là token (stETH, position manager của Uniswap, thị trường Compound v3); không thêm danh sách ngoại lệ |
| 2 | Lớp được hỏi | Chỉ các lớp mà mọi thành viên có tập địa chỉ **đóng**: `dex`, `dex-aggregator`, `bridge`, `lending`, `layer-2` (sau đợt duyệt 3: 9–15 thành viên mỗi lớp). `liquid-staking` và `nft-marketplace` chỉ có 2 thành viên (eth-labels thiếu Rocket Pool, LooksRare, ...) nên không hỏi. Không hỏi `cex`, `block-builder`, `erc4337-bundler`, `mev-bot`, `market-maker`, `stablecoin` (có thành viên tập mở: hỏi theo lớp chính là hỏi tổng theo cả sàn) |
| 3 | Lớp ít thành viên | Đợt duyệt 3 bổ sung thành viên còn `pending` cho các lớp ở dòng 2. Câu ghi số rõ thì N nhỏ hơn số thành viên |
| 4 | Property nhập nhằng | Thêm cụm từ trỏ nhiều nghĩa vào `data/schema/property_phrases.csv` (đã duyệt): "khối lượng/volume/amount/giá trị/value" → `eth_amount` hoặc `token_amount`; "số lượt/how many times" → `tx_count` hoặc `transfer_count`. Nghĩa chọn theo loại thực thể đã link |
| 5 | Chạy bộ sinh | `uv run python -m eth_nl2sql.benchmark_build`, không thêm lệnh vào `cli.py` |
| 6 | Phiên bản KB | `data/benchmark/manifest.json` ghi SHA256 từng CSV (`data/entity_knowledge_base/*.csv`, `data/schema/property_*.csv`) và SHA256 của `dev.jsonl`/`test.jsonl`; mỗi bản ghi có `knowledge_base_version` (SHA256 của danh sách hash các CSV) |
| 7 | Quy mô và chia tập | Khoảng 500 câu từ khoảng 40 template. Chia theo template, khoảng 1/3 dev, 2/3 test; mỗi tập có đủ 4 đặc thù, VI/EN, NIL và unsupported |

## 2. Quy ước chung

**Thời gian.** UTC, nửa mở `[start, end)`, trong tháng 9/2026. Span theo quy ước chung với
bộ linking (chốt 2026-11-01): tiếng Việt bỏ "trong"/"vào", giữ "ngày"/"từ"; tiếng Anh giữ
"on"/"in"/"during" và "the". Phần trong ngoặc vuông dưới đây không thuộc span.

| Cụm VI | Cụm EN | `[start, end)` |
|---|---|---|
| (không nói) | (không nói) | `time = null` |
| [trong] tháng 9 | in September | `[09-01, 10-01)` |
| ngày 5/9 | on September 5 | `[09-05, 09-06)` |
| [trong] tuần đầu tháng 9 | in the first week of September | `[09-01, 09-08)` |
| [trong] tuần thứ 2 tháng 9 | in the second week of September | tuần thứ k = `[7k−6, 7k+1)`: `[09-08, 09-15)` |
| [trong] tuần cuối tháng 9 | in the last week of September | `[09-24, 10-01)` |
| [trong] nửa đầu tháng 9 | in the first half of September | `[09-01, 09-16)` |
| từ 10/9 đến 15/9 | from September 10 to 15 | `[09-10, 09-16)` (ngày cuối tính trọn) |

Gold SQL viết:

```sql
block_timestamp >= TIMESTAMP '2026-09-05 00:00:00' AND block_timestamp < TIMESTAMP '2026-09-06 00:00:00'
```

Bảng `blocks` dùng cột `timestamp`.

**Gold SQL.**

- Chỉ chạm schema `main`, tên bảng không ghi schema.
- Viết trong tập con chung DuckDB ∩ Postgres: không QUALIFY/EXCLUDE/`list_*`. SQLGlot phải parse
  được bằng cả hai dialect.
- Placeholder `{E1}`, `{E2}`… đứng ở vị trí một danh sách IN: `to_address IN {E1}`. B8 thay bằng
  `('0x…', '0x…')`. `decimals` của token ghi literal (`/ 1e6`).
- Lượng ETH nhận/gửi chỉ tính giao dịch thành công (`receipt_status = 1`, sense `tx_success`
  ngầm; điều kiện ở phần Nhãn).
- Lượng token: `SUM(CAST(value AS DECIMAL(38,0))) / 1e{decimals}`, tức cộng chính xác rồi chia
  một lần, để kết quả xác định. Chỉ dùng token không có `value` dài hơn 38 chữ số.
- Phí: `SUM(CAST(receipt_gas_used AS DECIMAL(38,0)) * receipt_effective_gas_price) / 1e18`.
- Câu trả về nhiều dòng: `ORDER BY` có cột phá hoà (địa chỉ, hash, ngày), mọi khoá ghi rõ
  `NULLS LAST`, và có `LIMIT` khi câu hỏi hỏi top.
- Cột kết quả đặt tên snake_case (`eth_received`, `tx_count`).

**Nhãn.** Theo quy ước chung với bộ linking (chốt 2026-11-01):

- `tx_success` ngầm chỉ khi có `eth_amount` + (`received_by` | `sent_by` | `involving`) + ít nhất
  một mention đã link (không phải `nil`), và câu không nói rõ thành công/thất bại.
- `of_token` ngầm khi có mention `token` và câu chưa nói ra `of_token`.
- Câu có `unsupported`: không thêm nghĩa ngầm nào; chỉ ghi các property nói ra trong câu.
- Câu NIL: mention `nil` không tính là đã link, nên không có `tx_success` ngầm (ví dụ 7 của
  `linking-labels.md`).
- Mention `class`: `span` gồm từ chỉ độ lớn và số, không gồm "the"/"các": "major DEXs",
  "DEX lớn", "5 DEX lớn nhất", "top 3 bridges".
- Thành viên lớp: gộp lên cha chỉ khi cha cũng thuộc lớp; tập địa chỉ theo `linking-labels.md` §2
  (chỉ thực thể mang lớp, trừ contract token).

## 3. Chọn giá trị điền

| Chỗ trống | Lấy từ | Điều kiện |
|---|---|---|
| `{entity}` | Thực thể đã duyệt, `address_set = closed`, `kind ≠ group` | Đại lượng được hỏi khác 0 trong khoảng thời gian. Alias rút ngẫu nhiên trong các alias của thực thể (tên chính, viết tắt, tên cũ, tên tiếng Việt) để phủ đặc thù 2 |
| `{address}` | Alias `target_type = address` | Alias chỉ trỏ tới đúng một địa chỉ. Gồm ví có tên của sàn (tập mở, ví dụ "Coinbase 10") và contract có tên ("Uniswap V2: Router 2") |
| `{token}` | Alias `target_type = token` | ERC-20 có `decimals`, không có `value` dài hơn 38 chữ số, `callers ≥ 1.000`. Ưu tiên symbol trùng nhiều (USDT: 1.200 token mang symbol này) và tên trùng thực thể (Tether, Uniswap) |
| `{class}` | Alias `target_type = class` của các lớp ở quyết định 2 | `top_n` mặc định 5 (`default`), hoặc ghi rõ N từ 2 đến số thành viên − 1 (`explicit`) |
| `{literal}` | Địa chỉ hoạt động trong tháng 9 **không** có trong KB | Gõ thẳng `0x…` trong câu |
| `{nil_name}` | `not_in_kb`: tên không khớp alias nào trong CSV, kể cả dòng `pending`/`rejected` (Bybit, OKX, Hyperliquid…). `not_reviewed`: alias của thực thể vẫn `pending` sau đợt duyệt cuối | |

Mỗi template có 2–3 cách diễn đạt cho mỗi ngôn ngữ; `span` tính khi điền chỗ trống.

## 4. Danh mục template (đã duyệt 2026-11-01)

Ký hiệu: `T` là điều kiện thời gian (bỏ đi khi `time = null`); sense có dấu * là ngầm.

### A. Thực thể tập đóng (đặc thù 1, 2)

| id | Câu mẫu VI / EN | Sense | Khung gold SQL |
|---|---|---|---|
| `entity_eth_received` | Tornado Cash nhận bao nhiêu ETH trong tuần đầu tháng 9? / How much ETH did Tornado Cash receive in the first week of September? | `received_by`, `eth_amount`, `tx_success`* | `SELECT SUM(value) / 1e18 AS eth_received FROM transactions WHERE to_address IN {E1} AND receipt_status = 1 AND T` |
| `entity_call_count` | Có bao nhiêu giao dịch gọi Aave ngày 5/9? / How many transactions called Aave on September 5? | `contract_called`, `tx_count` | `SELECT COUNT(*) AS tx_count FROM transactions WHERE to_address IN {E1} AND T` |
| `entity_distinct_callers` | Bao nhiêu ví khác nhau đã tương tác với Lido trong tháng 9? / How many unique senders interacted with Lido in September? | `contract_called`, `distinct_senders` | `SELECT COUNT(DISTINCT from_address) AS sender_count FROM transactions WHERE to_address IN {E1} AND T` |
| `entity_failed_calls` | Có bao nhiêu giao dịch gọi 1inch bị thất bại? / How many failed transactions called 1inch? | `contract_called`, `tx_failed`, `tx_count` | `… WHERE to_address IN {E1} AND receipt_status = 0 AND T` |
| `entity_fees_paid` | Người gọi Uniswap đã trả tổng bao nhiêu phí giao dịch ngày 1/9? / What total transaction fee did callers of Uniswap pay on September 1? | `contract_called`, `tx_fee` | `SELECT SUM(CAST(receipt_gas_used AS DECIMAL(38,0)) * receipt_effective_gas_price) / 1e18 AS fee_eth FROM transactions WHERE to_address IN {E1} AND T` |
| `entity_daily_calls` | Số giao dịch gọi Curve mỗi ngày trong tuần đầu tháng 9? / Daily number of transactions calling Curve in the first week of September? | `contract_called`, `tx_count`, `day` | `SELECT CAST(block_timestamp AS DATE) AS day, COUNT(*) AS tx_count FROM transactions WHERE to_address IN {E1} AND T GROUP BY CAST(block_timestamp AS DATE) ORDER BY day ASC NULLS LAST`. Chỉ dùng khoảng nhiều ngày |
| `entity_top_callers` | 5 địa chỉ gọi Across nhiều nhất trong tháng 9? / Which 5 addresses called Across the most in September? | `contract_called`, `tx_count` | `SELECT from_address, COUNT(*) AS tx_count FROM transactions WHERE to_address IN {E1} AND T GROUP BY from_address ORDER BY tx_count DESC NULLS LAST, from_address ASC NULLS LAST LIMIT 5` |
| `entity_token_received` | Across nhận bao nhiêu USDC trong tháng 9? / How much USDC did Across receive in September? | `received_by`, `token_amount`, `of_token`* | `SELECT SUM(CAST(value AS DECIMAL(38,0))) / 1e6 AS amount FROM token_transfers WHERE token_address IN {E2} AND to_address IN {E1} AND T` (hai mention; thứ tự E1/E2 theo câu) |

### B. Ví hoặc contract có tên riêng

| id | Câu mẫu VI / EN | Sense | Khung gold SQL |
|---|---|---|---|
| `address_eth_received` | Ví Coinbase 10 nhận bao nhiêu ETH ngày 1/9? / How much ETH did Coinbase 10 receive on September 1? | `received_by`, `eth_amount`, `tx_success`* | như `entity_eth_received` |
| `address_eth_sent` | Kraken: Hot Wallet 2 đã gửi đi bao nhiêu ETH trong tháng 9? / How much ETH was sent from Kraken: Hot Wallet 2 in September? | `sent_by`, `eth_amount`, `tx_success`* | `… WHERE from_address IN {E1} AND receipt_status = 1 AND T` |
| `address_tx_sent_count` | Ví Binance 20 đã gửi bao nhiêu giao dịch từ 10/9 đến 15/9? / How many transactions did Binance 20 send from September 10 to 15? | `sent_by`, `tx_count` | `SELECT COUNT(*) AS tx_count FROM transactions WHERE from_address IN {E1} AND T` |
| `address_token_received` | Ví Coinbase 10 nhận bao nhiêu USDT trong tháng 9? / How much USDT did Coinbase 10 receive in September? | `received_by`, `token_amount`, `of_token`* | như `entity_token_received` |
| `address_largest_eth_sent` | Giao dịch ETH lớn nhất mà Coinbase 5 gửi trong tháng 9? / What was the largest ETH transaction sent by Coinbase 5 in September? | `sent_by`, `eth_amount`, `tx_success`* | `SELECT hash, value / 1e18 AS eth FROM transactions WHERE from_address IN {E1} AND receipt_status = 1 AND T ORDER BY value DESC NULLS LAST, hash ASC NULLS LAST LIMIT 1` |

### C. Token (đặc thù 2: symbol trùng, tên trùng thực thể)

| id | Câu mẫu VI / EN | Sense | Khung gold SQL |
|---|---|---|---|
| `token_transfer_count` | Có bao nhiêu lượt chuyển USDT trong ngày 5/9? / How many USDT transfers were there on September 5? | `transfer_count`, `of_token`* | `SELECT COUNT(*) AS transfer_count FROM token_transfers WHERE token_address IN {E1} AND T` |
| `token_amount_total` | Tổng lượng Tether USD được chuyển trong tuần đầu tháng 9? / What total amount of Tether USD was transferred in the first week of September? | `token_amount`, `of_token`* | `SELECT SUM(CAST(value AS DECIMAL(38,0))) / 1e6 AS amount FROM token_transfers WHERE token_address IN {E1} AND T` |
| `token_distinct_senders` | Bao nhiêu địa chỉ đã gửi LINK trong tháng 9? / How many unique senders transferred LINK in September? | `distinct_senders`, `of_token`* | `SELECT COUNT(DISTINCT from_address) AS sender_count FROM token_transfers WHERE token_address IN {E1} AND T` |
| `token_top_receivers` | 5 địa chỉ nhận nhiều WBTC nhất trong tháng 9? / Which 5 addresses received the most WBTC in September? | `received_by`, `token_amount`, `of_token`* | `SELECT to_address, SUM(CAST(value AS DECIMAL(38,0))) / 1e8 AS amount FROM token_transfers WHERE token_address IN {E1} AND T GROUP BY to_address ORDER BY amount DESC NULLS LAST, to_address ASC NULLS LAST LIMIT 5` |
| `token_largest_transfer` | Lượt chuyển DAI lớn nhất ngày 5/9? / What was the largest DAI transfer on September 5? | `token_amount`, `of_token`* | `SELECT transaction_hash, log_index, CAST(value AS DECIMAL(38,0)) / 1e18 AS amount FROM token_transfers WHERE token_address IN {E1} AND T ORDER BY CAST(value AS DECIMAL(38,0)) DESC NULLS LAST, transaction_hash ASC NULLS LAST, log_index ASC NULLS LAST LIMIT 1` |
| `token_daily_transfers` | Số lượt chuyển UNI theo ngày trong tuần đầu tháng 9? / Daily number of UNI transfers in the first week of September? | `transfer_count`, `of_token`*, `day` | như `entity_daily_calls` trên `token_transfers` |

### D. Theo lớp (đặc thù 3)

`{E1}` là địa chỉ của top-N thành viên theo `linking-labels.md` §2. SQL tính trên cả tập, không tách
theo từng thành viên (việc đó cần JOIN `link`).

| id | Câu mẫu VI / EN | Sense | Khung gold SQL |
|---|---|---|---|
| `class_eth_received` | Các DEX lớn nhận bao nhiêu ETH ngày 5/9? / How much ETH did the major DEXs receive on September 5? | `received_by`, `eth_amount`, `tx_success`* | như `entity_eth_received` |
| `class_call_count` | Có bao nhiêu giao dịch gọi 3 bridge lớn nhất trong tuần đầu tháng 9? / How many transactions called the top 3 bridges in the first week of September? | `contract_called`, `tx_count` | như `entity_call_count` |
| `class_distinct_callers` | Bao nhiêu ví đã dùng các giao thức cho vay lớn trong tháng 9? / How many unique senders used the major lending protocols in September? | `contract_called`, `distinct_senders` | như `entity_distinct_callers` |
| `class_token_received` | Các bridge lớn nhận bao nhiêu USDC trong tháng 9? / How much USDC did the major bridges receive in September? | `received_by`, `token_amount`, `of_token`* | như `entity_token_received` |
| `class_top_contracts` | Contract nào của các DEX aggregator lớn được gọi nhiều nhất ngày 1/9? Liệt kê 5. / Which 5 contracts of the major DEX aggregators were called the most on September 1? | `contract_called`, `tx_count` | `SELECT to_address, COUNT(*) AS tx_count FROM transactions WHERE to_address IN {E1} AND T GROUP BY to_address ORDER BY tx_count DESC NULLS LAST, to_address ASC NULLS LAST LIMIT 5` |

### E. Property nhập nhằng (đặc thù 4)

Cùng một cách nói, chỗ trống điền **cả** thực thể/ví/lớp **lẫn** token, nên nghĩa gold đổi theo loại
đích. Mỗi template cân đối khoảng một nửa mỗi loại.

| id | Câu mẫu VI / EN | Nghĩa theo loại đích | Khung gold SQL |
|---|---|---|---|
| `ambiguous_volume` | Khối lượng chuyển vào Uniswap trong tháng 9? / Khối lượng USDC được chuyển trong tháng 9? / What was the volume of Uniswap / of USDC in September? | thực thể, ví, lớp → `eth_amount` + `received_by` + `tx_success`*; token → `token_amount` + `of_token`* | như `entity_eth_received` hoặc `token_amount_total` |
| `ambiguous_value_received` | Tổng giá trị Coinbase 10 nhận được ngày 1/9? / What total value did Coinbase 10 receive on September 1? | ví/thực thể → `eth_amount`; câu "tổng giá trị USDT ví X nhận" → `token_amount` | như `address_eth_received` hoặc `address_token_received` |
| `ambiguous_times` | Uniswap V2: Router 2 được gọi bao nhiêu lượt ngày 5/9? / USDT được chuyển bao nhiêu lượt ngày 5/9? / How many times was … on September 5? | thực thể, ví, lớp → `tx_count` + `contract_called`; token → `transfer_count` + `of_token`* | như `entity_call_count` hoặc `token_transfer_count` |
| `ambiguous_users` | Uniswap có bao nhiêu người dùng trong tháng 9? / USDT có bao nhiêu người dùng? / How many users did … have? | thực thể, lớp → `distinct_senders` trên `transactions` (`to_address IN {E1}`); token → `distinct_senders` trên `token_transfers` (`token_address IN {E1}`) | như `entity_distinct_callers` hoặc `token_distinct_senders` |

### F. Không có thực thể có tên / địa chỉ gõ sẵn

Những câu này kiểm tra bộ linking có tạo ra mention thừa không.

| id | Câu mẫu VI / EN | Sense | Khung gold SQL |
|---|---|---|---|
| `literal_eth_received` | Địa chỉ 0x… nhận bao nhiêu ETH trong tháng 9? / How much ETH did 0x… receive in September? | `received_by`, `eth_amount`, `tx_success`* | `… WHERE to_address = '0x…' AND receipt_status = 1 AND T` |
| `literal_tx_sent_count` | 0x… đã gửi bao nhiêu giao dịch? / How many transactions did 0x… send? | `sent_by`, `tx_count` | `… WHERE from_address = '0x…' AND T` |
| `daily_avg_gas_price` | Giá gas trung bình mỗi ngày trong tuần đầu tháng 9? / Average gas price per day in the first week of September? | `gas_price_paid`, `day` | `SELECT CAST(block_timestamp AS DATE) AS day, AVG(receipt_effective_gas_price) / 1e9 AS avg_gwei FROM transactions WHERE T GROUP BY CAST(block_timestamp AS DATE) ORDER BY day ASC NULLS LAST` |
| `blob_tx_count` | Có bao nhiêu giao dịch blob ngày 5/9? / How many blob transactions were there on September 5? | `tx_blob`, `tx_count` | `SELECT COUNT(*) AS tx_count FROM transactions WHERE transaction_type = 3 AND T` |
| `contract_creation_count` | Bao nhiêu giao dịch tạo contract thành công trong tuần đầu tháng 9? / How many successful contract creation transactions were there …? | `contract_creation`, `tx_success`, `tx_count` | `… WHERE to_address IS NULL AND receipt_status = 1 AND T` |
| `top_block_producers` | 5 fee recipient tạo nhiều block nhất trong tháng 9? / Which 5 fee recipients produced the most blocks in September? | `block_producer`, `block_count` | `SELECT miner, COUNT(*) AS block_count FROM blocks WHERE T GROUP BY miner ORDER BY block_count DESC NULLS LAST, miner ASC NULLS LAST LIMIT 5` |

### G. NIL (khoảng 5%, không có gold SQL)

| id | Câu mẫu VI / EN | `nil_reason` |
|---|---|---|
| `nil_eth_received` | Bybit nhận bao nhiêu ETH trong tháng 9? / How much ETH did Bybit receive in September? | `not_in_kb` hoặc `not_reviewed` |
| `nil_call_count` | Có bao nhiêu giao dịch gọi Hyperliquid ngày 5/9? / How many transactions called Hyperliquid on September 5? | như trên |
| `nil_token_transfer_count` | Có bao nhiêu lượt chuyển token HYPE ngày 5/9? / How many HYPE transfers were there on September 5? | `not_in_kb` (symbol không có alias nào) |

### H. Unsupported (khoảng 5%, không có gold SQL)

| id | Câu mẫu VI / EN | `unsupported` |
|---|---|---|
| `unsupported_balance` | Số dư của ví Coinbase 10 là bao nhiêu? / What is the balance of Coinbase 10? | `balance` |
| `unsupported_usd_value` | Tornado Cash nhận bao nhiêu USD trong tháng 9? Tính theo giá trị USD. / What was the USD value of ETH received by Tornado Cash in September? | `usd_value` |
| `unsupported_internal_eth` | Tornado Cash đã trả ra bao nhiêu ETH qua giao dịch nội bộ? / How much ETH did Tornado Cash pay out through internal transactions? | `internal_eth` |

## 5. Phân bổ dự kiến

| Nhóm | Template | Số câu |
|---|---:|---:|
| A. Thực thể | 8 | 110 |
| B. Ví/contract có tên | 5 | 65 |
| C. Token | 6 | 75 |
| D. Lớp | 5 | 70 (khoảng nửa N mặc định, nửa N ghi rõ) |
| E. Property nhập nhằng | 4 | 60 |
| F. Không có tên / địa chỉ gõ sẵn | 6 | 70 |
| G. NIL | 3 | 25 |
| H. Unsupported | 3 | 25 |
| **Tổng** | **40** | **500** |

Mỗi template sinh câu VI và EN tỉ lệ 1:1. Đặc thù của từng câu suy ra từ nhãn:

- 1: mention `entity` có từ 2 địa chỉ;
- 2: alias khác tên chính, hoặc symbol token trùng;
- 3: mention `class`;
- 4: nhóm E.

## 6. Kiểm chứng mỗi câu

1. Bản ghi đúng `linking-labels.md`: `span` khớp `question[start:end]`, placeholder đánh số theo thứ
   tự xuất hiện, `target` tồn tại trong `link`.
2. SQLGlot parse được gold SQL với cả dialect `duckdb` và `postgres`; không có QUALIFY, EXCLUDE,
   `list_*`. Câu nhiều dòng thì có `ORDER BY` với cột phá hoà và `NULLS FIRST/LAST`.
3. Thay placeholder rồi chạy trên DuckDB (`memory_limit = '4GB'`), giới hạn thời gian 10 giây.
   Loại câu lỗi, câu chạy chậm, câu trả về rỗng hoặc chỉ có NULL/0, trừ khi template chủ ý hỏi
   điều đó.

4. **Gắn nhãn khả năng phân biệt** (2026-11-01; gắn nhãn, không lọc). Sau khi sinh, mỗi câu có
   gold SQL được chạy thêm các biến thể sai (`benchmark_validate.wrong_variants`):
   - `status`: bỏ điều kiện `receipt_status`;
   - `direction`: đổi `to_address` ↔ `from_address` ở chỗ so với địa chỉ đã link;
   - `time`: bỏ điều kiện thời gian, trừ khi khoảng thời gian là cả tháng 9 (khi đó hai câu như
     nhau).

5. **Ưu tiên mềm cho `tx_success` ngầm** (quyết định B, 2026-11-01). Câu có `tx_success` ngầm mà
   bỏ `receipt_status = 1` vẫn ra cùng đáp án thì được thay bằng một câu khác cùng template,
   cùng cách diễn đạt và cùng ngôn ngữ, nếu một trong 20 ứng viên rút thêm phân biệt được lỗi
   này. Ứng viên rút bằng RNG riêng (seed, template, số thứ tự câu), nên RNG chung vẫn rút như cũ
   và mọi template khác giữ nguyên câu hỏi. Không áp dụng cho `address_largest_eth_sent`, vì
   giao dịch thất bại hiếm khi là giao dịch lớn nhất. Đây là sai lệch chọn mẫu có chủ đích:
   trong 7 template này, ví/thực thể nghiêng về những nơi có giao dịch thất bại mang ETH. Luận
   văn phải nêu rõ điều này.

6. **Không hoà ở `LIMIT`** (2026-11-03, người dùng chọn). Câu hỏi top-k hay "lớn nhất" không
   nói cách phá hoà. Nếu kết quả gold đổi khi đảo chiều khoá phá hoà cuối cùng của `ORDER BY`
   (`benchmark_validate.reversed_tie_break`), một câu SQL đúng mà phá hoà cách khác sẽ bị chấm
   sai, và chấm sai hay đúng còn tuỳ lần chạy, vì DuckDB chạy song song. Câu như vậy được thay
   giống bước 5: tối đa 20 ứng viên cùng template, cùng cách diễn đạt và cùng ngôn ngữ, rút bằng
   RNG riêng; ứng viên phải không hoà và vẫn giữ ưu tiên mềm của bước 5. Không tìm được thì giữ
   câu cũ và ghi cảnh báo.

   Bước 4 gắn nhãn các câu. `manifest.json` → `discriminates` ghi cho mỗi câu biến thể nào cho kết quả **khác** gold
   (`true`), giống gold (`false`), hoặc chạy quá giờ (`null`). Bước này không dùng RNG, nên cùng
   seed vẫn cho đúng các câu như cũ. `evaluate-sql` báo thêm EX trên tập con phân biệt được của
   từng loại lỗi (`discriminating`).

## 7. Tập thực thể (quyết định 6 của `proposed-method.md` §9)

Khoảng 169 thực thể đã duyệt sau hai đợt bổ sung (đề xuất ghi trong `data/entity_knowledge_base/`,
ghi chú `[Claude đợt 3]`, `[Claude đợt 4]`, chờ duyệt):

| Đợt | Số thực thể | Nội dung |
|---|---:|---|
| 1–2 | 90 | Đã duyệt trước khi làm benchmark |
| 3 | 53 | Thành viên các lớp sẽ hỏi (bridge, dex, dex-aggregator, lending, layer-2) và thực thể con còn `pending` của cha đã duyệt (SushiSwap v2/v3, Balancer V2, Arbitrum One/Nova, Velora v5, ...) |
| 4 | 26 | Thực thể hoạt động cao còn lại: 12 sàn, cộng ví con của Coinbase/Bitget (chỉ hỏi qua ví có tên), Frax, Yearn, Safe, Ethena, Beacon Deposit Contract, Privacy Pools, Jump Trading, USDT0 |

Số thành viên của các lớp được hỏi sau đợt 3: dex 12, dex-aggregator 9, bridge 15, lending 10,
layer-2 12. Các thực thể còn `pending` sau đợt 4 là nguồn câu NIL `not_reviewed` (ví dụ OKX).

## 8. Sinh và kết quả (2026-11-01)

```bash
uv run python -m eth_nl2sql.benchmark_build                 # data/benchmark/{dev,test}.jsonl, manifest.json
uv run python -m eth_nl2sql.benchmark_build --count-scale 0.1 --out-dir /tmp/bench   # chạy thử
```

Mã nguồn: `benchmark_templates.py` (danh mục, render, nghĩa ngầm), `benchmark_validate.py` (kiểm
bản ghi, SQLGlot, chạy SQL), `benchmark_build.py` (chọn giá trị điền, thành viên lớp, sinh, chia,
ghi file). Seed cố định 20261101; chạy lại trên cùng KB và database cho cùng kết quả.

- **505 câu** (VI 260, EN 245), mỗi câu đều qua: kiểm bản ghi theo `linking-labels.md`; SQLGlot
  parse được bằng DuckDB và Postgres; chạy trên `main` dưới 10 giây với kết quả khác rỗng/0. Toàn
  bộ chạy mất khoảng 4 phút.
- **Chia theo template**: dev 177 câu (14 template), test 328 câu (26 template); cả hai tập có đủ 8
  nhóm, 4 đặc thù, VI/EN. `test.jsonl` chỉ dùng cho đánh giá cuối (`entity_linking_evaluation`
  từ chối đọc file này nếu không bật `allow_test`).
- Đặc thù (`manifest.json` → `phenomena`): nhiều địa chỉ 137, alias 154, lớp 70, property nhập
  nhằng 60. NIL 24 câu (`not_in_kb` 16, `not_reviewed` 8), unsupported 24 câu.
- `manifest.json` ghi SHA256 từng CSV (`knowledge_base_files`, cùng khoá đường dẫn với
  `entity_linking_evaluation.knowledge_base_version`), SHA256 của dev/test, số câu bị loại theo lý
  do, top-10 thành viên của mỗi lớp.

**Khả năng phân biệt** (2026-11-01). Lần sinh lại đầu tiên chỉ gắn nhãn: câu hỏi và gold không
đổi, chỉ đổi `knowledge_base_version` vì `property_phrases.csv` đã sửa. Lần thứ hai thêm ưu tiên
mềm (bước 5 ở mục 6); chỉ đổi 53 câu của 7 template có `tx_success` ngầm.

| Lỗi | dev | test | test trước ưu tiên mềm |
|---|---|---|---|
| `direction` | 114/123 | 185/187 | 185/187 |
| `time` | 105/105 | 197/199 | 200/202 |
| `status`, `tx_success` tường minh hoặc `tx_failed` | (không có template) | 26/26 | 26/26 |
| `status`, `tx_success` ngầm | 31/35 (trước: 11/35) | 46/60 | 13/60 |

**Bỏ câu hoà ở `LIMIT`** (2026-11-03, bước 6 ở mục 6). Lần sinh lại này thay 6 câu: 4 câu
`entity_top_callers` ở dev (q00031, q00034, q00036, q00037; ví dụ Gearbox Protocol: các ví từ hạng 1 đến
hạng 8 đều gọi đúng 2 lần) và 2 câu `address_largest_eth_sent` ở test. Mọi câu khác giữ nguyên id và nội
dung. `knowledge_base_version` đổi vì `property_senses.csv` có thêm cột `evidence_hint`.

Trên test, 13 câu `tx_success` ngầm còn lại không phân biệt được đều thuộc
`address_largest_eth_sent`. Độ đa dạng giữ gần nguyên: số đích khác nhau mỗi template thay đổi
trong khoảng −2 đến +1.

Trước ưu tiên mềm, `tx_success` ngầm hầu như không phân biệt được ở ví có tên và địa chỉ gõ sẵn: `address_eth_sent`
0/13, `address_eth_received` 1/13, `address_largest_eth_sent` 0/13, `literal_eth_received` 0/12,
`ambiguous_value_received` 0/8. Các ví này gần như không có giao dịch thất bại mang ETH. Câu "giao
dịch lớn nhất" về cấu trúc gần như không bao giờ phân biệt được, vì giao dịch thất bại hiếm khi là
giao dịch lớn nhất. Thực thể và lớp thì tốt hơn: `class_eth_received` 11/14, `entity_eth_received`
7/14.

**Đối chiếu với bộ linking (chỉ trên dev).** Mention F1 0,944, entity acc@1 0,942, class 0,857,
property F1 0,965, time 1,000. Các chỗ lệch đã xem từng câu; không có lỗi gold. Phần lớn là điểm
yếu của bộ linking hoặc từ điển: "L2s" không nhận ra lớp `layer-2`; token gọi bằng tên trùng thực
thể ("Ondo", "SUSHI", "Tether Gold") bị link sang thực thể dù câu hỏi lượng nhận/chuyển; "giá trị
USD Coin" bị hiểu là `usd_value`; "callers of" và "bao nhiêu" (lượng token) chưa có trong từ điển.
