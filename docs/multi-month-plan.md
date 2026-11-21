# Kế hoạch: chạy trên dữ liệu nhiều tháng

Lập ngày 2026-11-16. **Chưa có bước nào được thực hiện**, ngoại trừ việc trích xuất tháng 10 (M0).

Yêu cầu: hệ thống của luận văn phải chạy được trên một CSDL chứa nhiều tháng. Người dùng đã chọn
các điểm sau cùng ngày:

- Một CSDL DuckDB chứa tháng 9 và tháng 10/2026, và thêm được tháng sau.
- Câu hỏi được hỏi về bất kỳ tháng, ngày hay khoảng nào trong dữ liệu, kể cả khoảng vắt qua hai
  tháng ("từ 25/9 đến 5/10").
- Có một bộ câu hỏi mới. Không thêm mẫu so sánh giữa các tháng.
- Adapter LoRA của phase 2 (`3b-e2`, `1.5b-e2`) giữ nguyên, chỉ đánh giá, không huấn luyện lại.
- Kết quả tháng 9 hiện có (phase 1, phase 2, tag `thesis-v1`) giữ nguyên.
- Hạn nộp còn hơn 2 tuần.

Mỗi con số đều ghi nguồn: **đo** (đo trong repo), **ước lượng** (chưa đo).

## 1. Nguyên tắc

1. **Tháng 9 tái lập byte-for-byte.** Code nhận khoảng dữ liệu làm tham số, mặc định là tháng 9.
   Các file sau phải giữ nguyên từng byte:
   - `data/db/ethereum.duckdb` và báo cáo `ethereum.report.json` (trừ dấu thời gian);
   - `data/linking`, `data/prompts`, `data/benchmark` (trừ trường `knowledge_base_version` đã
     lệch từ trước), `data/train`.

   Cách kiểm tra là các lệnh `--check` hiện có, cộng với việc dựng lại dev prompts.
2. **Khoảng dữ liệu thuộc về CSDL.** Tháng đầu và tháng cuối đọc từ `meta.build`, không gắn cứng
   trong code. Prompt, bộ liên kết và bộ sinh câu hỏi đều đọc từ đó.
3. **Bộ câu hỏi mới là tập kiểm tra.** Prompt, quy tắc liên kết và adapter được chốt trước khi
   chạy. Mỗi cấu hình chạy đúng một lần, như S4 và S8. Không chỉnh gì sau khi đã xem kết quả.

## 2. Những chỗ đang gắn cứng tháng 9 (đo, 2026-11-16)

| Chỗ | Hiện tại | Sửa thành |
|---|---|---|
| `ingest/duckdb_build.py` | `build-db` nạp đúng một thư mục tháng | nạp nhiều thư mục tháng liền nhau (§3, M1) |
| `ingest/bigquery_schema.py:28-36` | mô tả bảng ghi "September 2026" | sinh từ khoảng dữ liệu |
| `paths.py:15` | `RAW_MONTH_DIR = raw/2026-09` | giữ làm mặc định |
| `sql/prompt.py:28-29`, `:226`, `:268` | system prompt, dòng "no time range" và dòng gợi ý của lớp ("by calls in September") ghi cứng tháng 9 | sinh từ khoảng dữ liệu; tháng 9 cho ra đúng chuỗi cũ |
| `linking/resources.py:128` | số lời gọi (prior, top-N của lớp) đếm trên cả `main.transactions` | giữ nguyên: trên CSDL 2 tháng sẽ tự đếm cả 2 tháng (D3) |
| `benchmark/templates.py:17-90` | `MONTH_START/END`, chữ "tháng 9"/"September" | nhận tháng neo làm tham số |
| `benchmark/build.py:307-312` | ngày 15/9 cứng (chọn địa chỉ gõ tay) | ngày 15 của tháng đầu (tháng 9 vẫn là 15/9) |
| `benchmark/validate.py:105`, `:191` | khoảng thời gian phải nằm trong tháng 9 | phải nằm trong khoảng dữ liệu |
| `linking/time_expr.py` | đã nhận mọi tháng; câu không có thời gian nghĩa là "cả tháng" | đổi nghĩa thành "toàn bộ dữ liệu"; cần kiểm tra khoảng vắt tháng tiếng Việt |

