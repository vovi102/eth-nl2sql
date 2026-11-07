# Kế hoạch phase 2: fine-tune LoRA

Lập ngày 2026-11-04. Các quyết định do người dùng chọn cùng ngày; chúng được ghi ở
[proposed-method.md](proposed-method.md) §9, quyết định 14–23. Tài liệu này mô tả các bước,
file, lệnh, kiểm tra rò rỉ, tiêu chí dừng và rủi ro. Chưa có bước nào được thực hiện.

Mỗi con số đều kèm nguồn:

- **đo**: đo trong repo, có ghi lệnh hoặc file;
- **trích**: lấy từ tài liệu, có link;
- **ước lượng**: chưa đo; số thật sẽ thay vào sau khi chạy thử.

## 1. Mục tiêu và nguyên tắc

Fine-tune LoRA đúng model của phase 1 (Qwen2.5-Coder-1.5B/3B-Instruct) để model **dùng evidence
và placeholder tốt hơn**, không phải để học thuộc địa chỉ. Prompt giữ nguyên định dạng của
[proposed-method.md](proposed-method.md) §5, và câu hỏi train đi qua chính bộ linking (không
dùng oracle). Đánh giá dùng cùng runtime, cùng file GGUF base và cùng `generate-sql` /
`evaluate-sql` như phase 1, để chênh lệch giữa hai phase chỉ đến từ adapter.

Ba tập dữ liệu có vai trò tách bạch:

| Tập | Template | Dùng để |
|---|---|---|
| train (mới) | khoảng 30 template train-only, chữ ký khác mọi template dev/test | huấn luyện; 5% câu giữ lại chỉ để theo dõi loss |
| dev | 14 template, 177 câu (không đổi) | chọn checkpoint (tối đa 2 lựa chọn mỗi model); so sánh với phase 1 trên dev |
| test | 26 template, 328 câu (không đổi) | đánh giá cuối, chạy theo thứ tự ở mục 7 |

## 2. Dữ liệu huấn luyện

### 2.1 Template train-only (quyết định 14)

Danh mục đã cài trong `src/eth_nl2sql/train_templates.py` (người dùng duyệt 2026-11-05): 30
template, 2.000 câu. Khuôn giống `benchmark_templates.py`: `[cụm](sense)` cho property,
`{slot}` cho mention. Template thường có 2 cách diễn đạt mỗi ngôn ngữ; template nhập nhằng có 1
cách cho mỗi variant.

**Chữ ký** của một câu mẫu gồm ba phần. Nó được tính từ code (`benchmark_templates.TEMPLATES`
cùng `split` trong `manifest.json`), không đọc file câu hỏi:

1. Tập sense, kể cả nghĩa ngầm. Mỗi sense được định danh bằng biểu thức SQL của nó trên bảng mà
   câu truy vấn dùng (`property_senses.csv`). Vì vậy `received_by` ("nhận") và
   `contract_called` ("gọi") là một sense: cả hai đều là `to_address IN {E}` trên
   `transactions`.
2. Các loại mention, đã sắp xếp.
3. Khung SQL ở dạng chuẩn:
   - placeholder và điều kiện thời gian thay bằng ký hiệu;
   - bỏ tên cột kết quả, thứ tự cột, thứ tự điều kiện `AND`/`OR` và thứ tự khoá `GROUP BY`;
   - `SUM(x / k)` viết thành `SUM(x) / k`, `COUNT(1)` thành `COUNT(*)`, `date_trunc('day', t)`
     thành `CAST(t AS DATE)`;
   - số thứ tự cột và tên alias trong `ORDER BY`/`GROUP BY` thay bằng biểu thức, theo đúng cách
     DuckDB gán tên.

Quy tắc kiểm tra cài trong `src/eth_nl2sql/template_signature.py`, chạy bằng
`uv run python -m eth_nl2sql.template_signature`:

- **Vi phạm** (lệnh thoát với mã 1; `train_build` sẽ dừng):
  - trùng id template (R1);
  - trùng chữ ký (R2);
  - cùng mention và cùng tập sense nhưng khung SQL khác: cùng evidence, câu truy vấn khác;
  - cùng mention và cùng khung SQL nhưng tập sense khác: cùng câu truy vấn, gọi tên khác;
  - câu chữ trùng hẳn một câu dev hay test, hoặc giống một câu **test** từ 0,8 trở lên (R3).
- **Gần** (R5, chỉ liệt kê). Thực thể, ví, lớp và địa chỉ gõ sẵn được tính là một họ "tập địa
  chỉ". Các kiểu gần:
  - đổi loại mention trong họ;
  - đổi chiều `to`/`from`;
  - đổi trạng thái `= 1`/`= 0`;
  - đổi ngày ↔ giờ;
  - đổi loại giao dịch (`transaction_type`);
  - "theo ngày/giờ" của một câu tổng có cùng khung SQL, hoặc ngược lại (`dimension`);
  - câu chữ giống một câu **dev** từ 0,8 trở lên.

Bộ quy tắc này chặt hơn bản kế hoạch ngày 2026-11-04. Khi đó, "cùng evidence", "cùng câu truy
vấn" và câu chữ giống câu test mới chỉ bị liệt kê là gần. Quy tắc được siết lại sau vòng rà soát
độc lập (mục 2.5); người dùng duyệt ngày 2026-11-05 (quyết định 25).

Có thêm hai test chạy trên bảng cụm từ thật (`tests/test_train_templates.py`):

- Bộ linking đọc mọi câu train đúng như markup: không thừa hay thiếu sense, không có tên lạ
  (NIL), mention đúng gold, và evidence không còn `{E}` chưa gắn placeholder.
- Không câu train nào trùng một câu dev/test **theo cách bộ linking đọc câu đó**. Test này cần
  thiết vì có 11 câu mẫu test mà markup khác với cách bộ linking đọc.

**Thay đổi so với bản nháp 2026-11-04.** Số liệu đo trên DB thật, 2026-11-04/05.

| Bản nháp | Thay bằng | Lý do |
|---|---|---|
| `entity_eth_sent`, `class_eth_sent` | (bỏ) | Contract không tự gửi giao dịch: chỉ 13/99 thực thể trong pool có giao dịch gửi ETH; lớp `lending` và `layer-2` không có. ETH mà protocol trả ra đi chủ yếu qua giao dịch nội bộ, loại dữ liệu này không có (sense `internal_eth` là unsupported) |
| `entity_distinct_recipients` | `entity_token_recipients` | Như trên: chuyển sang `token_transfers`, nơi 89/99 thực thể có chuyển token đi |
| `daily_block_count`, `daily_avg_base_fee` | `avg_base_fee` | Sense `day` chỉ áp cho `transactions`/`token_transfers`, nên evidence sẽ ghi sai cột thời gian của `blocks` |
| `entity_hourly_calls` | `entity_avg_gas_limit` | Cùng chữ ký với biến thể thực thể của `ambiguous_hourly_times`; cả hai là bản "theo giờ" của test `entity_daily_calls`, trong khi test không hỏi theo giờ |
| `entity_token_sent`, `class_token_sent` | `entity_token_sends_count`, `class_token_recipients` | Khung `TOKEN_SENT` (4 variant, 244 câu) là bản đổi chiều của test `entity_token_received`. Giữ `address_token_sent` và biến thể token của `ambiguous_value_sent` |
| `address_tx_received_count`, (sau đó) `address_failed_sends` | `address_failed_send_fees` | Lần lượt là bản đổi chiều của test `address_tx_sent_count` và `entity_failed_calls` |
| `token_top_senders` | `token_daily_receivers` | Không có mention địa chỉ nên evidence giữ dòng `from_address IN {E}` chưa gắn placeholder, trong khi gold không lọc theo dòng đó. Chỉ test `token_top_receivers` có dòng như vậy |
| `token_hourly_transfers` | `token_involving_transfers` | Bản ngày ↔ giờ của dev `token_daily_transfers` |
| `class_daily_calls`, `class_fees_paid`, `class_failed_calls` | `class_daily_callers`, `class_avg_gas_price`, `class_gas_used` | Đổi loại mention của test `entity_daily_calls`, dev `entity_fees_paid` và test `entity_failed_calls` |
| `eip1559_tx_count` | `eip1559_avg_priority_fee` | Chỉ khác test `blob_tx_count` ở hằng `transaction_type` |
| `literal_eth_sent` | `literal_fees_spent` | Bản đổi chiều của test `literal_eth_received`. Biến thể địa chỉ gõ sẵn của `ambiguous_value_sent` là bản gần duy nhất còn giữ |
| `ambiguous_daily_users`, biến thể thực thể | chỉ giữ biến thể token | Với thực thể, "người dùng" là `to_address IN {E1}`, điều evidence không ghi. Đó đúng là phần mà test `ambiguous_users` buộc model phải tự đoán |
| — | `entity_top_tokens_received`, `top_blocks_by_tx_count`, `total_blob_fee` | Thêm để có top-k nhóm theo token và theo block, cùng một sense chưa dùng (`blob_fee`) |

