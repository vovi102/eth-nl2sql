"""Train-only templates of phase 2 (docs/phase2-plan.md §2.1; proposed-method.md §9, decision 14).

The markup, slots and SQL frames follow benchmark_templates. No variant here may share a
signature (property senses, mention kinds, SQL frame) with a dev or test variant;
template_signature checks it, and lists the variants that only mirror one (R5). There is no
NIL or unsupported template: the SQL model is never asked about those questions (decision 16).

Counts: 2,000 questions, split over the groups as the benchmark (docs/benchmark.md §5) without
NIL and unsupported, and even, so that every template has as many Vietnamese questions as
English ones.

Wording rules (checked by tests/test_train_templates.py with the real phrase table):

- every phrase of a sense is one the linker knows, and the linker reads each surface with
  exactly the marked senses: no stray phrase ("lời" reads as "lỗi", "transfer" as "transfers");
- no capitalized word that is not a stopword starts a sentence (the linker takes it for an
  unknown name: "Name the ...", "Xếp hạng ...");
- a token slot without a token sense next to it gets the type word ("token {token}", "the
  token {token}"), else names shared with an entity (Uniswap, AAVE) link as the entity;
- no direction sense without an address mention: its evidence line would keep a bare `{E}`.

Filters (activity a filler needs, benchmark_build._active) add one kind to the benchmark's:
`token_out`, a token the target sent; the train builder fills it like `token_in`.
"""

from __future__ import annotations

from eth_nl2sql.benchmark_templates import ALL_TIMES, SPAN_TIMES, Template, Variant

DAY_TIMES = ("day",)  # hourly questions: one day, 24 rows at most

HOUR = "date_trunc('hour', block_timestamp)"
DAY = "CAST(block_timestamp AS DATE)"
FEE = "SUM(CAST(receipt_gas_used AS DECIMAL(38,0)) * receipt_effective_gas_price) / 1e18"
TOKEN_SUM = "SUM(CAST(value AS DECIMAL(38,0))) / {scale}"

AVG_GAS_PRICE = (
    "SELECT AVG(receipt_effective_gas_price) / 1e9 AS avg_gas_price_gwei FROM transactions "
    "WHERE to_address IN {target}{time_and}"
)
GAS_USED = (
    "SELECT SUM(receipt_gas_used) AS gas_used FROM transactions "
    "WHERE to_address IN {target}{time_and}"
)
HOURLY_CALLS = (
    f"SELECT {HOUR} AS hour, COUNT(*) AS tx_count FROM transactions "
    f"WHERE to_address IN {{target}}{{time_and}} GROUP BY {HOUR} ORDER BY hour ASC NULLS LAST"
)
TOKEN_SENT = (
    f"SELECT {TOKEN_SUM} AS amount FROM token_transfers "
    "WHERE token_address IN {token} AND from_address IN {target}{time_and}"
)
TOKEN_RECIPIENTS = (
    "SELECT COUNT(DISTINCT to_address) AS receiver_count FROM token_transfers "
    "WHERE token_address IN {token} AND from_address IN {target}{time_and}"
)
DAILY_ETH_RECEIVED = (
    f"SELECT {DAY} AS day, SUM(value) / 1e18 AS eth_received FROM transactions "
    f"WHERE to_address IN {{target}} AND receipt_status = 1{{time_and}} GROUP BY {DAY} "
    "ORDER BY day ASC NULLS LAST"
)
DAILY_CALLERS = (
    f"SELECT {DAY} AS day, COUNT(DISTINCT from_address) AS sender_count FROM transactions "
    f"WHERE to_address IN {{target}}{{time_and}} GROUP BY {DAY} ORDER BY day ASC NULLS LAST"
)
DAILY_TOKEN_AMOUNT = (
    f"SELECT {DAY} AS day, {TOKEN_SUM} AS amount FROM token_transfers "
    f"WHERE token_address IN {{token}}{{time_and}} GROUP BY {DAY} ORDER BY day ASC NULLS LAST"
)
DAILY_TOKEN_SENDERS = (
    f"SELECT {DAY} AS day, COUNT(DISTINCT from_address) AS sender_count FROM token_transfers "
    f"WHERE token_address IN {{token}}{{time_and}} GROUP BY {DAY} ORDER BY day ASC NULLS LAST"
)
DAILY_TOKEN_RECEIVERS = (
    f"SELECT {DAY} AS day, COUNT(DISTINCT to_address) AS receiver_count FROM token_transfers "
    f"WHERE token_address IN {{token}}{{time_and}} GROUP BY {DAY} ORDER BY day ASC NULLS LAST"
)
HOURLY_TRANSFERS = (
    f"SELECT {HOUR} AS hour, COUNT(*) AS transfer_count FROM token_transfers "
    f"WHERE token_address IN {{token}}{{time_and}} GROUP BY {HOUR} ORDER BY hour ASC NULLS LAST"
)
LITERAL_ETH_SENT = (
    "SELECT SUM(value) / 1e18 AS eth_sent FROM transactions "
    "WHERE from_address = {literal} AND receipt_status = 1{time_and}"
)


