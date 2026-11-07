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

## Linking và prompt

Bộ linking B1–B5 không dùng LLM; B6/B7 dựng prompt từ bản ghi linking (của bộ linking, hoặc gold
khi chạy linking oracle). Định dạng bản ghi: [docs/linking-labels.md](docs/linking-labels.md);
định dạng prompt: [docs/proposed-method.md](docs/proposed-method.md) §5.

```bash
uv run eth-nl2sql link-question "Tornado Cash nhận bao nhiêu ETH ngày 5/9?" --expand --prompt
uv run eth-nl2sql evaluate-linking --gold data/benchmark/dev.jsonl --report data/linking/dev.report.json
uv run eth-nl2sql build-prompts --input data/linking/dev.predictions.jsonl   # data/prompts/
uv run eth-nl2sql build-prompts --input data/benchmark/dev.jsonl             # linking oracle
# ablation: --no-evidence, --literal (địa chỉ thay cho {E1}), --schema linked (B6)
uv run eth-nl2sql evaluate-sql --gold data/benchmark/dev.jsonl --self-check      # phải ra EX 1.0
uv run eth-nl2sql evaluate-sql --gold data/benchmark/dev.jsonl --predictions <run>.jsonl \
    [--linking data/benchmark/dev.jsonl]   # mặc định: data/linking/dev.predictions.jsonl
```

`data/benchmark/test.jsonl` chỉ đọc được khi có `--allow-test` (đánh giá cuối).

## Sinh SQL bằng model local (phase 1)

Runtime là `llama-server` của llama.cpp (API tương thích OpenAI), model là GGUF Q4_K_M chính
thức của Qwen, để trong `models/`:

| File | Repo Hugging Face, revision | SHA256 |
|---|---|---|
| `qwen2.5-coder-3b-instruct-q4_k_m.gguf` (2,10 GB) | `Qwen/Qwen2.5-Coder-3B-Instruct-GGUF` @ `f74adce6aa16316c625447af059dbebe4983757c` | `724fb256bec1ff062b2f65e4569e871ad2e95ab2a3989723d1769c54294730b7` |
| `qwen2.5-coder-1.5b-instruct-q4_k_m.gguf` (1,12 GB) | `Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF` @ `f86cb2c1fa58255f8052cc32aeede1b7482d4361` | `cc324af070c2ecbfd324a30884d2f951a7ff756aba85cb811a6ec436933bb046` |

Bản build sẵn của llama.cpp cần glibc 2.38 (Ubuntu 24.04), máy WSL2 là Ubuntu 22.04, nên
llama.cpp được biên dịch, không cần sudo. CUDA 12.8 lấy từ conda-forge qua micromamba;
`CMAKE_CUDA_ARCHITECTURES=75` là GTX 1650. Biên dịch mất 16 phút với `-j 6`.

```bash
# CUDA 12.8 + cmake + gcc 13 vào ~/opt/micromamba/envs/cuda128
~/opt/micromamba/bin/micromamba create -p ~/opt/micromamba/envs/cuda128 -c conda-forge \
    --override-channels cuda-version=12.8 cuda-nvcc cuda-cudart-dev libcublas-dev \
    "cmake>=3.28" ninja gxx_linux-64=13
git clone --depth 1 --branch b12032 https://github.com/ggml-org/llama.cpp ~/opt/llama.cpp/src-b12032
cd ~/opt/llama.cpp/src-b12032 && ~/opt/micromamba/bin/micromamba run -p ~/opt/micromamba/envs/cuda128 \
    bash -c 'cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES=75 -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF && cmake --build build -j 6 --target llama-server llama-tokenize'
```

Chạy server (một slot, mọi layer trên GPU, context 8192), rồi sinh SQL và chấm:

```bash
~/opt/llama.cpp/src-b12032/build/bin/llama-server \
    -m $PWD/models/qwen2.5-coder-3b-instruct-q4_k_m.gguf -c 8192 -ngl 99 -fa on -np 1 \
    --cache-ram 0 --port 8080
uv run eth-nl2sql count-prompt-tokens data/prompts/dev.predictions.prompts*.jsonl
uv run eth-nl2sql generate-sql --prompts data/prompts/dev.predictions.prompts.jsonl \
    [--limit 10]   # -> data/predictions/<prompts>.<model>.jsonl (+ .meta.json)
uv run eth-nl2sql evaluate-sql --gold data/benchmark/dev.jsonl \
    --predictions data/predictions/<run>.jsonl [--only-predicted]
```

- `generate-sql` không gọi model với câu có `decision` khác `sql`. Câu có prompt dài hơn
  `n_ctx` − `--max-tokens` được đánh dấu `skipped` và không gửi đi (quyết định 7).
- Bản ghi linking lấy theo trường `input` trong `.meta.json` của file prompt.
- `--cache-ram 0` tắt cache prompt trên RAM của llama-server (mặc định tới 8 GB). Không tắt
  thì máy 7 GB bị OOM sau khoảng 300 câu. Prefix cố định chỉ cần KV cache của slot; đã so 20
  câu, kết quả không đổi.
- Câu SQL chạy được nhưng bỏ sót placeholder mà evidence đưa ra (model tự viết một địa chỉ
  thay cho `{E1}`) cũng được hỏi lại một lần. `--no-placeholder-check` tắt việc này, chỉ dùng
  cho ablation.
