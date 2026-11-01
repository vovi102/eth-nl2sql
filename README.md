# eth-nl2sql

NL-to-SQL trên dữ liệu Ethereum.

## Dữ liệu tháng 2026-09 (đầy đủ)

`data/raw/2026-09/` là **toàn bộ** tháng 9/2026 (UTC) của `bigquery-public-data.crypto_ethereum`,
dạng Parquet (zstd). `transactions` và `token_transfers` mỗi ngày một file
(`transactions/transactions_2026-09-05.parquet`), các bảng còn lại một file cho cả tháng
(`blocks/blocks_2026-09.parquet`):

| Bảng | Số dòng | Nguồn |
|---|---:|---|
| `blocks` | 215.033 | `blocks` |
| `transactions` | 56.880.856 | `transactions` (bỏ `input`, `block_hash`; có receipt/gas đầy đủ) |
| `token_transfers` | 103.638.134 | `token_transfers` |
| `contracts` | 309.264 | `contracts` chạm tới trong tháng; một dòng mỗi lần deploy |
| `tokens` | 95.473 | `tokens_latest` của các token có transfer trong tháng |
| `amended_tokens` | 95.473 | `amended_tokens`, để đối chiếu symbol/decimals |

```bash
uv run eth-nl2sql extract --month 2026-09 --dry-run   # chỉ ước lượng GB
uv run eth-nl2sql extract --month 2026-09             # trích xuất (chạy lại để resume)
uv run eth-nl2sql verify-extract --out data/raw/2026-09
```

`manifest.json` ghi câu SQL, job id và số byte bị tính phí của từng file; `parity.json` là
aggregate theo ngày tính trên BigQuery, `verify-extract` tính lại bằng DuckDB và phải khớp.
Project GCP là sandbox (giới hạn lưu trữ 10 GB) nên không dùng bảng staging: mỗi file đọc thẳng
bảng kết quả tạm của job qua Storage Read API. Lần trích xuất 2026-10-31 bị tính 106 GB.

Bảng BigQuery không dùng và các loại câu hỏi không trả lời được: [docs/data-scope.md](docs/data-scope.md).

## Cơ sở tri thức cho entity linking

Phương pháp và các quyết định: [docs/linking-method.md](docs/linking-method.md). Các file CSV
đã duyệt nằm trong `data/entity_knowledge_base/`; nhãn địa chỉ lấy từ eth-labels (đã ghim commit,
có SHA256 trong `data/raw/labels/eth-labels/source.json`).

```bash
uv run eth-nl2sql fetch-labels      # tải bản eth-labels đã ghim
uv run eth-nl2sql draft-entities    # sinh/cập nhật ứng viên, không ghi đè phần đã duyệt
uv run eth-nl2sql check-entities    # kiểm tra tham chiếu, liệt kê điểm cần duyệt
```

## Dữ liệu cũ (đã lưu trữ)

Tập mẫu CSV `data/raw/full/` (06/2026, giao dịch địa chỉ có nhãn + ~1% còn lại) và database
build từ nó đã chuyển sang `../archive/luanvan-full-2026-06/` ngày 2026-10-31. Không dùng nữa.

## Database DuckDB

```bash
uv sync
uv run eth-nl2sql build-db              # data/raw/2026-09 -> data/db/ethereum.duckdb
uv run eth-nl2sql verify-db             # kiểm tra lại database có sẵn với Parquet gốc
uv run eth-nl2sql load-knowledge-base   # chỉ nạp lại schema link sau một đợt duyệt KB
uv run eth-nl2sql check-property-senses # kiểm tra data/schema/property_*.csv
uv run eth-nl2sql fetch-bigquery-schema # tải lại mô tả cột của BigQuery (data/schema/)
uv run pytest
```

Database có ba schema:

| Schema | Nội dung | Ai dùng |
|---|---|---|
| `main` | `blocks`, `transactions`, `token_transfers`, `contracts`, `tokens`, `amended_tokens` của tháng 9: tên bảng, cột và giá trị như `bigquery-public-data.crypto_ethereum` | Gold SQL và model NL2SQL; gold SQL chỉ chạm schema này |
| `link` | `entity_classes`, `entities`, `entity_addresses`, `entity_aliases` (chỉ dòng đã duyệt của `data/entity_knowledge_base/`), `token_stats` (số ví gọi, số transfer của mỗi token), `property_senses`, `property_phrases` (từ điển nghĩa property đã duyệt trong `data/schema/`) | Entity linking, property linking |
| `meta` | `tables`, `columns` (mô tả cột chính thức của BigQuery, 2 giá trị ví dụ mỗi cột), `build` | Dựng prompt, schema linking |

Mô tả cột cũng được gắn vào `main` bằng `COMMENT ON`. Khác biệt duy nhất so với BigQuery:

- Mọi thời gian là `TIMESTAMP` theo **UTC** (không kèm múi giờ), tức giờ BigQuery hiển thị.
- `transactions.value` là `DECIMAL(38,0)` (BigQuery `NUMERIC`; wei, tối đa 24 chữ số).
- `token_transfers.value` giữ nguyên `VARCHAR` như BigQuery, vì có thể tới 78 chữ số (uint256);
  SQL tự `CAST` khi cần tính. Đơn vị là đơn vị nhỏ nhất của token (`tokens.decimals`).
- Các cột do bản trích xuất suy ra: `transactions.blob_count`, `tokens.token_standards` và
  `tokens.protocols` (mảng nối bằng `|`).

Build chỉ thay `ethereum.duckdb` khi mọi kiểm tra đều đạt; kết quả ghi vào
`data/db/ethereum.report.json`:

- SHA256 của mọi file Parquet khớp `SHA256SUMS`.
- **Khớp BigQuery:** mọi aggregate trong `parity.json` (số dòng, số khoá phân biệt, các tổng theo
  ngày) tính lại trên bảng đã nạp bằng chính câu SQL BigQuery đã chạy, phải bằng giá trị BigQuery.
- **Không mất mát:** fingerprint (số dòng + tổng hash từng dòng) của mỗi bảng bằng của file
  Parquet; số được so dưới dạng text đã trích xuất, nên không giá trị nào bị đổi, mất hay nhân đôi.
- Không có dòng treo: `transactions.block_number → blocks.number`,
  `token_transfers.block_number → blocks.number`,
  `token_transfers.transaction_hash → transactions.hash`.
- Mọi cột của `main` có mô tả trong `data/schema/bigquery_crypto_ethereum.json`.