**Danh mục (duyệt 2026-11-05, quyết định 24).** Ký hiệu trong bảng:

- dấu * là nghĩa ngầm;
- "Gần dev/test" liệt kê các kiểu gần về cấu trúc (R5);
- "Chạy thử" là số câu được lọc L1 giữ lại trên số câu sinh thử (mục 2.5);
- câu mẫu lấy từ lần chạy thử trên DB thật.

#### A. Thực thể tập đóng (480 câu)

| id | n | Mention: sense (* = ngầm) | Gần dev/test (R5) | Chạy thử: L1 giữ | Câu mẫu VI / EN |
|---|---:|---|---|---:|---|
| `entity_avg_gas_price` | 70 | thực thể: `contract_called`, `gas_price_paid` | — | 6/6 | Giá gas trung bình của các giao dịch gọi Wormhole ngày 29/9 là bao nhiêu?<br>What was the average gas price of transactions that called OpenSea from September 23 to 26? |
| `entity_gas_used` | 70 | thực thể: `contract_called`, `gas_used` | — | 6/6 | Lượng gas dùng của mọi giao dịch gọi tới OpenOcean trong nửa đầu tháng 9 là bao nhiêu?<br>Sum the gas used of all calls to Scroll in the first half of September. |
| `entity_avg_gas_limit` | 68 | thực thể: `contract_called`, `gas_limit_tx` | — | 6/6 | Các giao dịch tương tác với PancakeSwap V3 trong tháng 9 đặt giới hạn gas trung bình bao nhiêu?<br>What average gas limit did calls to Aave set in the first half of September? |
| `entity_token_sends_count` | 68 | thực thể+token: `sent_by`, `transfer_count`, `of_token`* | — | 5/6 | Privacy Pools đã chuyển đi USD₮ qua bao nhiêu lượt chuyển trong tuần thứ 3 tháng 9?<br>Count the token EIGEN transfers sent from EigenLayer from September 12 to 16. |
| `entity_involving_tx_count` | 68 | thực thể: `involving`, `tx_count` | — | 6/6 | Số giao dịch liên quan tới Ethereum Name Service từ 8/9 đến 12/9 là bao nhiêu?<br>How many transactions in September involved Convex? |
| `entity_token_recipients` | 68 | thực thể+token: `distinct_receivers`, `sent_by`, `of_token`* | — | 6/6 | Có bao nhiêu địa chỉ nhận token Coinbase Wrapped BTC được chuyển từ Tokenlon từ 17/9 đến 22/9?<br>To how many unique receivers did Dln send the token PeiPei in September? |
| `entity_top_tokens_received` | 68 | thực thể: `received_by`, `transfer_count` | — | 4/6 | Liệt kê 5 token mà Lido Finance nhận nhiều nhất trong tuần thứ 2 tháng 9 theo số lượt chuyển.<br>Which 5 tokens did deBridge Finance receive in the most transfers on September 23? |

#### B. Ví hoặc contract có tên (280)

| id | n | Mention: sense (* = ngầm) | Gần dev/test (R5) | Chạy thử: L1 giữ | Câu mẫu VI / EN |
|---|---:|---|---|---:|---|
| `address_daily_eth_received` | 70 | ví: `day`, `eth_amount`, `received_by`, `tx_success`* | dev `ambiguous_value_received` (theo ngày/giờ)<br>dev `class_eth_received` (theo ngày/giờ)<br>test `address_eth_received` (theo ngày/giờ)<br>test `ambiguous_volume` (theo ngày/giờ)<br>test `entity_eth_received` (theo ngày/giờ)<br>test `literal_eth_received` (theo ngày/giờ) | 6/6 | Thống kê lượng ETH mà contract Dln: Destination nhận theo ngày trong nửa đầu tháng 9.<br>Show the daily ETH amount sent to Uniswap V4: Universal Router in the last week of September. |
| `address_fees_spent` | 70 | ví: `sent_by`, `tx_fee` | dev `entity_fees_paid` (đổi chiều) | 6/6 | Tổng phí gas mà ví Gemini: Deployer 2 bỏ ra khi gửi giao dịch từ 13/9 đến 19/9 là bao nhiêu?<br>How much gas fee was spent by transactions sent from Kraken: Hot Wallet 4 in the first half of September? |
| `address_token_sent` | 70 | ví+token: `sent_by`, `token_amount`, `of_token`* | dev `address_token_received` (đổi chiều)<br>dev `ambiguous_value_received` (đổi chiều)<br>dev `class_token_received` (đổi chiều)<br>test `entity_token_received` (đổi chiều) | 6/6 | Tính tổng lượng NEXO mà ví Bitget 5 đã chuyển đi trong tuần cuối tháng 9.<br>How much StargateToken was withdrawn from Stargate Finance: STG Token on September 4? |
| `address_failed_send_fees` | 70 | ví: `sent_by`, `tx_failed`, `tx_fee` | — | 6/6 | Tổng phí giao dịch mà ví Wintermute 4 trả cho các giao dịch thất bại đã gửi trong tháng 9 là bao nhiêu?<br>How much gas fee did CoinSpot 10 spend on failed transactions it sent in the first half of September? |

#### C. Token (340)

| id | n | Mention: sense (* = ngầm) | Gần dev/test (R5) | Chạy thử: L1 giữ | Câu mẫu VI / EN |
|---|---:|---|---|---:|---|
| `token_daily_amount` | 68 | token: `day`, `token_amount`, `of_token`* | test `ambiguous_volume` (theo ngày/giờ)<br>test `token_amount_total` (theo ngày/giờ) | 6/6 | Lượng DOS luân chuyển mỗi ngày trong nửa đầu tháng 9 ra sao?<br>What amount of Concrete Token was transferred per day in September? |
| `token_transfers_to_address` | 68 | ví+token: `received_by`, `transfer_count`, `of_token`* | — | 6/6 | Lido DAO Token được gửi tới ví BloFin: Hot Wallet 1 bao nhiêu lượt?<br>How many transfers of Liquid staked Ether 2.0 did Lido: wstETH Token receive on September 28? |
| `token_involving_transfers` | 68 | ví+token: `involving`, `transfer_count`, `of_token`* | — | 6/6 | Có bao nhiêu lượt chuyển FUN liên quan đến ví CoinSpot 10 trong nửa đầu tháng 9?<br>In how many transfers of Aave Token was Bitstamp 2 involved in the first week of September? |
| `token_daily_receivers` | 68 | token: `day`, `distinct_receivers`, `of_token`* | — | 6/6 | Thống kê số địa chỉ nhận token Spiral theo ngày trong nửa đầu tháng 9.<br>Show the daily number of unique receivers of the token Dai Stablecoin in the third week of September. |
| `token_distinct_receivers` | 68 | token: `distinct_receivers`, `of_token`* | dev `token_distinct_senders` (đổi chiều)<br>test `ambiguous_users` (đổi chiều) | 5/6 | Có bao nhiêu địa chỉ nhận token USDG khác nhau trong tháng 9?<br>How many unique receivers got the token XAUt in September? |

