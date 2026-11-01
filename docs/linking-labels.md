# Định dạng nhãn linking

Chốt ngày 2026-11-01 (các quyết định ở mục 10). Một định dạng dùng chung cho hai việc:

- **Nhãn gold** của mỗi câu hỏi trong benchmark.
- **Đầu ra** của bộ linking (bước B1–B5 trong [proposed-method.md](proposed-method.md)).

Hai bên cùng định dạng thì mới so sánh trực tiếp được để tính các chỉ số ở mục 9, và thay
linking thật bằng linking oracle (gold) được khi chạy ablation.

## 1. Cấu trúc tổng quát

Mỗi câu hỏi có một bản ghi JSON:

```json
{
  "question_id": "q000123",
  "question": "Tornado Cash nhận bao nhiêu ETH trong tuần đầu tháng 9?",
  "lang": "vi",
  "mentions": [ ... ],
  "properties": [ ... ],
  "time": { ... },
  "unsupported": [ ... ],
  "warnings": [ ... ]
}
```

| Trường | Ý nghĩa |
|---|---|
| `question_id` | Mã câu hỏi trong benchmark |
| `question` | Câu hỏi gốc, Unicode dạng NFC. Vị trí ký tự (`span`) tính trên chuỗi này |
| `lang` | `vi` hoặc `en` |
| `mentions` | Các cụm từ chỉ thực thể/địa chỉ/token/lớp (mục 2) |
| `properties` | Các nghĩa property được dùng (mục 3) |
| `time` | Khoảng thời gian, hoặc `null` nếu câu hỏi không giới hạn thời gian (mục 4) |
| `unsupported` | Nghĩa mà dữ liệu không trả lời được (mục 5) |
| `warnings` | Cảnh báo kèm câu trả lời (mục 5) |

`span` luôn là `[start, end)` tính theo **chỉ số ký tự** (code point) trên `question`.

## 2. Mentions

Mỗi mention có:

| Trường | Bắt buộc | Ý nghĩa |
|---|---|---|
| `span`, `text` | Có | Vị trí và đoạn văn bản trong câu hỏi |
| `type` | Có | Một trong các loại ở bảng dưới |
| `target` | Có (trừ `nil`) | Mã đích trong KB, hoặc địa chỉ |
| `placeholder` | Với `entity`, `address`, `token`, `class` | `E1`, `E2`, ... (mục 6) |
| `top_n`, `top_n_source` | Chỉ với `class` | Số thành viên lấy; `explicit` (câu hỏi ghi số) hoặc `default` (N = 5) |
| `nil_reason` | Chỉ với `nil` | Lý do không link được |
| `candidates` | Chỉ trong **đầu ra** của bộ linking | Top-k ứng viên kèm điểm, để tính recall@5; nhãn gold không có |

| `type` | `target` | Khi nào dùng | Placeholder thay bằng |
|---|---|---|---|
| `entity` | `canonical_id` trong `link.entities` | Thực thể có tập địa chỉ **đóng** (protocol), hoặc thực thể có tập **mở** nhưng kèm cảnh báo (ví dụ 6) | Mọi địa chỉ của thực thể (và của các thực thể con) |
| `address` | Địa chỉ trong `link.entity_addresses` | Ví/contract có tên riêng ("Coinbase 10", "Uniswap V2: Router 2") | Một địa chỉ |
| `token` | Địa chỉ token trong `link.token_stats` | Token gọi bằng symbol hoặc tên ("USDT", "Tether USD") | Một địa chỉ; evidence kèm `decimals` |
| `class` | `class_id` trong `link.entity_classes` | Tham chiếu theo lớp ("các DEX lớn") | Địa chỉ của top-N thành viên |
| `literal_address` | Chính địa chỉ đó | Địa chỉ `0x…` gõ sẵn trong câu hỏi | Không dùng placeholder; giữ nguyên literal |
| `nil` | Không có | Có vẻ là tên thực thể nhưng không có trong KB đã duyệt | Không có |

Giá trị `nil_reason`:

- `not_in_kb`: tên không khớp alias nào.
- `not_reviewed`: khớp một thực thể chưa duyệt.

**Thành viên của lớp và top-N.**

- **Thành viên** là các thực thể có lớp đó **ở mức thực thể** (đã duyệt tay). Lớp ở mức địa chỉ
  (từ nhãn eth-labels) chỉ được dùng cho thực thể nhóm (`kind = group`, ví dụ MEV Bot), vì nhóm
  không có lớp duyệt tay. Như vậy nhãn tự động không ghi đè quyết định duyệt: MetaMask không
  thuộc `dex` dù địa chỉ "MetaMask: Swap Router" mang nhãn `dex` của eth-labels.