Phần không gắn với tháng: KB (địa chỉ và bí danh lấy từ nguồn nhãn), các nghĩa thuộc tính, bộ
chấm EX, runtime llama.cpp và adapter.

## 3. Các bước

| Bước | Nội dung | Kiểm tra |
|---|---|---|
| M0 | **Xong 2026-11-17.** Trích xuất 2026-10 về `data/raw/2026-10/` (11 GB, 121,9 GB billed): 222.451 blocks, 55.365.081 transactions, 130.611.631 token_transfers, 349.803 contracts, 106.146 tokens, 106.146 amended_tokens. Sửa `AGENTS.md`; README để tới M7. | parity theo ngày khớp BigQuery (đo, `verify-extract` chạy cuối lệnh `extract`) |
| M1 | **Xong 2026-11-17.** `build-db` nhiều tháng → `data/db/ethereum_2026-09_2026-10.duckdb` (22,4 GB, build 25 phút 53 giây, RSS tối đa 3,4 GB): 437.484 blocks, 112.245.937 transactions, 234.249.765 token_transfers, 560.020 contracts, 156.178 tokens, 156.178 amended_tokens. `verify-db` của CSDL tháng 9 chạy bằng code mới vẫn qua (2 phút 54 giây). | mọi kiểm tra dưới đây qua; 0 khóa ngoại treo; số dòng = tổng hai tháng trừ 99.047 contracts và 45.441 tokens trùng (đo) |
| M2 | **Xong 2026-11-17.** Prompt và bộ liên kết đọc khoảng dữ liệu từ `meta.build`; quy tắc xếp hạng lớp `--class-rank question` (mặc định) / `data` (quy tắc tháng 9, ghim trong `scripts/phase1`, `scripts/phase2` và trong `training/build.py`); cảnh báo `time_outside_data:all|part` (D5). Parser thời gian vốn đã nhận khoảng vắt tháng VI/EN, không phải sửa. | Đo: liên kết và 5 biến thể prompt của dev và test giống từng byte với `--class-rank data`; `build-train` ra `train.jsonl`, `sft.jsonl`, `dropped.jsonl` giống từng byte (manifest chỉ khác ngày tạo và `catalog_sha256`); quy tắc mới đổi 17/177 prompt dev, 25/328 prompt test; 294 test qua |
| M3 | **Xong 2026-11-19.** Bộ sinh câu hỏi theo tháng neo (`time_fill(..., month=)`, `range_fill` vắt tháng); SQL chuẩn xếp hạng lớp theo `--class-rank` (mặc định `question`); kiểm tra thời gian theo khoảng dữ liệu; `--split-seed` giữ cách chia dev/test theo mẫu của tháng 9; `--id-prefix`. Bộ mới `data/benchmark-multi/` (seed 20261118, mã `m00001`…): 177 dev + 328 test, đủ số câu ở mọi mẫu, 32 phút. Câu có SQL theo tầng thời gian: tháng 9: 176, tháng 10: 185, vắt tháng: 29, cả 2 tháng: 67. | Đo: dựng lại tháng 9 với `--class-rank data` ra dev/test giống hệt bản đã commit (trừ `knowledge_base_version`); mọi câu thời gian của bộ sinh (tháng 9, tháng 10, vắt tháng, VI/EN) được bộ liên kết đọc lại đúng khoảng; 362 test qua |
| M4 | **Xong 2026-11-19.** (1) Hai cài đặt xếp hạng lớp (SQL chuẩn của bộ đánh giá, bộ liên kết) cho cùng địa chỉ ở 505/505 câu (`data/linking-multi/check_gold_addresses.py`). (2) SQL chuẩn tự chấm trên CSDL 2 tháng: dev 177/177, test 328/328 (`data/predictions/multi/selfcheck`). (3) Liên kết trên bộ mới (`data/linking-multi`): thời gian đúng 1,000 (dev, test), lớp 1,000; F1 cụm đề cập dev 0,970, test 0,972 (tháng 9: 1,000; 0,982). Lỗi đều đã có từ trước: tên dài hơn 5 từ (4 câu, giới hạn `MAX_NGRAM`), tên vừa là token vừa là giao thức (5 câu, nhóm mơ hồ), chữ "Rank" đầu câu (2 câu). Liên kết lại 505 câu trên CSDL tháng 9 chọn cùng thực thể ở 504/505 câu (khác ở m00050, câu sai ở cả hai): dữ liệu tháng 10 không gây lỗi liên kết. | đo |
| M5 | **Xong 2026-11-20** (`scripts/multi/test.sh`, 162 phút, không bước nào lỗi). 4 cấu hình trên bộ mới, thiết lập mặc định của S4/S8. EX dev / test: 3B 161/177 / 253/328 (0,771); 3B+`3b-e2` 171/177 / 265/328 (0,808); 1.5B 115/177 / 217/328 (0,662); 1.5B+`1.5b-e2` 169/177 / 262/328 (0,799). Test tháng 9 (S4/S8): 0,768; 0,814; 0,613; 0,808. Test theo tầng (câu có SQL chuẩn; tháng 9: 114, tháng 10: 123, vắt tháng: 23, cả 2 tháng: 36): 3B 88/94/17/22; 3B+LoRA 95/100/18/20; 1.5B 78/78/12/17; 1.5B+LoRA 93/100/17/20; câu phải từ chối 32/32 ở cả 4 cấu hình. McNemar test, LoRA so với gốc: 3B 26/14 (p 0,081), 1.5B 48/3 (p < 0,001). Kết quả: `data/predictions/multi/` (`multi.summary.json`). | Đo: 3B+`3b-e2` trên prompt dev tháng 9 (CSDL cũ, `--class-rank data`) ra 177/177 câu trả lời giống S6 |
| M6 | **Xong 2026-11-22** (`data/predictions/multi/analyze.py` → `multi.analysis.json`, chỉ đọc kết quả M5 và test tháng 9 của S4/S8). (1) **Khoảng thời gian không gây lỗi.** Mọi SQL mô hình viết cho câu có thời gian đều chứa đúng hai mốc của dòng evidence; câu không có mốc nào là câu bị từ chối do lỗi liên kết của M4, lỗi parse, hoặc (1 câu, 1.5B+LoRA, m00397) SQL sai cả khung vì chép dòng evidence `{E}` chưa gắn placeholder. Câu không nêu thời gian: 0 SQL tự thêm mốc ngày, ở cả 4 cấu hình. (2) **Theo mẫu, EX trên bộ 2 tháng gần bằng test tháng 9** (cùng mẫu, câu khác): 3B+LoRA khác nhiều nhất 1 câu ở mỗi mẫu (`ambiguous_times` 14 → 13, `address_eth_received` 13 → 12); 4 mẫu thiếu khung SQL vẫn 0 câu với LoRA. (3) **Khác biệt giữa các tầng là do tỉ lệ mẫu, không do tháng.** Mẫu gần, 3B+LoRA: tháng 9 63/68, tháng 10 79/82, vắt tháng 11/11, cả 2 tháng 17/19. Tầng "cả 2 tháng" có 17/36 câu thuộc mẫu xa, trong đó 14 câu thuộc 4 mẫu LoRA luôn sai (`address_largest_eth_sent` 6, `token_top_receivers` 4, `token_largest_transfer` 3, `class_top_contracts` 1); 5 câu LoRA làm hỏng ở tầng này (3B: 3 tốt lên, 5 xấu đi) đều thuộc 3 mẫu đó. Câu vắt tháng mà 3B+LoRA sai: 5 câu test, đều thuộc 3 trong 4 mẫu luôn sai đó (`token_top_receivers` 2, `token_largest_transfer` 2, `class_top_contracts` 1), cộng m00050 (dev, bị từ chối do lỗi liên kết). (4) **LoRA so với gốc, test, giống S8:** 3B mẫu gần 155 → 170/180, mẫu xa 66 → 63/116 (S8: 157 → 172; 63 → 63); 1.5B mẫu gần 127 → 167, mẫu xa 58 → 63. 3B: 26 tốt lên / 14 xấu đi (S8: 27 / 12); p 0,081 so với 0,024 vì có thêm câu xấu đi ở mẫu xa. Lỗi của LoRA giống S8: `unknown_placeholder` (`token_top_receivers`), `execution` (`address_largest_eth_sent` dùng `transaction_hash`). (5) Câu hỏi về lớp, xếp hạng theo khoảng của câu (quy tắc mới, D3): `class_call_count`, `class_distinct_callers` 14/14 với 3B, 3B+LoRA, 1.5B+LoRA, như tháng 9. | — |
| M7 | Docs, README, kiểm thử; luận văn: Ch1 (phạm vi), Ch3 (khoảng dữ liệu trong prompt), Ch4 (CSDL 2 tháng, bộ mới), Ch5 (mục mới), Ch6, tóm tắt, PL C | build LaTeX 0 lỗi, 0 overfull |