#### D. Theo lớp (300)

| id | n | Mention: sense (* = ngầm) | Gần dev/test (R5) | Chạy thử: L1 giữ | Câu mẫu VI / EN |
|---|---:|---|---|---:|---|
| `class_daily_callers` | 76 | lớp: `contract_called`, `day`, `distinct_senders` | dev `entity_distinct_callers` (theo ngày/giờ)<br>test `class_distinct_callers` (theo ngày/giờ) | 6/6 | Mỗi ngày từ 12/9 đến 18/9 có bao nhiêu người dùng tương tác với 2 cầu nối chuỗi lớn nhất?<br>How many unique senders called the top 4 lending protocols per day in the third week of September? |
| `class_avg_gas_price` | 76 | lớp: `contract_called`, `gas_price_paid` | — | 6/6 | Giá gas trung bình khi gọi 5 L2 lớn nhất trong tháng 9 là bao nhiêu?<br>What was the mean gas price of calls to the major lending protocols? |
| `class_token_recipients` | 74 | lớp+token: `distinct_receivers`, `sent_by`, `of_token`* | — | 6/6 | Số địa chỉ nhận token Tether USD chuyển từ 4 layer 2 lớn nhất trong nửa đầu tháng 9 là bao nhiêu?<br>To how many unique receivers did the top 3 DEX aggregators send the token SPIRAL in the third week of September? |
| `class_gas_used` | 74 | lớp: `contract_called`, `gas_used` | — | 6/6 | Tổng lượng gas dùng của các giao dịch gọi 5 giao thức cho vay lớn nhất trong tháng 9 là bao nhiêu?<br>What was the combined gas used of calls to the major bridges from September 7 to 11? |

#### E. Property nhập nhằng (260)

| id | n | Mention: sense (* = ngầm) | Gần dev/test (R5) | Chạy thử: L1 giữ | Câu mẫu VI / EN |
|---|---:|---|---|---:|---|
| `ambiguous_daily_volume` | 66 | thực thể: `day`, `eth_amount`, `received_by`, `tx_success`*<br>lớp: `day`, `eth_amount`, `received_by`, `tx_success`*<br>token: `day`, `token_amount`, `of_token`* | dev `ambiguous_value_received` (theo ngày/giờ)<br>dev `class_eth_received` (theo ngày/giờ)<br>test `address_eth_received` (theo ngày/giờ)<br>test `ambiguous_volume` (theo ngày/giờ)<br>test `entity_eth_received` (theo ngày/giờ)<br>test `literal_eth_received` (theo ngày/giờ)<br>test `token_amount_total` (theo ngày/giờ) | 16/18 | Thống kê khối lượng mà 0x nhận theo ngày từ 19/9 đến 24/9.<br>Show the volume 1inch v5 received per day in the first half of September. |
| `ambiguous_hourly_times` | 66 | thực thể: `contract_called`, `hour`, `tx_count`<br>token: `hour`, `transfer_count`, `of_token`* | dev `token_daily_transfers` (ngày↔giờ)<br>test `ambiguous_times` (theo ngày/giờ)<br>test `class_call_count` (theo ngày/giờ)<br>test `entity_call_count` (theo ngày/giờ)<br>test `entity_daily_calls` (ngày↔giờ)<br>test `token_transfer_count` (theo ngày/giờ) | 11/12 | Liệt kê số lượt PancakeSwap V3 được gọi theo giờ ngày 8/9.<br>List the hourly count on September 17 of how many times Liquity got called. |
| `ambiguous_value_sent` | 64 | địa chỉ gõ sẵn: `eth_amount`, `sent_by`, `tx_success`*<br>ví+token: `sent_by`, `token_amount`, `of_token`* | dev `address_eth_sent` (đổi loại mention)<br>dev `address_token_received` (đổi chiều)<br>dev `ambiguous_value_received` (đổi chiều)<br>dev `class_eth_received` (đổi chiều)<br>dev `class_token_received` (đổi chiều)<br>test `address_eth_received` (đổi chiều)<br>test `ambiguous_volume` (đổi chiều)<br>test `entity_eth_received` (đổi chiều)<br>test `entity_token_received` (đổi chiều)<br>test `literal_eth_received` (đổi chiều) | 12/12 | Tổng giá trị mà địa chỉ 0x18dd3c14e34c1bc379f7538068c59160d9f68e25 đã gửi đi trong tháng 9 là bao nhiêu?<br>What total value did 0x5b43453fce04b92e190f391a83136bfbecedefd1 send out in the second week of September? |
| `ambiguous_daily_users` | 64 | token: `day`, `distinct_senders`, `of_token`* | dev `token_distinct_senders` (theo ngày/giờ)<br>test `ambiguous_users` (theo ngày/giờ) | 6/6 | Thống kê số người dùng của token Decentraland MANA theo ngày trong tuần đầu tháng 9.<br>Show the daily number of users of the token PayPal USD from September 22 to 26. |

#### F. Không có thực thể có tên / địa chỉ gõ sẵn (340)

| id | n | Mention: sense (* = ngầm) | Gần dev/test (R5) | Chạy thử: L1 giữ | Câu mẫu VI / EN |
|---|---:|---|---|---:|---|
| `hourly_tx_count` | 58 | —: `hour`, `tx_count` | — | 6/6 | Số giao dịch mỗi giờ ngày 22/9 là bao nhiêu?<br>What was the hourly number of transactions on September 27? |
| `eip1559_avg_priority_fee` | 58 | —: `priority_fee_cap`, `tx_eip1559` | — | 6/6 | Các giao dịch EIP-1559 trong tuần cuối tháng 9 đặt phí ưu tiên trung bình bao nhiêu gwei?<br>What was the average priority fee of EIP-1559 transactions in the last week of September? |
| `literal_fees_spent` | 56 | địa chỉ gõ sẵn: `sent_by`, `tx_fee` | dev `entity_fees_paid` (đổi chiều) | 6/6 | 0x9430801ebaf509ad49202aabc5f5bc6fd8a3daf8 đã tiêu bao nhiêu phí gas cho các giao dịch gửi đi trong nửa đầu tháng 9?<br>What transaction fee was paid in total by transactions sent from 0x3e0a91dc5848e17765e3167249a2cb018cbb60ee on September 24? |
| `avg_base_fee` | 56 | —: `base_fee` | — | 6/6 | Trung bình phí cơ sở của các block ngày 6/9 là bao nhiêu?<br>What was the average base fee in September? |
| `top_blocks_by_tx_count` | 56 | —: `block_tx_count` | — | 6/6 | Liệt kê 5 block đứng đầu về số giao dịch trong block.<br>Which 5 blocks ranked highest in transactions per block on September 25? |
| `total_blob_fee` | 56 | —: `blob_fee` | — | 6/6 | Các giao dịch đã trả tổng cộng bao nhiêu phí blob?<br>How much blob fee did transactions pay in total in the first half of September? |


### 2.2 Kích thước và tỉ lệ (quyết định 16)