- **Gộp lên thực thể cha** theo `parent_id` trước khi xếp hạng, **chỉ khi cha cũng thuộc lớp đó**
  (gộp lên tổ tiên cao nhất mang lớp): Uniswap V2/V3/V4 tính là một thành viên Uniswap; Circle
  CCTP là thành viên riêng của `bridge` vì Circle không thuộc lớp này (quy ước chốt 2026-11-01).
- **Tập địa chỉ của thành viên trong lớp** gồm địa chỉ của các thực thể trong cây (cha và các con)
  **có mang lớp đó**, trừ contract token (địa chỉ có trong `link.token_stats`). Uniswap gồm địa
  chỉ của Uniswap và cả ba phiên bản, nhưng không gồm Uniswap Protocol (Permit2, không mang lớp
  `dex`); Circle CCTP không gồm USDC/EURC; Sky trong `lending` không gồm DAI, USDS, SKY. Không có quy tắc
  này thì Circle đứng đầu `bridge` với 4,85 triệu lượt gọi, gần hết là lượt gọi contract USDC.
  Hệ quả đã chấp nhận (người dùng chọn giữ quy tắc đơn giản, 2026-11-01): một số contract lõi
  cũng là token nên bị loại: Lido trong `liquid-staking` không gồm stETH (nơi nhận ETH stake);
  Uniswap trong `dex` không gồm V3 Positions NFT, V4 Position Manager; Compound trong `lending`
  không gồm các thị trường v3 (cUSDCv3, cUSDTv3), nên xếp hạng 7 thay vì 4.
- **Top-N** xếp theo **số giao dịch tháng 9 có `to_address` thuộc tập địa chỉ của thành viên**,
  tức số lượt được gọi; hoà thì xếp theo mã thực thể. Trên dữ liệu tháng 9, cách này cho cùng
  top-5 với cách đếm cả gửi lẫn nhận ở các lớp dex, bridge và lending, nhưng không tính giao dịch
  của các ví vận hành.

Hỏi về **cả một thực thể** (mention `entity`) thì vẫn dùng mọi địa chỉ của thực thể và các con, kể
cả contract token: "Circle nhận bao nhiêu ETH" tính cả contract USDC.

Nhãn gold chỉ lưu `target` + `top_n`. Danh sách địa chỉ được tính lại khi dựng evidence, nên luôn
khớp với phiên bản KB đang dùng.

## 3. Properties

```json
{"sense_id": "eth_amount", "span": [18, 31], "text": "bao nhiêu ETH", "implicit": false}
```

| Trường | Ý nghĩa |
|---|---|
| `sense_id` | Mã nghĩa trong `link.property_senses` |
| `span`, `text` | Cụm từ trong câu hỏi; `null` nếu `implicit` |
| `implicit` | `true` khi câu hỏi không nói ra nhưng quy ước bắt buộc. Ví dụ hỏi lượng ETH nhận/gửi thì mặc định chỉ tính giao dịch thành công, nên có `tx_success` ngầm |

Thứ tự trong danh sách không quan trọng.

## 4. Time

```json
{"start": "2026-09-01T00:00:00", "end": "2026-09-08T00:00:00", "span": [38, 54], "text": "tuần đầu tháng 9"}
```

- Khoảng `[start, end)` theo giờ **UTC**, khớp kiểu `TIMESTAMP` của `main`.
- "Ngày 5/9" là `[2026-09-05, 2026-09-06)`; "tháng 9" là cả tháng `[2026-09-01, 2026-10-01)`.
- Câu hỏi không nhắc thời gian thì `time` là `null`, nghĩa là toàn bộ dữ liệu (tháng 9).

## 5. Unsupported và warnings

- `unsupported`: danh sách `sense_id` có `kind = unsupported` mà câu hỏi dùng, ví dụ `balance`. Khi
  có mục này, hệ thống trả lời rằng dữ liệu không đủ, không sinh SQL.
- `warnings`: hiện có một loại, `open_address_set:<entity_id>`. Dùng khi câu hỏi nhắc tới cả một
  thực thể có tập địa chỉ mở; câu trả lời phải nói đây là cận dưới (quyết định 4). Benchmark không
  có loại câu này.

## 6. Quy tắc placeholder

- Đánh số `E1`, `E2`, ... theo **thứ tự xuất hiện** trong câu hỏi.
- Hai mention trỏ cùng một đích thì dùng chung một placeholder.
- `literal_address` và `nil` không có placeholder.

## 7. Ví dụ

**1. Thực thể có tập đóng, nhiều địa chỉ** (đặc thù 1)

