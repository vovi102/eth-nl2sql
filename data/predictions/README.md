# data/predictions: kết quả sinh SQL

Mỗi thư mục là một thí nghiệm. File chỉ được chuyển chỗ khi sắp xếp lại repo, tên và nội dung giữ
nguyên. Các đường dẫn ghi **bên trong** `*.meta.json`, `phase1-freeze.json` và `data/train/freeze.json`
là đường dẫn lúc chạy, tức bố cục phẳng cũ (tag `thesis-v1`); không có lệnh nào đọc lại các đường dẫn
`data/predictions/...` đó. Đường dẫn tới `data/prompts/` và `data/linking/` trong meta vẫn đúng.

## Tên file

Mỗi bước nối thêm một đoạn vào tên của file đầu vào:

```
dev                       data/benchmark/dev.jsonl (gold)
dev.predictions           data/linking/: bản ghi của bộ linking
dev.predictions.prompts   data/prompts/: prompt dựng từ linking dự đoán
dev.prompts               data/prompts/: prompt dựng từ gold (linking oracle)
  [.no-evidence|.literal|.linked-schema]     ablation của build-prompts
  .<model>[.<adapter>[.scale0]]              generate-sql (adapter: LoRA phase 2)
  [.no-check][.no-repair]                    ablation của generate-sql (tên do script đặt)
```

Mỗi run `X.jsonl` có `X.jsonl.meta.json` (prompts, linking, server, SHA256), `X.ex.json` (báo cáo EX),
`X.ex-details.jsonl` (từng câu) và `X.ex.log`. `server-*.log` là log của `llama-server`.
`<model>` là `qwen2.5-coder-{3b,1.5b}-instruct-q4_k_m` (viết tắt 3B, 1.5B bên dưới).

## Thư mục

| Thư mục | Thí nghiệm | Sinh bởi | Số liệu trong docs |
|---|---|---|---|
| `phase1/dev/` | Phase 1 trên dev, bản đóng băng (`phase1-freeze.json`): 3B mặc định, no-check, oracle, linked-schema, no-evidence, literal; 1.5B mặc định. `dev.prompt-tokens.json`: độ dài prompt | chạy tay | `docs/proposed-method.md` §8 |
| `phase1/dev-history/` | Các lần chạy dev trước bản đóng băng (bảng các bước ở §8) | chạy tay | `docs/proposed-method.md` §8 |
| `phase1/test/` | S4: phase 1 trên test, 7 cấu hình; `recheck/`: kiểm tra lại dev trước khi đọc test | `scripts/phase1/test.sh` | `docs/proposed-method.md` §8, README |
| `phase2/precheck/` | Kiểm tra runtime với một LoRA ngẫu nhiên (scale 0 và 1) | `scripts/phase2/precheck.sh` | `docs/phase2-plan.md` |
| `phase2/dev/` | S5/S6: adapter e1, e2 và kiểm tra scale 0 cho 1.5B và 3B; `dev-phase2-<model>.summary.json` ghi checkpoint được chọn | `scripts/phase2/dev.sh` | `docs/proposed-method.md`, `docs/phase2-plan.md` |
| `phase2/quant/` | S7: fp16 so với Q4_K_M, có và không có LoRA (no-check, no-repair) | `scripts/phase2/quant.sh` | `docs/proposed-method.md`, README |
| `phase2/test/` | S8: phase 2 trên test (3B+LoRA mặc định, no-check, oracle; 1.5B+LoRA); `recheck/`: kiểm tra lại dev | `scripts/phase2/test.sh` | README (bảng kết quả), `docs/proposed-method.md` |

### Kết quả chính

EX = `overall.ex` trong `X.ex.json`.

| Run | dev | test |
|---|---:|---:|
| 3B, phase 1 | 0,898 | 0,768 |
| 3B, oracle linking | 0,881 | 0,790 |
| 3B, no-check | 0,768 | 0,665 |
| 3B, linked-schema | 0,904 | 0,802 |
| 3B, no-evidence | 0,090 | 0,015 |
| 3B, literal (chỉ câu có địa chỉ) | 0,818 | 0,690 |
| 1.5B, phase 1 | 0,599 | 0,613 |
| 3B + LoRA e2 | 0,977 | 0,814 |
| 3B + LoRA e2, oracle | | 0,823 |
| 1.5B + LoRA e2 | 0,960 | 0,808 |

### `phase1/dev-history/`

Theo thứ tự thời gian, 3B trên dev:

| Thư mục | Thay đổi | EX |
|---|---|---:|
| `prompt-v1/` | Lần chạy đầu, chưa có fixed prefix: no-check 0,729, placeholder-check 0,825. `trial/`: chạy thử `--limit 10` | 0,825 |
| `rejected-direction-rule/` | Thêm luật from/to vào system prompt (`RULES.txt`): v2-long 0,718, v3-short 0,780. Đã loại | |
| `before-hints/` | Có fixed prefix, chưa có `evidence_hint` (no-check 0,746) | 0,825 |
| `hint-v1/` | `evidence_hint` lần đầu | 0,847 |
| `before-tie-fix/` | 7 cấu hình trên benchmark cũ (trước luật không hoà ở top-k, docs/benchmark.md §6) | 0,887 |