- **2.000 câu trước khi lọc.** Tỉ lệ nhóm theo benchmark §5, bỏ NIL và unsupported, rồi chuẩn
  hoá lại:

  | Nhóm | Tỉ lệ | Số câu |
  |---|---:|---:|
  | entity | 24% | 480 |
  | address | 14% | 280 |
  | token | 17% | 340 |
  | class | 15% | 300 |
  | ambiguous | 13% | 260 |
  | plain | 17% | 340 |

  Số câu chia đều cho các template trong nhóm, tức 56–76 câu mỗi template, luôn là số chẵn.
- VI/EN 1:1 trong từng template, theo cách luân phiên của `benchmark_build.generate`.
- **Không có câu NIL hay unsupported.** Với các câu này `decision` khác `sql`, nên model không
  bao giờ được gọi (quyết định 16).
- Seed riêng là 20261104, khác seed benchmark 20261101. Áp dụng như benchmark:
  - ưu tiên mềm `tx_success` ([benchmark.md](benchmark.md) §6 bước 5);
  - bỏ câu hoà ở `LIMIT` (bước 6);
  - chỉ giữ câu chạy dưới 10 giây và có kết quả khác rỗng/0.

  Không giữ lại câu hoà vì đích sẽ không xác định được.
- Pool giá trị điền dùng chung với benchmark (đo bằng `load_fillers`, 2026-11-04): 99 thực thể,
  472 ví có tên, 211 token, 5 lớp, 200 địa chỉ literal. Thực thể, token, lớp và thời gian được
  phép trùng với dev/test (quyết định 15).
- Thời gian sinh: khoảng 16 phút (**ước lượng**, suy từ 505 câu benchmark mất khoảng 4 phút,
  [benchmark.md](benchmark.md) §8).

### 2.3 Linking thật và lọc nhiễu (quyết định 17, L1)

Mỗi câu train đi qua `EntityLinker`, đúng như `evaluate-linking`. Câu chỉ được giữ khi đồng thời:

1. `decision` của bộ linking là `sql`;
2. placeholder của bộ linking trùng gold: cùng đích, cùng thứ tự `{E1}`, `{E2}`, cùng `top_n`
   với lớp;
3. gold SQL, sau khi thay placeholder bằng địa chỉ **của bản ghi linking**, cho cùng kết quả với
   gold (`same_rows`).

Nghĩa property, span và thời gian được phép lệch. Đó là nhiễu thật mà model cần chịu được.
Số câu bị loại được ghi vào manifest theo template và lý do.

Nếu một template mất hơn 20% số câu, xem lại câu chữ của template đó (cần hỏi người dùng).
Không sửa `property_phrases` hay KB cho phase 2: sửa chúng sẽ làm đổi linking của dev/test, và
khi đó phải chạy lại phase 1.

**Phần mất đã biết trước, không tính vào ngưỡng 20%.**

- Câu có một sense áp cho token nhưng slot là thực thể: `entity_top_tokens_received`, biến thể
  thực thể của `ambiguous_daily_volume` và `ambiguous_hourly_times`. Bộ linking đặt ngữ cảnh
  "token", nên thực thể có tên trùng tên token (Uniswap, Tether, Pendle, yearn.finance…) bị link
  thành token.
- Vòng rà soát (mục 2.5) đo được khoảng 19–20% câu bị mất vì lý do này. Câu chữ không tránh
  được, trừ khi thêm từ loại "giao thức", mà từ này sai với 19/99 thực thể không phải protocol.
- Đây là lỗi thật của bộ linking, và câu test cùng loại cũng mất như vậy. Báo cáo S2 ghi riêng số
  câu này.

**Việc S2 phải làm thêm** (phát hiện ở S1):

- Bộ lọc `token_out`: token mà đích đã chuyển đi (`token_transfers.from_address`), thu hẹp pool
  token cho cả đích là lớp, như `token_in` đang làm. `benchmark_build._fill_slots` hiện chưa có
  bộ lọc này.
- Loại mọi bản ghi mà evidence còn `{E}` chưa gắn placeholder.
- Báo tỉ lệ câu train mà tập sense của bộ linking khác gold. Trên câu mẫu tỉ lệ này gần 0, xem
  mục 2.5.

### 2.4 Định dạng ví dụ (quyết định 19)

- `messages` của ví dụ train do `PromptBuilder.build(linking_record)` dựng, với tuỳ chọn mặc định:
  schema đủ, có evidence, dùng placeholder. Đây đúng là prompt mà `generate-sql` sẽ gửi.
- Đích (assistant) là `` ```sql\n<gold_sql còn placeholder>\n``` ``. Không có ví dụ sửa lỗi:
  vòng hỏi lại lúc suy luận giữ nguyên như phase 1.
- Độ dài: prompt dev dài 4.253–4.448 token, phần trả lời trung bình 110 token (**đo**:
  `data/predictions/dev.prompt-tokens.json` và các lần thử của 3B trên dev). Độ dài train được đo
  lại bằng `count-prompt-tokens`; câu nào dài hơn 6.144 token thì bị loại và ghi lại.

### 2.5 Chạy thử và rà soát độc lập ở S1 (2026-11-05)

**Chạy thử trên DB thật.**

- Sinh 6 câu cho mỗi variant bằng chính `benchmark_build.generate`, seed 20261104. Bộ lọc
  `token_out` được giả lập bằng tập token mà đích đã chuyển đi.
- Mỗi câu được link bằng bộ linking thật và lọc theo L1.
- Kết quả theo template ở bảng danh mục, mục 2.1.

Kết quả:

- 204 câu được sinh. Mọi gold SQL chạy dưới 10 giây và có kết quả khác rỗng/0.
- Lọc L1 giữ **197 câu (96,6%)**; 200 câu có tập sense của bộ linking đúng gold.
- Cả 7 câu bị loại đều do bộ linking:
  - tên thực thể trùng tên token: Pendle, yearn.finance, Aave, "Ethereum Name Service" (token
    ENS);
  - alias token lạ: "StarkNet Token", "Wrapped liquid staked Ether 2.0".

  Không câu nào bị loại vì câu chữ của template.
- Lần chạy thử đầu, trước khi sửa, đã bắt được hai lỗi câu chữ: "lời gọi" bị đọc thành `tx_failed`
  và câu bắt đầu bằng "Name" bị link thành tên lạ.

Script chạy thử nằm ngoài repo; ở S2 nó được thay bằng `train_build --count-scale`.

**Rà soát độc lập.** Một workflow gồm 8 agent rà soát theo 4 góc nhìn:

- ngữ nghĩa SQL;
- câu chữ VI/EN và cách bộ linking đọc từng cụm từ;
- tìm cách lách bộ kiểm tra rò rỉ;
- rò rỉ ở mức thiết kế.

Mỗi phát hiện được một agent khác kiểm chứng theo hướng phản bác. Có 35 phát hiện; 24 được xác
nhận, 11 bị bác. Toàn bộ thay đổi theo các phát hiện đã xác nhận đều có ở mục 2.1 và 2.3:

- `token_top_senders` để lại `{E}` chưa gắn placeholder trong evidence, và câu "Xếp hạng…" bị
  link thành tên lạ (NIL).
- Biến thể thực thể của `ambiguous_daily_users` dạy đúng quy ước mà evidence của test
  `ambiguous_users` không ghi.
- 5 template là bản đổi chiều hoặc đổi loại giao dịch của template test.
- Câu chứa token thiếu dấu hiệu "token", nên token trùng tên thực thể (Uniswap, AAVE) bị link
  thành thực thể. Mất khoảng 11–13% số câu.
- Bộ kiểm tra cũ để lọt bản viết lại cùng nghĩa của template test (`COUNT(1)`, đảo thứ tự điều
  kiện, `SUM(value / 1e18)`, sense đồng nghĩa) và gắn nhầm kiểu `dimension`. Đã viết lại theo
  mục 2.1 và thêm test cho từng cách lách.

Các phát hiện bị bác (không sửa):