```json
{
  "question": "Tornado Cash nhận bao nhiêu ETH trong tuần đầu tháng 9?", "lang": "vi",
  "mentions": [
    {"span": [0, 12], "text": "Tornado Cash", "type": "entity", "target": "tornado-cash", "placeholder": "E1"}
  ],
  "properties": [
    {"sense_id": "received_by", "span": [13, 17], "text": "nhận", "implicit": false},
    {"sense_id": "eth_amount", "span": [18, 31], "text": "bao nhiêu ETH", "implicit": false},
    {"sense_id": "tx_success", "span": null, "text": null, "implicit": true}
  ],
  "time": {"start": "2026-09-01T00:00:00", "end": "2026-09-08T00:00:00", "span": [38, 54], "text": "tuần đầu tháng 9"},
  "unsupported": [], "warnings": []
}
```

**2. Token gọi bằng symbol trùng** (đặc thù 2; 1.208 token mang symbol USDT)

```json
{
  "question": "Có bao nhiêu lượt chuyển USDT trong ngày 5/9?", "lang": "vi",
  "mentions": [
    {"span": [25, 29], "text": "USDT", "type": "token", "target": "0xdac17f958d2ee523a2206206994597c13d831ec7", "placeholder": "E1"}
  ],
  "properties": [
    {"sense_id": "transfer_count", "span": [3, 24], "text": "bao nhiêu lượt chuyển", "implicit": false},
    {"sense_id": "of_token", "span": null, "text": null, "implicit": true}
  ],
  "time": {"start": "2026-09-05T00:00:00", "end": "2026-09-06T00:00:00", "span": [36, 44], "text": "ngày 5/9"},
  "unsupported": [], "warnings": []
}
```

**3. Tham chiếu theo lớp, N mặc định** (đặc thù 3)

```json
{
  "question": "How much ETH did the major DEXs receive on September 5?", "lang": "en",
  "mentions": [
    {"span": [21, 31], "text": "major DEXs", "type": "class", "target": "dex", "placeholder": "E1", "top_n": 5, "top_n_source": "default"}
  ],
  "properties": [
    {"sense_id": "eth_amount", "span": [0, 12], "text": "How much ETH", "implicit": false},
    {"sense_id": "received_by", "span": [32, 39], "text": "receive", "implicit": false},
    {"sense_id": "tx_success", "span": null, "text": null, "implicit": true}
  ],
  "time": {"start": "2026-09-05T00:00:00", "end": "2026-09-06T00:00:00", "span": [40, 54], "text": "on September 5"},
  "unsupported": [], "warnings": []
}
```

Nếu câu hỏi là "5 DEX lớn nhất ..." thì `top_n_source` là `explicit`.

**4. Ví có tên của một sàn**

```json
{
  "question": "Ví Coinbase 10 nhận bao nhiêu ETH ngày 1/9?", "lang": "vi",
  "mentions": [
    {"span": [3, 14], "text": "Coinbase 10", "type": "address", "target": "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43", "placeholder": "E1"}
  ],
  "properties": [
    {"sense_id": "received_by", "span": [15, 19], "text": "nhận", "implicit": false},
    {"sense_id": "eth_amount", "span": [20, 33], "text": "bao nhiêu ETH", "implicit": false},
    {"sense_id": "tx_success", "span": null, "text": null, "implicit": true}
  ],
  "time": {"start": "2026-09-01T00:00:00", "end": "2026-09-02T00:00:00", "span": [34, 42], "text": "ngày 1/9"},
  "unsupported": [], "warnings": []
}
```

**5. Địa chỉ gõ sẵn trong câu hỏi**

```json
{
  "question": "How many transactions did 0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511 send?", "lang": "en",
  "mentions": [
    {"span": [26, 68], "text": "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511", "type": "literal_address", "target": "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511"}
  ],
  "properties": [
    {"sense_id": "tx_count", "span": [0, 21], "text": "How many transactions", "implicit": false},
    {"sense_id": "sent_by", "span": [69, 73], "text": "send", "implicit": false}
  ],
  "time": null, "unsupported": [], "warnings": []
}
```

**6. Cả một thực thể có tập mở** (không có trong benchmark; pipeline trả lời kèm cảnh báo)