- Prefix cố định: trước mỗi câu, cache của server được đưa về đúng các token mà mọi prompt
  trong file đều có chung (system prompt + schema; `/completion` với `n_predict: 0`). Nhờ vậy
  kết quả greedy không phụ thuộc câu chạy trước. `--no-fixed-prefix` tắt việc này.
- Mỗi dòng có `{question_id, decision, sql, attempts: [{reply, sql, error_kind, error,
  seconds, prompt_tokens, cached_tokens, completion_tokens}]}`; `sql` là SQL cuối, còn
  placeholder.
- `.meta.json` ghi SHA256 của GGUF và của file prompt/linking, `build_info` của server,
  `n_ctx`, chat template, tham số sinh và thống kê lỗi/thời gian.
- `--only-predicted` của `evaluate-sql` chỉ chấm các câu có dự đoán và không bị `skipped`,
  dùng cho lần chạy thử và ablation literal. Không có cờ này thì câu thiếu dự đoán bị tính sai.
- Client (`OpenAICompatibleClient` trong `src/eth_nl2sql/sql_generation.py`) chỉ cần API chat
  của OpenAI, nên phase 2 dùng lại được với server khác (vLLM). Riêng `count-prompt-tokens`
  và prefix cố định cần `/apply-template`, `/tokenize` và `/completion` của llama-server.
- `data/predictions/prompt-v1/` là các lần chạy đầu tiên (2026-11-02), trước khi có prefix cố
  định; `data/predictions/rejected-direction-rule/` là hai bản system prompt có thêm quy tắc
  from/to đã bị bỏ (`RULES.txt`, [docs/proposed-method.md](docs/proposed-method.md) §8).
  `before-hints/` và `hint-v1/` là lần chạy trước khi có `evidence_hint` và với hint lần 1 của
  `contract_called` (cột `evidence_hint` trong `data/schema/property_senses.csv`).

## Fine-tune LoRA (phase 2, đang làm)

Kế hoạch và các quyết định: [docs/phase2-plan.md](docs/phase2-plan.md). Phase 1 được chốt
trong `data/predictions/phase1-freeze.json`: SHA256 của KB, model, code và 7 lần chạy dev, kèm
EX của từng lần.

Template chỉ dùng cho train nằm trong `src/eth_nl2sql/train_templates.py` (30 template, 2.000
câu). Kiểm tra rò rỉ so với template dev/test chỉ đọc code template và `manifest.json`, không
đọc file câu hỏi:

```bash
uv run python -m eth_nl2sql.template_signature [--report data/train/signature.json]
```

Với mỗi template train, lệnh in ra câu dev/test giống nhất về câu chữ, và các template dev/test
"gần". Các kiểu gần:

- đổi chiều to/from;
- đổi trạng thái thành công/thất bại;
- ngày ↔ giờ;
- đổi loại giao dịch;
- đổi loại mention;
- "theo ngày" của một câu tổng.

Lệnh cũng in số template dev/test có template train gần, cùng số câu hỏi tương ứng. Lệnh thoát
với mã 1 khi có vi phạm:

- trùng id template;
- trùng chữ ký (tập sense, loại mention, khung SQL ở dạng chuẩn);
- cùng evidence nhưng khác câu truy vấn, hoặc cùng câu truy vấn nhưng khác tên gọi;
- câu chữ trùng hẳn một câu dev/test, hoặc giống một câu test từ 0,8 trở lên.

Định nghĩa đầy đủ ở [docs/phase2-plan.md](docs/phase2-plan.md) §2.1 và §3.

Sinh dữ liệu train (S2) bằng lệnh sau:

```bash
uv run python -m eth_nl2sql.train_build --count-scale 0.1 --out-dir /tmp/train-dry  # chạy thử
uv run python -m eth_nl2sql.train_build      # data/train/{train,dropped,sft}.jsonl, manifest.json
uv run eth-nl2sql count-prompt-tokens data/train/sft.jsonl   # cần llama-server đang chạy
```

Các bước của lệnh:

1. Kiểm tra rò rỉ của danh mục; có vi phạm thì dừng.
2. Sinh câu từ template bằng bộ sinh của benchmark (seed 20261104). Bỏ ra 20 thực thể giữ riêng
   (rút theo seed) và thêm bộ lọc `token_out`.
3. Bỏ câu trùng câu dev.
4. Link bằng bộ linking thật, rồi lọc L1: bộ linking phải ra `sql`, đúng placeholder của gold,
   gold SQL với địa chỉ của bộ linking phải ra cùng kết quả, và evidence không còn `{E}` chưa gắn
   placeholder.
5. Dựng prompt đúng như `generate-sql` và lấy gold SQL làm câu trả lời.

Đóng băng (S3) nằm ở `data/train/freeze.json` và `kaggle/phase2-train/config.json`. Chạy test
phase 1 một lần (S4), không cần trông, khoảng 4 giờ:

```bash
bash scripts/phase1_test.sh --check   # chỉ kiểm tra điều kiện (không GPU, không đọc test)
bash scripts/phase1_test.sh           # log: data/predictions/test-phase1.log
```

S4 đã chạy ngày 2026-11-06. Tóm tắt nằm ở `data/predictions/test-phase1.summary.json`; kết quả
và phân tích ở [docs/proposed-method.md](docs/proposed-method.md) §8.

Mỗi template để riêng 5% số câu (`split: val`) để theo dõi loss. Vì `sft.jsonl` có sẵn trường
`decision` và `messages`, `count-prompt-tokens` đếm được độ dài cả chuỗi train.

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