- mint từ địa chỉ 0x0 lọt vào "top người gửi";
- tiền tố "contract" cho ví gửi tiền;
- `address_fees_spent` không tính phí blob;
- cụm "changed hands" với mint/burn.

Lý do chung: gold theo đúng quy ước của sense đã duyệt, và benchmark cũng làm như vậy.

**Mốc so sánh.** Áp cùng quy tắc cho chính benchmark (dev so với test) thì có:

- 2 cặp trùng chữ ký (ví dụ `ambiguous_value_received`/ví của dev ≡ `address_eth_received` của
  test);
- 3 cặp cùng evidence, 1 cặp cùng câu truy vấn;
- 7 cặp trùng câu chữ, 15 cặp câu chữ giống từ 0,8 trở lên;
- 21 cặp gần về cấu trúc.

Tập train so với test: 0 vi phạm.

### 2.6 Kết quả S2 (2026-11-05)

Lệnh `uv run python -m eth_nl2sql.train_build` chạy hết 20 phút và ghi vào `data/train/`
(`train.jsonl`, `dropped.jsonl`, `sft.jsonl`, `manifest.json`, `sft.tokens.json`).

- **Số câu.** Sinh 2.000, lọc L1 giữ **1.968 (98,4%)**, trong đó 98 câu là `val`. VI/EN là
  981/987.
- **Theo nhóm.** entity 467, address 275, token 338, class 300, ambiguous 248, plain 340.
- **Nghĩa.** Không câu train nào có tập sense của bộ linking khác gold (`senses_mismatch` = 0).
- **32 câu bị loại**, đều là lỗi thật của bộ linking. Mỗi dòng trong `dropped.jsonl` ghi kèm cách
  bộ linking đọc câu đó.
  - 20 câu: tên thực thể bị đọc thành token (`cause` = `entity read as token`). Template mất
    nhiều nhất là `entity_top_tokens_received`, 8/68.
  - 11 câu `decision nil`: tên dài bị tách ra thành một phần lạ, ví dụ "Wrapped liquid staked
    Ether 2.0", "CoW Swap Eth Flow", "SPDR S&P 500 ETF (Ondo Tokenized)".
  - 1 câu: token có alias viết thường "send" không được link.

  Không template nào mất quá 20% số câu.
- **Thực thể giữ riêng** (quyết định 27, rút trong 41 ứng viên): `ambient-finance`, `axelar`, `beacon-deposit-contract`, `celer-network`, `chainlink`, `convex-finance`, `debridge`, `ether-fi`, `gearbox-protocol`, `livepeer`, `odos`, `openocean`, `opensea`, `optimism`, `paxos-gold`, `ring-protocol`, `shiba-inu`, `stargate`, `wormhole`, `yearn`.
  - Đã bỏ khỏi giá trị điền 57 ví/contract và 9 token mang tên của chúng.
  - Không câu nào phải loại vì còn nêu tên chúng.
  - Kiểm tra độc lập trên `sft.jsonl` với mọi alias đang dùng: 0 prompt nêu tên, 0 mention trỏ
    tới chúng.
- **Định dạng.** B8 (`extract_sql`) đọc lại đúng gold SQL ở cả 1.968 câu trả lời; không evidence
  nào còn `{E}` chưa gắn placeholder; không câu nào trùng câu dev.
- **Độ dài**, đếm bằng tokenizer của Qwen (`count-prompt-tokens`, tính cả chat template và câu
  trả lời): 4.277–4.624 token, trung vị 4.462. Mọi ví dụ đều dưới mức 6.144 của quyết định 18.

**Rà soát độc lập S2.** 8 agent, 4 góc nhìn (logic, định dạng, dữ liệu, test), mỗi phát hiện được
kiểm chứng theo hướng phản bác. Có 19 phát hiện; 8 được xác nhận, 11 bị bác.

- Phát hiện quan trọng nhất: lần rút đầu tiên chọn ngẫu nhiên trong cả 99 thực thể, nên 17/20
  thực thể giữ riêng vẫn bị nêu tên trong 309/1.960 prompt train, qua danh sách thành viên lớp,
  tên token và tên ví/contract. Người dùng chọn rút lại theo quy tắc mới (quyết định 27), rồi
  chạy lại.
- Các phát hiện khác đã sửa:
  - `dropped.jsonl` chưa ghi cách bộ linking đọc câu;
  - manifest thiếu tham số chạy và nhóm câu chữ giống dev;
  - test cũ chỉ chạy qua các template không có mention, chưa chạm tới lý do loại nào của L1;
  - kế hoạch mô tả sai các trường của `sft.jsonl`.

## 3. Kiểm tra chống rò rỉ

Chạy tự động trong `train_build` và có test riêng. Gặp lỗi thì dừng, không ghi file. R1–R3, R5
và R9 đã cài ở S1 (`template_signature.py`, `tests/test_template_signature.py`,
`tests/test_train_templates.py`); R4 và R6–R8 cài ở S2 và S8.

| # | Kiểm tra | Cách làm |
|---|---|---|
| R1 | Không dùng template của test hoặc dev | `template_id` của train ∩ khoá `templates` của `manifest.json` = ∅ |
| R2 | Không trùng template, dù viết khác | Không trùng chữ ký; không cùng evidence; không cùng câu truy vấn (mục 2.1). Tính từ code, không đọc `test.jsonl`. Có test so thêm với cách bộ linking đọc từng câu mẫu dev/test |
| R3 | Câu chữ không trùng | Câu mẫu train, sau khi bỏ markup và slot và chuẩn hoá bằng `normalize_alias`, phải khác mọi câu mẫu dev/test. Giống một câu test từ 0,8 trở lên (tỉ lệ từ chung theo `difflib`) là vi phạm; giống một câu dev thì chỉ liệt kê |
| R4 | Câu hỏi không trùng dev | So chuỗi câu hỏi đã chuẩn hoá với `dev.jsonl`. Với test, R2 và R3 đã bảo đảm câu không thể trùng |
| R5 | Liệt kê template gần | `template_signature --report`: với mỗi template train, các template dev/test gần nó và kiểu gần; với mỗi template dev/test, các template train gần nó. Báo cáo cuối chia EX test theo nhóm gần/xa (mục 5.5) |
| R6 | Không gửi dev/test lên Kaggle | Dataset huấn luyện chỉ có `sft.jsonl` của train. Riêng ablation lượng tử hoá (mục 5.4) cần prompt dev: đăng thành một dataset riêng, phải hỏi trước. Không bao giờ đăng test |
| R7 | Đóng băng | Manifest train ghi SHA256 của `train.jsonl`, `sft.jsonl`, cấu hình huấn luyện, `knowledge_base_version` và seed, **trước** khi chạy test phase 1 (mục 7, S3) |
| R8 | Thực thể đã thấy / chưa thấy | Train giữ riêng 20 thực thể mà không prompt train nào nêu tên (quyết định 27): không ô thực thể, không ví/contract hay token mang tên chúng, không nằm trong danh sách thành viên lớp; `train_build` kiểm tra lần cuối evidence và câu hỏi của từng ví dụ. Danh sách ở `data/train/manifest.json` (`holdout_entities`, `holdout`). Lúc đánh giá cuối, với `--allow-test`: `evaluate-sql --train-manifest` chia EX test theo việc thực thể đích của câu có thuộc nhóm giữ riêng không; chỉ in số đếm. Nhóm giữ riêng là các thực thể ít nổi bật hơn (không phải thành viên lớn của lớp), nên so **mức tăng** phase 2 − phase 1 giữa hai nhóm, không so EX tuyệt đối |
| R9 | Bộ linking đọc câu train đúng như markup | Test với bảng cụm từ thật: đúng tập sense, không có tên lạ, mention đúng gold, evidence không còn `{E}` chưa gắn placeholder |

## 4. Huấn luyện trên Kaggle (quyết định 18)

