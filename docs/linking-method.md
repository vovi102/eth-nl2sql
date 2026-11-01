# Phương pháp schema/entity linking

Các quyết định đã chốt ngày 2026-10-31. Số liệu đo trên `data/raw/2026-09`.

## 1. Nguyên tắc: BigQuery là nguồn chuẩn

- Mô tả cột cho schema linking lấy nguyên văn mô tả chính thức của
  `bigquery-public-data.crypto_ethereum` (`bq show`). Bản tiếng Việt và từ đồng nghĩa là
  phần bổ sung, được đánh dấu riêng.
- Thông tin về token (symbol, name, decimals) lấy từ `tokens`; tính chất của địa chỉ
  (contract hay không) lấy từ `contracts` và `transactions`; mức độ hoạt động lấy từ dữ liệu
  tháng 9.
- Nguồn ngoài chỉ được **gắn tên** cho địa chỉ, và phải qua các bước đối chiếu ở mục 3. Khi
  có xung đột thì theo BigQuery; tên từ nguồn ngoài trở thành alias.

## 2. Bốn đặc thù blockchain và cách xử lý

| Đặc thù | Cách xử lý |
|---|---|
| Một thực thể có nhiều địa chỉ | Thực thể ánh xạ tới một **tập địa chỉ**, mỗi địa chỉ có vai trò riêng. Chỉ đặt câu hỏi ở mức thực thể khi tập địa chỉ là **tập đóng** (contract của một protocol). Sàn có tập địa chỉ mở (ví nạp tiền sinh liên tục) nên chỉ được hỏi qua **từng ví có tên** ("Coinbase 10") |
| Nhiều alias | Bảng alias: tên lấy từ nguồn (nameTag, tiền tố nameTag, symbol/name token) và alias soạn tay (viết tắt, mô tả, tiếng Việt), có ghi loại alias và trạng thái duyệt |
| Tham chiếu theo lớp ("any major DEX") | Bảng lớp (dex, bridge, mixer, cex, ...). Thành viên của lớp gán ở mức địa chỉ, theo các nhãn lớp của eth-labels kèm phần duyệt tay. "Major" được xác định bằng xếp hạng hoạt động trong tháng 9 |
| Property nhập nhằng | Bảng nghĩa property: một cụm từ ("giá trị", "phí") có nhiều nghĩa (cột/biểu thức), mỗi nghĩa ghi loại thực thể mà nó áp dụng. Kết quả entity linking giới hạn các cột mà schema linking được chọn |

Không đưa vào benchmark nhóm câu hỏi "tổng theo cả sàn" theo giả định thế giới đóng, vì biết trước
đáp án sẽ thấp hơn thực tế. Hướng mở rộng có thể làm sau: gom cụm ví nạp tiền của sàn bằng
heuristic trên dữ liệu BigQuery, dùng 19.067 ví nạp tiền Bitget có nhãn làm tập kiểm chứng.

## 3. Nguồn nhãn ngoài: eth-labels

`dawsbot/eth-labels`, được cào từ label cloud của Etherscan. Ghim commit `3446e22d` (2026-07-10).
Giấy phép MIT chỉ áp dụng cho code của repo; dữ liệu nhãn có nguồn gốc từ Etherscan và phải
trích dẫn Etherscan.

Đánh giá trên dữ liệu tháng 9:

- Tin được ở **nội dung**: định dạng địa chỉ khớp 100%; symbol token khớp với `tokens` ở 98,4%
  (7.446/7.567); các contract nổi tiếng có nhãn đúng; chỉ 30/2.256 nhãn kiểu contract lại thuộc
  địa chỉ tự gửi giao dịch, và đều giải thích được (ví vận hành bridge, người ký multisig).
- Yếu ở **độ phủ**: chỉ 5.870/86.924 địa chỉ hoạt động trong tháng 9; 28% địa chỉ không có
  `nameTag`; Bybit không có nhãn nào; Binance có 5 hot wallet có nhãn (3 hoạt động).