```json
{
  "question": "Binance nhận bao nhiêu ETH trong tháng 9?", "lang": "vi",
  "mentions": [
    {"span": [0, 7], "text": "Binance", "type": "entity", "target": "binance", "placeholder": "E1"}
  ],
  "properties": [
    {"sense_id": "received_by", "span": [8, 12], "text": "nhận", "implicit": false},
    {"sense_id": "eth_amount", "span": [13, 26], "text": "bao nhiêu ETH", "implicit": false},
    {"sense_id": "tx_success", "span": null, "text": null, "implicit": true}
  ],
  "time": {"start": "2026-09-01T00:00:00", "end": "2026-10-01T00:00:00", "span": [33, 40], "text": "tháng 9"},
  "unsupported": [], "warnings": ["open_address_set:binance"]
}
```

**7. Tên không có trong KB** (NIL)

```json
{
  "question": "Bybit nhận bao nhiêu ETH trong tháng 9?", "lang": "vi",
  "mentions": [
    {"span": [0, 5], "text": "Bybit", "type": "nil", "nil_reason": "not_in_kb"}
  ],
  "properties": [
    {"sense_id": "received_by", "span": [6, 10], "text": "nhận", "implicit": false},
    {"sense_id": "eth_amount", "span": [11, 24], "text": "bao nhiêu ETH", "implicit": false}
  ],
  "time": {"start": "2026-09-01T00:00:00", "end": "2026-10-01T00:00:00", "span": [31, 38], "text": "tháng 9"},
  "unsupported": [], "warnings": []
}
```

Câu có mention `nil` không có gold SQL: đáp án đúng là báo không nhận ra thực thể.

**8. Dữ liệu không trả lời được**

```json
{
  "question": "Số dư của ví Coinbase 10 là bao nhiêu?", "lang": "vi",
  "mentions": [
    {"span": [13, 24], "text": "Coinbase 10", "type": "address", "target": "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43", "placeholder": "E1"}
  ],
  "properties": [],
  "time": null,
  "unsupported": [{"sense_id": "balance", "span": [0, 5], "text": "Số dư"}],
  "warnings": []
}
```

## 8. Lưu trữ

- Benchmark: mỗi tập (`dev`, `test`) là một file JSON Lines, mỗi dòng gồm bản ghi linking ở trên
  cộng `gold_sql` (SQL có placeholder) và `template_id`.
- Đầu ra của bộ linking: cùng định dạng, thêm `candidates` trong từng mention.
- Mỗi file ghi kèm phiên bản KB: SHA256 của các file CSV trong `data/entity_knowledge_base/` và
  `data/schema/property_*.csv`. Gold chỉ có nghĩa với đúng phiên bản KB đó.

## 9. Cách chấm

| Chỉ số | So sánh |
|---|---|
| Mention P/R/F1 (chặt) | Cùng `span`, `type` và `target` |
| Mention P/R/F1 (lỏng) | `span` giao nhau, cùng `type` và `target` |
| Entity accuracy@1 | Với mỗi mention gold có `target`: ứng viên hạng 1 trùng `target` |
| Entity recall@5 | `target` nằm trong 5 ứng viên đầu (`candidates`) |
| NIL accuracy | Mention gold `nil` được dự đoán là `nil` |
| Class | `target` và `top_n` đều trùng |
| Property P/R | Tập `sense_id` kể cả `implicit`; báo cáo thêm P/R chỉ trên các nghĩa không `implicit` |
| Time | `start` và `end` trùng |
| Unsupported | Tập `sense_id` trùng |
| End-to-end | EX: kết quả SQL sau khi thay placeholder |

## 10. Các quyết định (chốt ngày 2026-11-01)

| # | Điểm | Quyết định |
|---|---|---|
| 1 | `span` | Ghi cho mọi mention, property và time. Benchmark sinh từ template nên vị trí có sẵn khi điền chỗ trống, không tốn công gán tay |
| 2 | Property ngầm | Tính vào điểm property; báo cáo thêm số chỉ tính nghĩa nói ra trong câu |
| 3 | Top-N của lớp | Theo lượt được gọi (`to_address`), ở mức thực thể cha |
| 4 | Câu NIL và unsupported | Khoảng 10% benchmark: khoảng 5% NIL (`not_in_kb`, `not_reviewed`), khoảng 5% unsupported (số dư, giá trị USD, ETH qua internal call). Câu về thực thể có tập mở không có trong benchmark |
| a | Gộp thực thể con | Uniswap V2/V3/V4 tính là một thành viên Uniswap khi xếp hạng lớp |
| b | Thành viên của lớp | Chỉ theo lớp ở mức thực thể đã duyệt; lớp ở mức địa chỉ chỉ dùng cho thực thể nhóm |
| c | Tập địa chỉ của thành viên trong lớp (bổ sung 2026-11-01, phiên benchmark đề xuất, người dùng duyệt) | Chỉ các thực thể trong cây mang lớp đó, trừ contract token (mục 2) |