Mỗi bước dừng lại báo cáo, và chỉ làm bước sau khi người dùng đồng ý.

### M1. Quy tắc gộp tháng trong `build-db`

`build-db --raw-dir data/raw/2026-09 --raw-dir data/raw/2026-10 --db ...`. Có thể truyền nhiều
`--raw-dir`, nhưng các tháng phải liền nhau.

- **blocks, transactions, token_transfers:** các tháng không giao nhau về thời gian, nên chỉ cần
  nối lại. Kiểm tra parity theo ngày bằng hợp các file `parity.json`, kiểm tra khóa ngoại trên mọi
  ngày.
- **contracts:** mỗi tháng lấy các hợp đồng mà tháng đó chạm tới, nên hai tháng chồng lấn nhau.
  Lấy hợp không trùng theo khóa (`address`, `block_number`). Nếu cùng một khóa mà khác giá trị thì
  dừng build.
- **tokens, amended_tokens:** mỗi tháng là một bản chụp `tokens_latest` tại lúc trích xuất
  (tháng 9 chụp ngày 31/10, tháng 10 chụp ngày 16/11). Với địa chỉ có ở cả hai bản, lấy bản mới
  nhất. Báo cáo ghi số địa chỉ có `decimals`, `symbol` hay `name` khác nhau giữa hai bản (D2).