- **Label slug không đáng tin, `nameTag` đáng tin**: ví dụ không có slug `uniswap` (Uniswap chỉ
  mang nhãn lớp `dex`), còn 5.000 ví "Binance Dep" lại nằm dưới slug `bilaxy`. Vì vậy thực thể
  được xác định theo tiền tố `nameTag` ("Tornado.Cash: Router" → Tornado.Cash; "Coinbase 10" →
  Coinbase); slug chỉ dùng để gợi ý lớp.

Quy tắc nhận một địa chỉ có nhãn:

1. Địa chỉ đúng định dạng và có `nameTag`.
2. Thực thể của địa chỉ có hoạt động trong tháng 9. Mọi địa chỉ của thực thể đều được giữ, kể cả
   địa chỉ không hoạt động, để tập địa chỉ đóng không bị thiếu.
3. Nhãn kiểu contract (router, pool, token, ...) thì địa chỉ không được là ví thường. Bằng chứng
   lấy từ BigQuery có ba trạng thái (`is_contract`):
   - `true`: địa chỉ nằm trong `contracts`.
   - `false`: địa chỉ tự ký giao dịch, tức là ví thường.
   - để trống: không đủ bằng chứng. `contracts` chỉ chứa contract có hoạt động trong tháng, nên
     một pool không ai gọi trong tháng 9 không có trong đó.

   Chỉ `false` mới vi phạm quy tắc. Nhãn ví thì cho phép cả hai loại, vì sàn có thể dùng
   smart-contract wallet (ví dụ Coinbase 10).
4. Nhãn token có trong `tokens` thì symbol/name lấy theo `tokens`.
5. Tag nằm trên nhiều địa chỉ: Etherscan đổi tên hoặc đánh số lại ví, và eth-labels giữ tag cũ
   cạnh tag mới. Nếu **đúng một** địa chỉ mang tag đó làm tag duy nhất, tag thuộc về địa chỉ đó;
   trên các địa chỉ còn lại, alias bị loại tự động (`rejected`, ghi chú "Tên cũ"). Địa chỉ vẫn ở
   trong thực thể và vẫn gọi được bằng tag còn lại. Nếu không phân định được (đánh số lệch dây
   chuyền, hoặc hai địa chỉ cùng chỉ mang tag đó):
   - các địa chỉ thuộc **cùng một thực thể**: tự động giữ cả, alias trỏ tới tất cả. Alias vẫn link
     đúng thực thể, chỉ không phân biệt được địa chỉ con, nên không cảnh báo;
   - các địa chỉ thuộc **các thực thể khác nhau**: cảnh báo, người duyệt quyết định (vì có thể link
     sai thực thể).

## 4. Phân biệt token thật/giả

1.208 token có symbol `USDT`, và 89% lượt transfer thuộc các token có symbol trùng; nhiều token
giả dùng ký tự đồng dạng (`꒤5DT`). Số lượt transfer không phân biệt được thật/giả. Tín hiệu dùng
là **số ví khác nhau gọi trực tiếp contract token** trong tháng (USDT thật: 3,38 triệu; token
giả: 0–2). Gold SQL không bao giờ lọc token bằng `symbol = ...`; luôn dùng địa chỉ.

## 5. Cơ sở tri thức và quy trình duyệt

Các file CSV trong `data/entity_knowledge_base/` là nguồn đã qua duyệt; `build-db` chỉ nạp các dòng hợp lệ vào schema
`link` của DuckDB.

| File | Nội dung |
|---|---|
| `entity_classes.csv` | Danh mục lớp và các slug eth-labels tương ứng (soạn tay) |
| `entities.csv` | Thực thể, thực thể cha (Uniswap V3 → Uniswap), loại, tập đóng/mở, `same_as` để gộp trùng |
| `entity_addresses.csv` | Địa chỉ của thực thể, vai trò, lớp, bằng chứng từ BigQuery |
| `entity_aliases.csv` | Alias, ngôn ngữ, loại alias, đích (thực thể / địa chỉ / token / lớp), nguồn |

Mỗi dòng có cột `status`:

- `auto`: sinh máy móc từ nguồn đã chấp nhận.
- `pending`: đề xuất, chờ duyệt.
- `approved` / `rejected`: kết quả duyệt.

