# Phạm vi dữ liệu và giới hạn câu hỏi

Nguồn: `bigquery-public-data.crypto_ethereum` (11 view). Dữ liệu dùng trong project là
**toàn bộ tháng 9/2026 (UTC)**, trích xuất ngày 2026-10-31 vào `data/raw/2026-09/`.

## Bảng được dùng (6/11)

| Bảng trong project | Nguồn BigQuery | Phạm vi |
|---|---|---|
| `blocks` | `blocks` | mọi block trong tháng |
| `transactions` | `transactions` | mọi giao dịch trong tháng; bỏ `input`, `block_hash` |
| `token_transfers` | `token_transfers` | mọi event Transfer (ERC-20/721) đã giải mã trong tháng |
| `contracts` | `contracts` | chỉ contract được gọi, được tạo, hoặc phát token transfer trong tháng |
| `tokens` | `tokens_latest` | token có transfer trong tháng; metadata ở thời điểm trích xuất |
| `amended_tokens` | `amended_tokens` | symbol/decimals đã hiệu chỉnh, để đối chiếu |

## Bảng không dùng (5/11) và lý do

| Bảng | Nội dung | Lý do loại |
|---|---|---|
| `tokens` | bản thô của `tokens_latest` | trùng lặp; `tokens_latest` đã là bản mới nhất cho mỗi token |
| `logs` | toàn bộ event log thô (topics, data dạng hex) | rất lớn so với `token_transfers`; ngoài Transfer, các event cần giải mã ABI theo từng contract nên không hỏi được bằng ngôn ngữ tự nhiên trên dữ liệu hex |
| `traces` | internal call giữa các contract | thường là bảng lớn nhất dataset; vượt ngân sách quét (150 GB) và tài nguyên máy (RAM, GPU GTX 1650) |
| `balances` | số dư ETH **hiện tại** của mọi địa chỉ | không có mốc thời gian, không khớp cửa sổ tháng 9 → đáp án sai |
| `token_supply_latest` | tổng cung token **hiện tại** | không theo thời gian; `tokens.total_supply` đã có thông tin tương tự |

Kích thước `logs`/`traces` ở trên là đánh giá định tính, chưa đo cho tháng 9.

## Loại câu hỏi không trả lời được (hoặc trả lời sai)

| Loại câu hỏi | Ví dụ | Nguyên nhân |
|---|---|---|
| Số dư tài khoản | "Số dư ETH của ví X cuối tháng 9?" | không có `balances`; không thể cộng dồn từ một tháng giao dịch (thiếu số dư đầu kỳ, thiếu ETH chuyển qua internal call) |
| ETH chuyển qua internal call | "Contract X đã trả ra bao nhiêu ETH?" | không có `traces`; `transactions.value` chỉ có ETH của lời gọi ngoài cùng |
| Event khác Transfer | "Bao nhiêu lượt swap trên Uniswap?", "Ví X đã approve token nào?" | không có `logs` |
| Hàm được gọi / dữ liệu lời gọi | "Giao dịch nào gọi hàm `transfer`?" | bỏ cột `transactions.input` |
| Ngoài tháng 9/2026 | "So sánh phí gas tháng 8 và tháng 9" | chỉ trích xuất một tháng |
| Thực thể theo tên (ví, sàn) | "Binance đã nhận bao nhiêu ETH?" | chưa có bảng nhãn địa chỉ; `tokens` chỉ có tên token, không có tên ví/sàn |
| Lịch sử contract ngoài tháng | "Có bao nhiêu contract được deploy năm 2025?" | `contracts` chỉ giữ contract có hoạt động trong tháng 9 |
| Metadata token theo thời gian | "Tổng cung USDT ngày 1/9?" | `tokens` là ảnh chụp tại ngày trích xuất (2026-10-31) |