- **Dấu vân tay (fingerprint):** so dấu vân tay của bảng đã lưu với dấu vân tay của đúng phép gộp
  đó chạy trên các file Parquet.
- **meta:** `meta.build` ghi thêm `months`. Mô tả bảng sinh từ khoảng dữ liệu. Giá trị ví dụ vẫn
  lấy từ các dòng đầu, tức từ tháng 9.

Ước lượng: CSDL khoảng 20 GB, thời gian build khoảng gấp đôi tháng 9. Ổ đĩa còn 882 GB (đo).

## 4. Quyết định cần duyệt

Trạng thái ngày 2026-11-17: người dùng duyệt D1, D4, D5. D6: tạm thời giữ nguyên KB, sẽ bổ sung
sau. D2: lấy bản mới nhất. D3: "cần sát thực tế", tức xếp hạng theo khoảng thời gian của câu hỏi
(cách b), thay cho đề xuất ban đầu (a).

Cách hiện thực D3 (làm ở M2; người dùng đồng ý cả hai điểm dưới đây ngày 2026-11-17):

- **Top-N của lớp** ("các sàn lớn", "5 cầu nối lớn nhất") xếp theo số lời gọi trong đúng khoảng
  [start, end) của câu hỏi. Câu không có thời gian thì xếp theo toàn bộ dữ liệu. Dòng gợi ý ghi
  khoảng đó thay cho "by calls in September". SQL chuẩn của bộ đánh giá dùng cùng quy tắc.
- **Prior của tên mơ hồ** vẫn đếm trên toàn bộ dữ liệu. Prior trả lời câu "tên này chỉ thực thể
  nào", không phải câu "thực thể nào lớn nhất trong khoảng này". Với khoảng một ngày, đếm theo
  ngày có thể làm thực thể được chọn đổi qua lại vì nhiễu.
- **Tái lập tháng 9:** các thí nghiệm tháng 9 xếp top-N theo cả tháng 9, kể cả với câu hỏi về một
  tuần hay một ngày. Quy tắc cũ được giữ thành một tùy chọn, để các lệnh kiểm tra tái lập kết quả
  tháng 9 vẫn ra đúng từng byte. Đã đếm (2026-11-17): trong bộ đánh giá tháng 9 có 70 câu hỏi
  theo lớp, 44 câu hỏi một khoảng ngắn hơn cả tháng. Quy tắc mới làm đổi danh sách top-N ở 11/44
  câu đó (3 dev, 8 test; ví dụ test `q00341` "các bridge lớn trong tuần cuối tháng 9": Mayan →
  deBridge).