Chỉ dùng các dòng `auto`/`approved` mà thực thể đích đã `approved`.

`eth-nl2sql draft-entities` sinh lại các file này mà không ghi đè phần đã duyệt:

- Dòng đã có thì chỉ làm mới các cột bằng chứng.
- Dòng mới được thêm với trạng thái `pending` hoặc `auto`.
- Dòng không còn được sinh ra thì vẫn được giữ, trừ khi nó vẫn là `auto` (chưa ai duyệt nên xoá
  cũng không mất gì).

`eth-nl2sql check-entities` kiểm tra các tham chiếu và liệt kê những điểm cần người duyệt xem lại:

- Nhãn kiểu contract gắn cho địa chỉ tự ký giao dịch.
- Alias trỏ tới nhiều đích. Không tính trường hợp alias trỏ tới token (token được xếp hạng khi
  link), tới thực thể đã gộp bằng `same_as`, hoặc tới ví nằm trong chính thực thể mà alias đó gọi
  tên.
  Alias mà mọi đích đều đã `approved` là **alias một-nhiều đã duyệt**: khi link sẽ trả về tất cả
  các đích, và không còn bị cảnh báo.

Duyệt theo đợt khoảng 40 thực thể, theo thứ tự `rank`. Đề xuất của Claude có ghi chú bắt đầu bằng
`[Claude]` và luôn để trạng thái `pending`.

## 6. Nhật ký quyết định khi duyệt

| Ngày | Quyết định |
|---|---|
| 2026-11-01 | "ParaSwap" là alias của Velora (tên cũ); thực thể `paraswap` có cha là `velora` |
| 2026-11-01 | Giữ nhập nhằng có chủ đích giữa tên protocol/công ty và token: "Tether" → USDT, "Uniswap" → UNI, "Chainlink" → LINK |
| 2026-11-01 | Revolut (ngân hàng số) không thuộc lớp `cex`; chỉ hỏi theo tên ví. Áp dụng tương tự cho dịch vụ lưu ký và nhà phát hành stablecoin: lớp theo lớp chỉ chứa thành viên không gây tranh cãi |
| 2026-11-01 | Tag chung "MEV Bot: 0x…", "Bundler: 0x…" là nhóm (`kind = group`), không phải tổ chức: giữ các địa chỉ để lớp có thành viên, bỏ alias tên nhóm → thực thể để từ đó chỉ trỏ tới lớp |
| 2026-11-01 | Tag bị Etherscan đặt trùng cho 2 địa chỉ (Aave: Risk Steward V3, Convex Finance: Pool Manager, SushiSwap: LILPEPE, SushiSwap: XXX): giữ cả hai địa chỉ và giữ alias trỏ tới cả hai |
| 2026-11-01 | Duyệt toàn bộ đợt 1 theo đề xuất: 19 lớp, 49 thực thể, 67 alias |
| 2026-11-01 | Áp dụng quy tắc 5 (tag duy nhất) cho tag trùng; loại tự động 11 tag cũ, ví dụ "Coinbase 44" chỉ trỏ tới `0x28e7…`, không trỏ tới `0xb5d8…` (Coinbase 5, 130.127 giao dịch). 4 tag 1inch lệch số dây chuyền: giữ alias trỏ tới cả hai |
| 2026-11-01 | Tag trùng không phân định được giữa các địa chỉ cùng một thực thể: tự động giữ cả, không cảnh báo (khoảng 170 trường hợp ở đợt 2, phần lớn "Bancor: Converter N" và "Converter #N" bị trùng sau khi chuẩn hoá bỏ dấu `#`). Chỉ cảnh báo khi tag trùng giữa các thực thể khác nhau |
| 2026-11-01 | Duyệt **tạm thời** đợt 2 (rank 46–88) theo đề xuất: 40 thực thể, 2 lớp mới (`yield`, `rwa`), 31 alias. Arbitrum/Optimism/Polygon chỉ thuộc `layer-2` (không thuộc `bridge`, để lớp bridge không bị L2 lấn át); bỏ alias "Stake" (trùng động từ) và "LZ" (quá ngắn); Nexo, Stake.com không thuộc `cex`; hifiswap không gán lớp |