**Môi trường.**
- GPU T4 16 GB. Còn **30,00/30 giờ GPU**, làm mới ngày 2026-11-07 (**đo**: `kaggle quota`,
  2026-11-04).
- Giới hạn 12 giờ mỗi phiên **chưa xác minh**: trang tài liệu Kaggle không đọc được bằng fetch.
  Phải kiểm tra trong giao diện trước khi chạy.
- T4 không hỗ trợ bf16, nên dùng fp16 mixed precision.

**Cấu hình** (giống nhau cho 1.5B và 3B):

| Tham số | Giá trị | Nguồn |
|---|---|---|
| Base | `Qwen/Qwen2.5-Coder-{1.5B,3B}-Instruct`, ghim revision lúc chạy | |
| Phương pháp | LoRA trên trọng số fp16; chuyển sang QLoRA NF4 nếu OOM | QLoRA: [2305.14314](https://arxiv.org/abs/2305.14314) |
| Module | q, k, v, o, gate, up, down | QLoRA §4: cần LoRA ở mọi lớp linear |
| r / α / dropout | 16 / 32 / 0,05 | ước lượng (mặc định phổ biến) |
| lr | 2e-4, warmup 5%, cosine | lr theo QLoRA (7B); lịch theo OmniSQL ([2503.02240](https://arxiv.org/html/2503.02240) §6.1.3) |
| Epoch | 2; lưu adapter ở cuối mỗi epoch | OmniSQL §6.1.3 |
| Batch | 1 × tích luỹ gradient 16 | ước lượng |
| Độ dài chuỗi | 6.144 | mục 2.4 |
| Loss | chỉ trên phần assistant; chỉ tính logits ở đó (logits fp32 của cả chuỗi tốn khoảng 2,7 GB) | tự tính: 4,4k × 151.936 × 4 byte |
| Bộ nhớ | gradient checkpointing | |
| Thư viện | TRL + PEFT, ghim phiên bản; một T4 (DDP 2×T4 chỉ dùng nếu thiếu thời gian) | |
| Seed | 0 | |

**Đầu ra mỗi lần chạy:**
- adapter PEFT của từng checkpoint;
- adapter GGUF f16, chuyển ngay trên Kaggle bằng `convert_lora_to_gguf.py` của llama.cpp tag
  b12032 (chỉ cần `gguf-py`, không biên dịch), khoảng 60 MB với 3B (**ước lượng** từ số tham số
  LoRA);
- log loss;
- `pip freeze` và thông tin GPU.

Tải về bằng `kaggle kernels output`.

**Thời gian** (**ước lượng**; bước chạy thử sẽ đo s/bước và VRAM thật):
- Một ví dụ 3B tốn khoảng 6·N·4,4k·1,2 ≈ 1e14 FLOP. Với T4 đạt 25–35% của 65 TFLOPS fp16, mất
  khoảng 4–6 giây.
- 1.5B mất khoảng một nửa.

## 5. Đánh giá (quyết định 20, 22)

### 5.1 Runtime

- `llama-server` b12032 local chạy **đúng file Q4_K_M của phase 1** (SHA256 trong README), thêm
  `--lora <adapter>.gguf`. Các tham số khác giữ nguyên: `-c 8192 -ngl 99 -fa on -np 1
  --cache-ram 0`.
- `generate-sql` giữ prefix cố định, kiểm tra placeholder và một lần hỏi lại.
- DB 10 GB (**đo**: `du -sh data/db/ethereum.duckdb`) chỉ có ở local. Vì vậy B8 trực tuyến chỉ
  chạy được ở local, và đây là lý do không đánh giá trên Kaggle.

### 5.2 Kiểm tra runtime trước khi đánh giá

Chạy dev với `--lora-scaled <adapter>:0`, tức adapter đã nạp nhưng hệ số bằng 0. Câu trả lời
phải **giống hệt** lần chạy phase 1 (`data/predictions/dev.predictions.prompts.qwen2.5-coder-3b-instruct-q4_k_m.jsonl`).
Nếu khác, việc nạp adapter đã làm đổi runtime, và phải sửa trước khi đánh giá tiếp.

### 5.3 Chọn checkpoint

- Mỗi model có 2 checkpoint (cuối epoch 1 và cuối epoch 2), chấm bằng EX trên dev.
- Chọn checkpoint có EX cao hơn; nếu bằng nhau thì chọn epoch 2. Quy tắc này chốt trước khi chạy.
- Số dev của phase 2 vì vậy hơi lạc quan, và phải ghi rõ như vậy.

### 5.4 Ablation lượng tử hoá (chỉ trên dev, chỉ với 3B)

- Trên Kaggle, sinh bằng transformers fp16, giải mã tham lam, 512 token, chỉ một lần thử. Chạy
  hai cấu hình: base và base + LoRA.
- Chấm local bằng `evaluate-sql`.
- So với Q4_K_M chạy local ở cùng điều kiện: `--no-placeholder-check --no-repair` (cờ mới).
- Tốn khoảng 1 giờ GPU (**ước lượng**).
- Kết quả đo hai chỗ lệch cùng lúc: lượng tử hoá của base, và việc adapter học trên fp16 nhưng
  chạy trên Q4_K_M.

### 5.5 Các cấu hình chạy cho phase 2 (R1)

| Cấu hình | dev | test |
|---|---|---|
| 1.5B + LoRA, mặc định | ✓ | ✓ |
| 3B + LoRA, mặc định | ✓ | ✓ |
| 3B + LoRA, linking oracle | ✓ | ✓ |
| 3B + LoRA, `--no-placeholder-check` | ✓ | ✓ |
| Lượng tử hoá fp16 / Q4_K_M (mục 5.4) | ✓ | |

Không chạy `--no-evidence`, `--literal` hay `--schema linked` cho phase 2. Adapter chỉ được train
trên định dạng mặc định, nên các dòng này sẽ đo độ lệch định dạng chứ không đo giá trị của từng
thành phần. Đây là chỗ lệch có chủ đích so với câu "mỗi cấu hình chạy cho cả hai phase" của
[proposed-method.md](proposed-method.md) §7.

Báo cáo EX theo cùng cách chia như phase 1 (ngôn ngữ, nhóm, đặc thù, `discriminating`), cộng
thêm ba cách chia:

- **Thực thể đã thấy / chưa thấy** (R8).
- **Template gần / xa** (R5). Theo danh mục hiện tại, 13/22 template test trả lời được (180/296
  câu) có một template train gần. 9 template xa:
  - `address_largest_eth_sent` và `token_largest_transfer`: cần `LIMIT 1` (lấy dòng lớn nhất),
    train không có;
  - `class_top_contracts`: cần top-k nhóm theo địa chỉ trên `transactions`, train không có;
  - `contract_creation_count`: cần `contract_creation` cùng `tx_success` nói rõ trong câu, train
    không có;
  - `token_top_receivers`: evidence có dòng `to_address IN {E}` chưa gắn placeholder, chỉ
    template này có;
  - `address_tx_sent_count`, `blob_tx_count`, `daily_avg_gas_price`, `entity_failed_calls`: các
    kỹ năng thành phần đều có trong train, nhưng không có template train gần.

  So sánh gần và xa vì vậy lẫn hai yếu tố, độ gần template và việc có kỹ năng hay không, nên phải
  đọc theo từng template.
- **Nghĩa của bộ linking có khớp gold không.** Bộ linking đọc đúng mọi câu mẫu train (R9).
  Ngược lại, theo vòng rà soát (mục 2.5), 12/86 câu mẫu test trong 8/22 template bị đọc lệch: thừa
  dòng `transfer_count`, thiếu `tx_count`, `token_amount` hoặc `eth_amount`. Adapter chỉ được
  train trên evidence đúng, nên thay đổi EX trên các câu bị đọc lệch đo độ bền với nhiễu chưa gặp
  khi train, không đo khả năng dùng evidence tốt hơn.

**Mốc phase 1 trên test (S4, 2026-11-06; [proposed-method.md](proposed-method.md) §8).** Chỉ tính
296 câu trả lời được:

| Phần | Số câu | 3B mặc định | 3B oracle | 1.5B |
|---|---|---|---|---|
| Template gần | 180 | 157 (0,872) | 158 (0,878) | 117 (0,650) |
| Template xa | 116 | 63 (0,543) | 69 (0,595) | 52 (0,448) |
| Nghĩa property của bộ linking khớp gold | 259 | 208 (0,803) | 209 (0,807) | 154 (0,595) |
| Nghĩa property lệch gold | 37 | 12 (0,324) | 18 (0,486) | 15 (0,405) |

Trên toàn bộ test, bộ linking đọc lệch nghĩa property ở 37/296 câu (12,5%) thuộc 9 template. Tỉ
lệ này gần với 12/86 câu mẫu (14%) của vòng rà soát. Lỗi tập trung ở vài template: 13/13 câu
`token_largest_transfer`, và 5 câu ở mỗi template `entity_failed_calls`, `blob_tx_count`,
`token_top_receivers`. Phần chia "thực thể đã thấy / chưa thấy" cần manifest train nên chỉ có
ở phase 2. Hai điểm từ S4 cần nhớ khi đọc kết quả phase 2:

- 5 câu EN của `class_top_contracts` bị từ chối vì bộ linking đọc "Rank" thành mention NIL.
  Phase 2 dùng cùng bộ linking, nên 5 câu này vẫn sai ở mọi cấu hình trừ oracle.
- 10 câu của hai template "lớn nhất" chỉ hỏi số lượng, nhưng gold có thêm mã giao dịch. Benchmark
  giữ nguyên; nếu báo phép đo phụ "kết quả có số lượng của gold" thì báo cho cả hai phase.

## 6. File và lệnh mới

| File | Nội dung |
|---|---|
| `src/eth_nl2sql/train_templates.py` | **Đã có (S1).** `TRAIN_TEMPLATES`, dùng chung các dataclass và hằng thời gian với `benchmark_templates.py` |
| `src/eth_nl2sql/template_signature.py` | **Đã có (S1).** Chữ ký câu mẫu, kiểm tra R1–R3 và R5. CLI `uv run python -m eth_nl2sql.template_signature [--report PATH]` |
| `src/eth_nl2sql/benchmark_validate.py` | **Đã sửa (S1).** `TIE_BREAKERS` thêm `hour`, `number` và `token_address` cho gold của train. Không gold nào của benchmark kết thúc `ORDER BY` bằng ba cột này, nên benchmark không đổi |
| `src/eth_nl2sql/train_build.py` | **Đã có (S2).** `uv run python -m eth_nl2sql.train_build [--count-scale 0.1] [--out-dir …] [--holdout 20]`, cùng cách chạy với benchmark (quyết định 5 của [benchmark.md](benchmark.md)). Các bước: kiểm tra rò rỉ; giá trị điền trừ thực thể giữ riêng và mọi giá trị điền mang tên chúng, cộng `token_out`; sinh câu bằng `benchmark_build.generate` theo từng variant; bỏ câu trùng dev; linking thật và lọc L1; loại câu còn nêu tên thực thể giữ riêng; dựng prompt. Ghi `data/train/{train.jsonl, dropped.jsonl, sft.jsonl, manifest.json}`. Mỗi dòng của `dropped.jsonl` có kèm cách bộ linking đọc câu (`linked`) và nguyên nhân lỗi đã biết (`cause`: thực thể đọc thành token hoặc ngược lại) |
| `kaggle/phase2-train/` | `train_lora.py`, `kernel-metadata.json` (private, T4), `requirements.lock`, `config.json` (mục 4). Script nhận `--model` và `--max-steps` (dùng khi chạy thử) |
| `kaggle/phase2-fp16-generate/` | `generate_fp16.py` cho mục 5.4: đầu ra cùng định dạng dự đoán `{question_id, decision, sql, attempts}` |
| `sql_generation.py` / `cli.py` | `generate-sql --adapter PATH`: ghi SHA256 adapter và `/lora-adapters` vào `.meta.json`, thêm tên adapter vào tên file dự đoán. Thêm `--no-repair` |
| `execution_evaluation.py` / `cli.py` | `evaluate-sql --train-manifest`: chia đã thấy / chưa thấy (R8) |
| `tests/test_template_signature.py`, `tests/test_train_templates.py` | **Đã có (S1).** Chuẩn hoá SQL, các kiểu vi phạm và gần, mọi câu mẫu train cho bản ghi hợp lệ, bộ linking đọc đúng (R9), không trùng với cách bộ linking đọc câu dev/test |
| `tests/test_train_build.py` | **Đã có (S2).** Rút và lọc thực thể giữ riêng; số câu mỗi variant; `token_out`; mọi lý do loại của L1 (bộ linking giả lập); phân loại lỗi thực thể/token. Hai lần chạy trên DB mẫu có KB làm giàu: prompt train giống hệt prompt `generate-sql`, B8 đọc lại đúng gold, manifest khớp với file; thực thể giữ riêng không bị nêu tên ở đâu |
| README | Mục "Fine-tune LoRA (phase 2)": lệnh sinh, đăng dataset, huấn luyện, chuyển adapter, đánh giá |

Mỗi dòng của `sft.jsonl` có dạng:

```json
{"question_id": "t00001", "template_id": "entity_avg_gas_price", "lang": "vi", "group": "entity",
 "split": "train", "decision": "sql",
 "messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", "content": "```sql\n...\n```"}]}
```

- Notebook huấn luyện học trên `split == "train"` và chỉ theo dõi loss trên `split == "val"`.
- Hai trường `decision` và `messages` giúp `count-prompt-tokens` đọc được thẳng file này.
- Loss chỉ tính trên phần assistant. Khi viết notebook (S5) phải kiểm tra cách thư viện TRL ghim
  phiên bản làm điều này với chat template của Qwen.

Lệnh đánh giá dự kiến (`*.gguf` là adapter đã chuyển):

```bash
~/opt/llama.cpp/src-b12032/build/bin/llama-server \
    -m $PWD/models/qwen2.5-coder-3b-instruct-q4_k_m.gguf --lora $PWD/models/lora/3b-e2.gguf \
    -c 8192 -ngl 99 -fa on -np 1 --cache-ram 0 --port 8080
uv run eth-nl2sql generate-sql --prompts data/prompts/dev.predictions.prompts.jsonl \
    --adapter models/lora/3b-e2.gguf
uv run eth-nl2sql evaluate-sql --gold data/benchmark/dev.jsonl --predictions data/predictions/<run>.jsonl
```

## 7. Các bước

| Bước | Việc | Cần người dùng |
|---|---|---|
| S0 | **Xong (2026-11-04).** Chốt phase 1: cấu hình mặc định là 3B, theo quyết định 1–13. `data/predictions/phase1-freeze.json` ghi SHA256 của KB, model, 18 file code, file prompt/linking và 7 lần chạy dev, kèm EX. Trước khi chạy S4, chạy lại dev với cấu hình mặc định để kiểm tra câu trả lời giống hệt bản đã chốt | |
| S1 | **Xong, đã duyệt (2026-11-05).** Viết `train_templates.py` và `template_signature.py`, kèm test; chạy R1–R3, R5, R9 trên danh mục; chạy thử trên DB thật; rà soát độc lập (mục 2.5). Quyết định 24–27 | |
| S2 | **Xong (2026-11-05).** `train_build` chạy thử với `--count-scale 0.1`, rồi chạy đủ; báo số câu bị loại theo lý do và phân bố độ dài token (mục 2.6) | Xem báo cáo lọc |
| S3 | **Xong (2026-11-05 22:42).** Đóng băng (R7): `kaggle/phase2-train/config.json` (cấu hình quyết định 18) và `data/train/freeze.json` (SHA256 của dữ liệu train, cấu hình, 21 file code, KB, bản chốt phase 1; `test_read_before_freeze: false`) | |
| S4 | **Xong (2026-11-06, 00:24–04:18).** Chạy test phase 1 **một lần** cho 7 cấu hình: 3B mặc định, `--no-placeholder-check`, oracle, `--schema linked`, `--no-evidence`, `--literal`, và 1.5B. Script `scripts/phase1_test.sh` chạy không cần trông, mất 233 phút trên GTX 1650. Script kiểm tra bản đóng băng, chạy lại dev (177/177 câu trả lời giống hệt bản đã chốt), link test, dựng prompt, sinh SQL, chấm điểm và ghi tóm tắt vào `data/predictions/test-phase1.summary.json`. EX test: 3B 0,768, oracle 0,790, 1.5B 0,613. Chi tiết ở [proposed-method.md](proposed-method.md) §8 | Người dùng cho phép `--allow-test` (2026-11-05) |
| S5 | Đăng `sft.jsonl` thành Kaggle dataset private. Chạy thử 1.5B (50 bước: s/bước, VRAM, loss có giảm) rồi chạy đủ. Tải adapter; kiểm tra runtime (5.2); chấm dev cho 2 checkpoint; chọn checkpoint | **Đăng dataset**; tải model HF lớn hơn 1 GB trên Kaggle |
| S6 | Như S5 với 3B | |
| S7 | Ablation lượng tử hoá (5.4) | **Đăng prompt dev** lên Kaggle |
| S8 | Chạy test phase 2 theo mục 5.5, cộng phần chia đã thấy / chưa thấy | Cho phép `--allow-test` |
| S9 | Viết kết quả vào §8, cập nhật README | |

Sau S4, mọi thay đổi kế hoạch phase 2 phải ghi rõ là "sau khi thấy test phase 1".

## 8. Ngân sách

| Việc | Giờ GPU Kaggle | Nguồn |
|---|---:|---|
| Chạy thử 1.5B | 0,5 | ước lượng |
| Huấn luyện 1.5B (khoảng 1.800 câu sau lọc × 2 epoch) | 2–3,5 | ước lượng (mục 4) |
| Chạy thử 3B | 0,5 | ước lượng |
| Huấn luyện 3B | 4,5–6,5 | ước lượng (mục 4) |
| Ablation lượng tử hoá | khoảng 1 | ước lượng |
| Dự phòng: huấn luyện lại 3B một lần (quyết định 23) | khoảng 6 | ước lượng |
| **Tổng** | **khoảng 15–18** | quota 30 giờ/tuần (**đo**) |

Đánh giá chạy local, không tốn quota:
- dev: 790 s cho một lần chạy 3B (**đo**: `wall_seconds` trong `.meta.json` của lần chạy 3B mặc
  định);
- test: khoảng 25 phút (**ước lượng**, theo tỉ lệ 328/177);
- chi phí thêm của `--lora`: chưa đo.

## 9. Tiêu chí dừng (quyết định 23)

- Mỗi model tối đa **2 lần huấn luyện đầy đủ**. Lần thứ hai chỉ được sửa lỗi đường ống hoặc lỗi
  dữ liệu đã chứng minh được, ví dụ nhãn sai, lọc sai hay định dạng đích sai. Không chỉnh siêu
  tham số theo dev.
- Chênh lệch dưới khoảng ±9 câu dev (khoảng 0,05) không được coi là tăng hay giảm. Đây là mức
  dao động do cách viết prompt, đo ở phase 1 ([proposed-method.md](proposed-method.md) §8).
- Dừng sớm một lần chạy nếu loss bị NaN, hoặc loss trên 5% câu giữ lại tăng liên tục sau epoch 1.
  Khi đó vẫn chấm checkpoint epoch 1.
- Sau tối đa 2 lần huấn luyện mỗi model, chạy test (S8) và báo cáo **dù kết quả âm**.

## 10. Rủi ro

| Rủi ro | Ảnh hưởng | Cách xử lý |
|---|---|---|
| Template train khác phân phối test | EX tăng ít, hoặc giảm ở các dạng câu có trong test mà train không có (top-k, `LIMIT 1`, cặp `{E1}`/`{E2}`) | Đây chính là thứ cần đo. Báo EX theo template và nhóm; so phase 1 với phase 2 trên cùng câu |
| fp16 trên T4 (Qwen2.5 vốn huấn luyện bf16) | Loss NaN do tràn số | Giữ trọng số LoRA ở fp32, giảm lr một nửa, hoặc chuyển QLoRA; ghi lại |
| OOM ở 6.144 token | Không chạy được | Chỉ tính logits trên phần trả lời; giảm độ dài chuỗi xuống 5.120 (prompt dài nhất 4.448); chuyển QLoRA |
| Adapter học trên fp16, chạy trên Q4_K_M | EX phase 2 thấp hơn mức model học được | Ablation lượng tử hoá (5.4) đo phần này |
| `--lora` làm đổi runtime hoặc mất prefix cố định | So sánh với phase 1 không còn sạch | Kiểm tra hệ số 0 (5.2) |
| Bộ linking loại nhiều câu train | Thiếu dữ liệu ở một số template | Ngưỡng 20% ở mục 2.3; xem lại câu chữ, không sửa từ điển |
| Dev bị nhìn khi chọn checkpoint | Số dev phase 2 lạc quan | Chỉ 2 lựa chọn, quy tắc chốt trước; số khách quan là test |
| Thấy kết quả test phase 1 trước khi huấn luyện | Người làm nghiên cứu chỉnh phase 2 theo test | Đóng băng ở S3 trước S4; mọi thay đổi sau S4 phải ghi rõ |
| Hết quota hoặc phiên Kaggle bị ngắt | Mất một lần chạy | Lưu adapter mỗi epoch; quota làm mới hằng tuần |
| GPU không tất định | Chạy lại huấn luyện không ra adapter giống hệt | Ghi seed và môi trường; chỉ có đánh giá là tất định (prefix cố định) |
| Ưu tiên mềm `tx_success` cũng áp cho train | Train nghiêng về đích có giao dịch thất bại | Giống benchmark; ghi rõ trong luận văn |
| Câu chữ template vô tình chứa một cụm từ của từ điển ("lời" đọc thành "lỗi", "transfer" đọc thành "transfers", chữ hoa đầu câu bị coi là tên lạ) | Evidence của câu train mang nghĩa sai mà lọc L1 không bắt, vì L1 cho phép nghĩa lệch | Test R9 bắt mọi trường hợp như vậy trên câu mẫu; S2 báo tỉ lệ câu có nghĩa lệch |
| Train gần như không có nhiễu nghĩa, test có (12/86 câu mẫu) | Phase 2 có thể không cải thiện hoặc kém đi trên các câu test bị đọc lệch | Báo EX theo "nghĩa của bộ linking có khớp gold không" (mục 5.5) |
| Template train gần template test (9/30 template train, 612 câu; 13/22 template test có template train gần) | Phần tăng trên các template test này có thể đến từ việc học khung SQL gần giống | Liệt kê đầy đủ (R5); báo EX test theo nhóm gần/xa; ngay benchmark cũng có cặp dev–test gần ở mức tương đương |
| Kỹ năng test cần mà train không có (`LIMIT 1`, top-k theo địa chỉ, `contract_creation`) | EX phase 2 trên các template này có thể giảm | Đây chính là thứ cần đo; báo EX theo template, có ghi chú kỹ năng còn thiếu |