Đo cho D2 và cho `contracts` (2026-11-17, trên Parquet). `tokens` có 45.441 địa chỉ ở cả hai
tháng (tháng 9 có 95.473, tháng 10 có 106.146). Trong số đó, `decimals`, `decimals_int`,
`token_standard`, `is_erc20` và `is_erc721` không lệch ở địa chỉ nào. Symbol và tên lệch ở đúng 1
địa chỉ: `0x8edcc305…` (Stake DAO, sdUSDCv2 → sdUSDC-HY), không có trong KB, bộ đánh giá hay dữ liệu
huấn luyện. `total_supply` lệch ở 6.174 địa chỉ, nhưng không nghĩa thuộc tính hay mẫu nào dùng cột
này. `amended_tokens` cũng như vậy. Vì thế hai lựa chọn của D2 cho kết quả giống hệt nhau trên mọi
câu hỏi hiện có. `contracts` có 99.047 dòng trùng khóa giữa hai tháng, và không dòng nào lệch giá
trị.

Đo cho D3 (2026-11-17, `top-5` theo số lời gọi tới địa chỉ của thành viên, so như tập hợp, KB
hiện tại): so với tháng 9, top-5 tính riêng tháng 10 khác ở 4/21 lớp (bridge: Mayan → Chainflip;
cex: Bitget → Crypto.com; layer-2: Starknet → Blast; lending: Euler → Liquity). Top-5 tính trên
cả 2 tháng chỉ khác tháng 9 ở 1 lớp (layer-2: Starknet → Blast).

| # | Câu hỏi | Đề xuất | Lý do |
|---|---|---|---|
| D1 | CSDL tháng 9 cũ | Giữ nguyên `ethereum.duckdb`, dựng file mới cho 2 tháng | toàn bộ kết quả và freeze check hiện có dùng file cũ |
| D2 | `tokens` có ở cả hai tháng | **Duyệt:** lấy bản chụp mới nhất | `tokens_latest` vốn là bản mới nhất; số địa chỉ lệch được ghi lại |
| D3 | Prior của bộ liên kết, top-N của lớp ("các sàn lớn") | **Người dùng chọn (b):** top-N theo khoảng thời gian của câu hỏi; prior vẫn theo toàn bộ dữ liệu (xem trên) | sát thực tế |
| D4 | Bộ câu hỏi mới | Cùng 40 mẫu, cùng số câu mỗi mẫu (505 câu), seed mới. Loại thời gian bốc như cũ; tháng neo bốc đều trong các tháng của CSDL; loại `range` vắt qua ranh giới tháng với xác suất 1/2; loại `none` nghĩa là cả 2 tháng | chia ngẫu nhiên tháng neo, nên so EX của câu neo tháng 9 với câu neo tháng 10 trong cùng một lần chạy cho ra tác động của tháng mà không bị lẫn độ khó của câu hỏi |
| D5 | Câu hỏi về khoảng ngoài dữ liệu (ví dụ tháng 11) | Bộ liên kết thêm một dòng cảnh báo vào khối gợi ý; không thêm câu hỏi loại này vào bộ mới | dòng cảnh báo không bao giờ xuất hiện với tháng 9, nên prompt cũ không đổi |
| D6 | KB | Giữ nguyên, không chạy lại `draft` trên tháng 10 | KB đã duyệt; token mới nổi trong tháng 10 không có bí danh, ghi thành hạn chế |

## 5. Rủi ro

- **RAM 10 GB.** Build và truy vấn trên khoảng 112 triệu giao dịch phải dựa vào spill của DuckDB
  (`memory_limit` 4 GB). Nếu quá chậm thì nạp và kiểm tra theo từng ngày như hiện nay.
- **Tháng 10 đi lệch tháng 9 về thực thể.** Một số thực thể có thể không còn hoạt động. Bộ sinh câu
  hỏi đã lọc theo hoạt động; nếu một mẫu không đủ câu thì báo cáo, không hạ ngưỡng.
- **Chuỗi tháng trong system prompt khác với lúc huấn luyện adapter.** Đây chính là điều cần đo,
  không phải lỗi cần sửa.

## 6. Thời gian (ước lượng)

M1: 1 ngày. M2: 0,5 ngày. M3: 1,5 ngày. M4–M6: 1 ngày, trong đó chạy mô hình khoảng 3 giờ
(4 cấu hình × 505 câu, theo tốc độ của S8). M7: 2–3 ngày. Tổng cộng khoảng 6–7 ngày công.