def _one(slots: dict[str, str], vi: tuple[str, ...], en: tuple[str, ...], sql: str,
         **filters: str) -> tuple[Variant, ...]:  # fmt: skip
    return (Variant(slots, {"vi": vi, "en": en}, sql, filters),)


TRAIN_TEMPLATES: tuple[Template, ...] = (
    # ---- entities with a closed address set: 480 questions ----
    Template("entity_avg_gas_price", "entity", _one(
        {"target": "entity"},
        ("[Giá gas](gas_price_paid) trung bình của các giao dịch [gọi](contract_called) {target} "
         "{time} là bao nhiêu?",
         "Các giao dịch [tương tác với](contract_called) {target} {time} trả "
         "[giá gas](gas_price_paid) trung bình bao nhiêu gwei?"),
        ("What was the average [gas price](gas_price_paid) of transactions that "
         "[called](contract_called) {target} {time}?",
         "What average [gas price](gas_price_paid) was paid by [callers of](contract_called) "
         "{target} {time}?"),
        AVG_GAS_PRICE, target="calls"), ALL_TIMES, 70),
    Template("entity_gas_used", "entity", _one(
        {"target": "entity"},
        ("Tổng [gas tiêu thụ](gas_used) của các giao dịch [gọi](contract_called) {target} {time} "
         "là bao nhiêu?",
         "[Lượng gas dùng](gas_used) của mọi giao dịch [gọi](contract_called) tới {target} "
         "{time} là bao nhiêu?"),
        ("What was the total [gas used](gas_used) by transactions that "
         "[called](contract_called) {target} {time}?",
         "Sum the [gas used](gas_used) of all [calls to](contract_called) {target} {time}."),
        GAS_USED, target="calls"), ALL_TIMES, 70),
    Template("entity_avg_gas_limit", "entity", _one(
        {"target": "entity"},
        ("[Giới hạn gas](gas_limit_tx) trung bình của các giao dịch [gọi](contract_called) "
         "{target} {time} là bao nhiêu?",
         "Các giao dịch [tương tác với](contract_called) {target} {time} đặt "
         "[giới hạn gas](gas_limit_tx) trung bình bao nhiêu?"),
        ("What was the average [gas limit](gas_limit_tx) of transactions that "
         "[called](contract_called) {target} {time}?",
         "What average [gas limit](gas_limit_tx) did [calls to](contract_called) {target} set "
         "{time}?"),
        "SELECT AVG(gas) AS avg_gas_limit FROM transactions WHERE to_address IN {target}"
        "{time_and}", target="calls"), ALL_TIMES, 68),
    Template("entity_token_sends_count", "entity", _one(
        {"target": "entity", "token": "token"},
        ("{target} đã [chuyển đi](sent_by) {token} qua [bao nhiêu lượt chuyển](transfer_count) "
         "{time}?",
         "Token {token} được [chuyển từ](sent_by) {target} đi [bao nhiêu lượt](transfer_count) "
         "{time}?"),
        ("[How many transfers](transfer_count) of {token} did {target} [send](sent_by) {time}?",
         "Count the token {token} [transfers](transfer_count) [sent from](sent_by) {target} "
         "{time}."),
        "SELECT COUNT(*) AS transfer_count FROM token_transfers "
        "WHERE token_address IN {token} AND from_address IN {target}{time_and}",
        target="token_out"), ALL_TIMES, 68),
    Template("entity_involving_tx_count", "entity", _one(
        {"target": "entity"},
        ("Có [bao nhiêu giao dịch](tx_count) [liên quan](involving) đến {target} {time}?",
         "[Số giao dịch](tx_count) [liên quan](involving) tới {target} {time} là bao nhiêu?"),
        ("[How many transactions](tx_count) {time} [involved](involving) {target}?",
         "Give the [number of transactions](tx_count) that [involved](involving) {target} {time}."),
        "SELECT COUNT(*) AS tx_count FROM transactions "
        "WHERE (from_address IN {target} OR to_address IN {target}){time_and}",
        target="calls"), ALL_TIMES, 68),
    Template("entity_token_recipients", "entity", _one(
        {"target": "entity", "token": "token"},
        ("Có [bao nhiêu địa chỉ nhận](distinct_receivers) token {token} được "
         "[chuyển từ](sent_by) {target} {time}?",
         "[Số địa chỉ nhận](distinct_receivers) token {token} [gửi từ](sent_by) {target} {time} "
         "là bao nhiêu?"),
        ("How many [unique receivers](distinct_receivers) got the token {token} "
         "[transferred from](sent_by) {target} {time}?",
         "To how many [unique receivers](distinct_receivers) did {target} [send](sent_by) the "
         "token {token} {time}?"),
        TOKEN_RECIPIENTS, target="token_out"), ALL_TIMES, 68),
    Template("entity_top_tokens_received", "entity", _one(
        {"target": "entity"},
        ("{target} [nhận](received_by) 5 token nào với [số lượt chuyển](transfer_count) nhiều "
         "nhất {time}?",
         "Liệt kê 5 token mà {target} [nhận](received_by) nhiều nhất {time} theo "
         "[số lượt chuyển](transfer_count)."),
        ("Which 5 tokens did {target} [receive](received_by) in the most "
         "[transfers](transfer_count) {time}?",
         "List the 5 tokens with the largest [number of transfers](transfer_count) "
         "[received](received_by) by {target} {time}."),
        "SELECT token_address, COUNT(*) AS transfer_count FROM token_transfers "
        "WHERE to_address IN {target}{time_and} GROUP BY token_address "
        "ORDER BY transfer_count DESC NULLS LAST, token_address ASC NULLS LAST LIMIT 5",
        target="calls"), ALL_TIMES, 68),
    # ---- named wallets and contracts: 280 questions ----
    Template("address_daily_eth_received", "address", _one(
        {"target": "address"},
        ("Thống kê [lượng ETH](eth_amount) mà {target} [nhận](received_by) [theo ngày](day) "
         "{time}.",
         "{target} [nhận](received_by) [bao nhiêu ETH](eth_amount) vào [mỗi ngày](day) "
         "{time}?"),
        ("Show the [daily](day) [ETH amount](eth_amount) [sent to](received_by) {target} {time}.",
         "Give the [daily](day) totals {time} of [how much ETH](eth_amount) {target} "
         "[received](received_by)."),
        DAILY_ETH_RECEIVED, target="eth_in"), SPAN_TIMES, 70),
    Template("address_fees_spent", "address", _one(
        {"target": "address"},
        ("{target} đã trả tổng bao nhiêu [phí giao dịch](tx_fee) cho các giao dịch "
         "[gửi](sent_by) đi {time}?",
         "Tổng [phí gas](tx_fee) mà {target} bỏ ra khi [gửi](sent_by) giao dịch {time} là bao "
         "nhiêu?"),
        ("What total [transaction fee](tx_fee) did {target} pay for the transactions it "
         "[sent](sent_by) {time}?",
         "How much [gas fee](tx_fee) was spent by transactions [sent from](sent_by) {target} "
         "{time}?"),
        f"SELECT {FEE} AS fee_eth FROM transactions WHERE from_address IN {{target}}{{time_and}}",
        target="sends"), ALL_TIMES, 70),
    Template("address_token_sent", "address", _one(
        {"target": "address", "token": "token"},
        ("{target} đã [gửi](sent_by) đi [bao nhiêu](token_amount) {token} {time}?",
         "Tính tổng [lượng](token_amount) {token} mà {target} đã [chuyển đi](sent_by) {time}."),
        ("What [amount](token_amount) of {token} did {target} [send](sent_by) out {time}?",
         "[How much](token_amount) {token} was [withdrawn from](sent_by) {target} {time}?"),
        TOKEN_SENT, target="token_out"), ALL_TIMES, 70),
    Template("address_failed_send_fees", "address", _one(
        {"target": "address"},
        ("{target} đã mất bao nhiêu [phí gas](tx_fee) cho các giao dịch [gửi](sent_by) đi bị "
         "[lỗi](tx_failed) {time}?",
         "Tổng [phí giao dịch](tx_fee) mà {target} trả cho các giao dịch [thất bại](tx_failed) "
         "đã [gửi](sent_by) {time} là bao nhiêu?"),
        ("How much [gas fee](tx_fee) did {target} spend on [failed](tx_failed) transactions it "
         "[sent](sent_by) {time}?",
         "What total [transaction fee](tx_fee) was paid for [reverted](tx_failed) transactions "
         "[sent from](sent_by) {target} {time}?"),
        f"SELECT {FEE} AS fee_eth FROM transactions "
        "WHERE from_address IN {target} AND receipt_status = 0{time_and}",
        target="sends"), ALL_TIMES, 70),
    # ---- tokens: 340 questions ----
    Template("token_daily_amount", "token", _one(
        {"token": "token"},
        ("[Lượng](token_amount) {token} luân chuyển [mỗi ngày](day) {time} ra sao?",
         "Thống kê tổng [lượng](token_amount) {token} được chuyển [theo ngày](day) {time}."),
        ("What [amount](token_amount) of {token} was transferred [per day](day) {time}?",
         "Show the [daily](day) [volume](token_amount) of {token} moved {time}."),
        DAILY_TOKEN_AMOUNT), SPAN_TIMES, 68),
    Template("token_transfers_to_address", "token", _one(
        {"target": "address", "token": "token"},
        ("{target} đã [nhận](received_by) {token} qua [bao nhiêu lượt chuyển](transfer_count) "
         "{time}?",
         "{token} được [gửi tới](received_by) {target} [bao nhiêu lượt](transfer_count) "
         "{time}?"),
        ("[How many transfers](transfer_count) of {token} did {target} "
         "[receive](received_by) {time}?",
         "What [number of transfers](transfer_count) of {token} were [sent to](received_by) "
         "{target} {time}?"),
        "SELECT COUNT(*) AS transfer_count FROM token_transfers "
        "WHERE token_address IN {token} AND to_address IN {target}{time_and}",
        target="token_in"), ALL_TIMES, 68),
    Template("token_involving_transfers", "token", _one(
        {"target": "address", "token": "token"},
        ("Có [bao nhiêu lượt chuyển](transfer_count) {token} [liên quan](involving) đến "
         "{target} {time}?",
         "{target} [liên quan](involving) đến [bao nhiêu lượt chuyển](transfer_count) {token} "
         "{time}?"),
        ("In [how many transfers](transfer_count) of {token} was {target} "
         "[involved](involving) {time}?",
         "Count the {token} [transfers](transfer_count) [involving](involving) {target} "
         "{time}."),
        "SELECT COUNT(*) AS transfer_count FROM token_transfers "
        "WHERE token_address IN {token} AND (from_address IN {target} OR to_address IN {target})"
        "{time_and}", target="token_in"), ALL_TIMES, 68),
    Template("token_daily_receivers", "token", _one(
        {"token": "token"},
        ("Thống kê [số địa chỉ nhận](distinct_receivers) token {token} [theo ngày](day) {time}.",
         "[Mỗi ngày](day) {time} có [bao nhiêu địa chỉ nhận](distinct_receivers) token "
         "{token}?"),
        ("Show the [daily](day) number of [unique receivers](distinct_receivers) of the token "
         "{token} {time}.",
         "How many [unique receivers](distinct_receivers) got the token {token} [per day](day) "
         "{time}?"),
        DAILY_TOKEN_RECEIVERS), SPAN_TIMES, 68),
    Template("token_distinct_receivers", "token", _one(
        {"token": "token"},
        ("Có [bao nhiêu địa chỉ nhận](distinct_receivers) token {token} khác nhau {time}?",
         "[Số địa chỉ nhận](distinct_receivers) token {token} {time} là bao nhiêu?"),
        ("How many [unique receivers](distinct_receivers) got the token {token} {time}?",
         "How many [unique receivers](distinct_receivers) did the token {token} have {time}?"),
        "SELECT COUNT(DISTINCT to_address) AS receiver_count FROM token_transfers "
        "WHERE token_address IN {token}{time_and}"), ALL_TIMES, 68),
    # ---- classes: 300 questions ----
    Template("class_daily_callers", "class", _one(
        {"target": "class"},
        ("[Số địa chỉ gửi](distinct_senders) khác nhau [gọi](contract_called) {target} "
         "[mỗi ngày](day) {time}?",
         "[Mỗi ngày](day) {time} có [bao nhiêu người dùng](distinct_senders) "
         "[tương tác với](contract_called) {target}?"),
        ("How many [unique senders](distinct_senders) [called](contract_called) {target} "
         "[per day](day) {time}?",
         "Show the [daily](day) number of [users](distinct_senders) who "
         "[interacted with](contract_called) {target} {time}."),
        DAILY_CALLERS), SPAN_TIMES, 76),
    Template("class_avg_gas_price", "class", _one(
        {"target": "class"},
        ("[Giá gas](gas_price_paid) trung bình khi [gọi](contract_called) {target} {time} là bao "
         "nhiêu?",
         "Người dùng [tương tác với](contract_called) {target} {time} trả "
         "[giá gas](gas_price_paid) trung bình bao nhiêu?"),
        ("What average [gas price](gas_price_paid) did transactions that "
         "[called](contract_called) {target} pay {time}?",
         "What was the mean [gas price](gas_price_paid) of [calls to](contract_called) {target} "
         "{time}?"),
        AVG_GAS_PRICE), ALL_TIMES, 76),
    Template("class_token_recipients", "class", _one(
        {"target": "class", "token": "token"},
        ("{target} đã [gửi](sent_by) token {token} tới "
         "[bao nhiêu địa chỉ nhận](distinct_receivers) khác nhau {time}?",
         "[Số địa chỉ nhận](distinct_receivers) token {token} [chuyển từ](sent_by) {target} "
         "{time} là bao nhiêu?"),
        ("To how many [unique receivers](distinct_receivers) did {target} [send](sent_by) the "
         "token {token} {time}?",
         "How many [unique receivers](distinct_receivers) got the token {token} "
         "[withdrawn from](sent_by) {target} {time}?"),
        TOKEN_RECIPIENTS, target="token_out"), ALL_TIMES, 74),
    Template("class_gas_used", "class", _one(
        {"target": "class"},
        ("Tổng [lượng gas dùng](gas_used) của các giao dịch [gọi](contract_called) {target} "
         "{time} là bao nhiêu?",
         "Các giao dịch [tương tác với](contract_called) {target} {time} có tổng "
         "[gas tiêu thụ](gas_used) bao nhiêu?"),
        ("What was the combined [gas used](gas_used) of [calls to](contract_called) {target} "
         "{time}?",
         "Give the total [gas used](gas_used) by transactions that [called](contract_called) "
         "{target} {time}."),
        GAS_USED), ALL_TIMES, 74),
    # ---- ambiguous property phrases (feature 4): 260 questions ----
    Template("ambiguous_daily_volume", "ambiguous", (
        Variant({"target": "entity"}, {
            "vi": ("Thống kê [khối lượng](eth_amount) mà {target} [nhận](received_by) "
                   "[theo ngày](day) {time}.",),
            "en": ("Show the [volume](eth_amount) {target} [received](received_by) "
                   "[per day](day) {time}.",),
        }, DAILY_ETH_RECEIVED, {"target": "eth_in"}),
        Variant({"target": "class"}, {
            "vi": ("[Mỗi ngày](day) {time}, [khối lượng](eth_amount) [nạp vào](received_by) "
                   "{target} là bao nhiêu?",),
            "en": ("Show the [volume](eth_amount) [deposited into](received_by) {target} "
                   "[per day](day) {time}.",),
        }, DAILY_ETH_RECEIVED),
        Variant({"token": "token"}, {
            "vi": ("Thống kê [khối lượng](token_amount) {token} được chuyển [theo ngày](day) "
                   "{time}.",),
            "en": ("[Per day](day) {time}, what [volume](token_amount) of {token} changed "
                   "hands?",),
        }, DAILY_TOKEN_AMOUNT),
    ), SPAN_TIMES, 66),
    Template("ambiguous_hourly_times", "ambiguous", (
        Variant({"target": "entity"}, {
            "vi": ("Liệt kê [số lượt](tx_count) {target} được [gọi](contract_called) "
                   "[theo giờ](hour) {time}.",),
            "en": ("List the [hourly](hour) count {time} of [how many times](tx_count) {target} "
                   "got [called](contract_called).",),
        }, HOURLY_CALLS, {"target": "calls"}),
        Variant({"token": "token"}, {
            "vi": ("[Mỗi giờ](hour) {time}, {token} được chuyển "
                   "[bao nhiêu lượt](transfer_count)?",),
            "en": ("List the [hourly](hour) [number of times](transfer_count) {token} changed "
                   "hands {time}.",),
        }, HOURLY_TRANSFERS),
    ), DAY_TIMES, 66),
    Template("ambiguous_value_sent", "ambiguous", (
        Variant({"target": "literal"}, {
            "vi": ("Tổng [giá trị](eth_amount) mà địa chỉ {target} đã [gửi](sent_by) đi {time} là "
                   "bao nhiêu?",),
            "en": ("What total [value](eth_amount) did {target} [send](sent_by) out {time}?",),
        }, LITERAL_ETH_SENT, {"target": "eth_out"}),
        Variant({"target": "address", "token": "token"}, {
            "vi": ("Tổng [giá trị](token_amount) {token} mà {target} đã [gửi](sent_by) đi {time} "
                   "là bao nhiêu?",),
            "en": ("What total [value](token_amount) of {token} did {target} [send](sent_by) out "
                   "{time}?",),
        }, TOKEN_SENT, {"target": "token_out"}),
    ), ALL_TIMES, 64),
    # Only the token variant: for an entity, "users" means callers (to_address IN {E1}), which
    # the evidence does not say and which only test ambiguous_users would then learn from.
    Template("ambiguous_daily_users", "ambiguous", _one(
        {"token": "token"},
        ("[Mỗi ngày](day) {time}, token {token} có [số người dùng](distinct_senders) là bao "
         "nhiêu?",
         "Thống kê [số người dùng](distinct_senders) của token {token} [theo ngày](day) {time}."),
        ("Show the [daily](day) number of [users](distinct_senders) of the token {token} "
         "{time}.",
         "How many [users](distinct_senders) did the token {token} have [per day](day) {time}?"),
        DAILY_TOKEN_SENDERS), SPAN_TIMES, 64),
    # ---- no named entity: a typed address or the whole chain: 340 questions ----
    Template("hourly_tx_count", "plain", _one(
        {},
        ("[Số giao dịch](tx_count) [mỗi giờ](hour) {time} là bao nhiêu?",
         "Thống kê [số giao dịch](tx_count) trên toàn mạng [theo giờ](hour) {time}."),
        ("What was the [hourly](hour) [number of transactions](tx_count) {time}?",
         "Show the [transaction count](tx_count) [per hour](hour) {time}."),
        f"SELECT {HOUR} AS hour, COUNT(*) AS tx_count FROM transactions{{time_where}} "
        f"GROUP BY {HOUR} ORDER BY hour ASC NULLS LAST"), DAY_TIMES, 58),
    Template("eip1559_avg_priority_fee", "plain", _one(
        {},
        ("[Phí ưu tiên](priority_fee_cap) trung bình của giao dịch [EIP-1559](tx_eip1559) {time} "
         "là bao nhiêu?",
         "Các giao dịch [EIP-1559](tx_eip1559) {time} đặt [phí ưu tiên](priority_fee_cap) trung "
         "bình bao nhiêu gwei?"),
        ("What was the average [priority fee](priority_fee_cap) of [EIP-1559](tx_eip1559) "
         "transactions {time}?",
         "On average, what [priority fee](priority_fee_cap) did [EIP-1559](tx_eip1559) "
         "transactions set {time}?"),
        "SELECT AVG(max_priority_fee_per_gas) / 1e9 AS avg_priority_fee_gwei FROM transactions "
        "WHERE transaction_type = 2{time_and}"), ALL_TIMES, 58),
    Template("literal_fees_spent", "plain", _one(
        {"target": "literal"},
        ("Địa chỉ {target} đã trả tổng bao nhiêu [phí giao dịch](tx_fee) cho các giao dịch "
         "[gửi](sent_by) đi {time}?",
         "{target} đã tiêu bao nhiêu [phí gas](tx_fee) cho các giao dịch [gửi](sent_by) đi "
         "{time}?"),
        ("How much did {target} pay in [gas fee](tx_fee) for the transactions it "
         "[sent](sent_by) {time}?",
         "What [transaction fee](tx_fee) was paid in total by transactions [sent from](sent_by) "
         "{target} {time}?"),
        f"SELECT {FEE} AS fee_eth FROM transactions WHERE from_address = {{literal}}{{time_and}}",
        target="sends"), ALL_TIMES, 56),
    Template("avg_base_fee", "plain", _one(
        {},
        ("[Phí cơ sở](base_fee) trung bình {time} là bao nhiêu gwei?",
         "Trung bình [phí cơ sở](base_fee) của các block {time} là bao nhiêu?"),
        ("What was the average [base fee](base_fee) {time}?",
         "What average [base fee](base_fee) did blocks have {time}?"),
        "SELECT AVG(base_fee_per_gas) / 1e9 AS avg_base_fee_gwei FROM blocks{block_time_where}"),
        ALL_TIMES, 56),
    Template("top_blocks_by_tx_count", "plain", _one(
        {},
        ("5 block nào có [số giao dịch trong block](block_tx_count) nhiều nhất {time}?",
         "Liệt kê 5 block đứng đầu {time} về [số giao dịch trong block](block_tx_count)."),
        ("Which 5 blocks ranked highest in [transactions per block](block_tx_count) {time}?",
         "List the top 5 blocks by [transactions per block](block_tx_count) {time}."),
        "SELECT number, transaction_count FROM blocks{block_time_where} "
        "ORDER BY transaction_count DESC NULLS LAST, number ASC NULLS LAST LIMIT 5"),
        ALL_TIMES, 56),
    Template("total_blob_fee", "plain", _one(
        {},
        ("Tổng [phí blob](blob_fee) {time} là bao nhiêu?",
         "Các giao dịch đã trả tổng cộng bao nhiêu [phí blob](blob_fee) {time}?"),
        ("What was the total [blob fee](blob_fee) paid {time}?",
         "How much [blob fee](blob_fee) did transactions pay in total {time}?"),
        "SELECT SUM(CAST(receipt_blob_gas_used AS DECIMAL(38,0)) * receipt_blob_gas_price) "
        "/ 1e18 AS blob_fee_eth FROM transactions{time_where}"), ALL_TIMES, 56),
)  # fmt: skip

TRAIN_TEMPLATES_BY_ID = {t.template_id: t for t in TRAIN_TEMPLATES}
TRAIN_GROUPS = ("entity", "address", "token", "class", "ambiguous", "plain")
# docs/phase2-plan.md §2.2: benchmark shares without NIL and unsupported, out of 2,000.
GROUP_QUESTIONS = {
    "entity": 480, "address": 280, "token": 340, "class": 300, "ambiguous": 260, "plain": 340,
}  # fmt: skip
