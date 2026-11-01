"""Benchmark templates: question patterns with gold SQL frames (docs/benchmark.md §4).

A surface is a question with two kinds of markup. `[text](sense_id)` is a property said in the
question; `{slot}` is a mention, filled by the builder with a `Fill`. Rendering removes the
markup and records every span on the final string, so labels never need hand-counting. Senses
that the question does not say but the conventions require (`tx_success`, `of_token`) are added
by `implicit_senses`, with the same rules as the linker (docs/benchmark.md §2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

SURFACE_RE = re.compile(r"\[([^\]]+)\]\((\w+)\)|\{(\w+)\}")
MONTH_START = date(2026, 9, 1)
MONTH_END = date(2026, 10, 1)

# Mention kinds a slot can take. "named" kinds carry a placeholder in the gold SQL.
PLACEHOLDER_TYPES = ("entity", "address", "token", "class")


@dataclass(frozen=True)
class Fill:
    """What goes into a slot: the text is the span, the prefix stays outside it ("các ", "ví ")."""

    text: str
    mention: dict | None = None  # type/target/... of linking-labels.md §2; None for time
    prefix: str = ""
    time: tuple[date, date] | None = None  # [start, end) for the time slot


@dataclass(frozen=True)
class Variant:
    """One way to ask: slot kinds, surfaces per language and the gold SQL frame.

    The SQL frame is a str.format pattern. Slot names become placeholders ({entity} -> {E1}),
    and the builder supplies {time_and}, {time_where}, {scale} and {literal}.
    """

    slots: dict[str, str]  # slot name -> mention kind (entity, address, token, class, ...)
    surfaces: dict[str, tuple[str, ...]]  # lang -> surfaces
    sql: str | None
    filters: dict[str, str] = field(default_factory=dict)  # slot -> activity the filler needs


@dataclass(frozen=True)
class Template:
    template_id: str
    group: str
    variants: tuple[Variant, ...]
    times: tuple[str, ...]  # time kinds the template may use (see TIME_PHRASES)
    count: int  # questions to generate
    unsupported: bool = False


# ---- time ---------------------------------------------------------------------------------

ORDINAL_EN = {1: "first", 2: "second", 3: "third", 4: "fourth"}


def time_fill(kind: str, lang: str, day: int = 1, last: int = 1) -> Fill | None:
    """The time slot. Vietnamese drops "trong" from the span; English keeps "in"/"on"/"the"."""
    d = MONTH_START
    if kind == "none":
        return None
    if kind == "month":
        start, end, vi, en = d, MONTH_END, "tháng 9", "in September"
    elif kind == "day":
        start = d.replace(day=day)
        end = start + timedelta(days=1)
        vi, en = f"ngày {day}/9", f"on September {day}"
    elif kind == "week":  # week k of September = [7k-6, 7k+1)
        start, end = d.replace(day=7 * day - 6), d.replace(day=7 * day + 1)
        vi = "tuần đầu tháng 9" if day == 1 else f"tuần thứ {day} tháng 9"
        en = f"in the {ORDINAL_EN[day]} week of September"
    elif kind == "last_week":
        start, end, vi, en = (
            d.replace(day=24),
            MONTH_END,
            "tuần cuối tháng 9",
            ("in the last week of September"),
        )
    elif kind == "half":
        start, end = d, d.replace(day=16)
        vi, en = "nửa đầu tháng 9", "in the first half of September"
    elif kind == "range":
        start, end = d.replace(day=day), d.replace(day=last) + timedelta(days=1)
        vi, en = f"từ {day}/9 đến {last}/9", f"from September {day} to {last}"
    else:
        raise ValueError(kind)
    if lang == "vi":
        prefix = "" if vi.startswith(("ngày", "từ")) else "trong "
        return Fill(vi, prefix=prefix, time=(start, end))
    return Fill(en, time=(start, end))


def time_condition(window: tuple[date, date] | None, column: str = "block_timestamp") -> str:
    if window is None:
        return ""
    start, end = window
    return f"{column} >= TIMESTAMP '{start} 00:00:00' AND {column} < TIMESTAMP '{end} 00:00:00'"


# ---- rendering ----------------------------------------------------------------------------


@dataclass
class Rendered:
    question: str
    mentions: list[dict]
    slots: list[str]  # the slot of each mention
    properties: list[dict]
    unsupported: list[dict]
    time: dict | None


def render(surface: str, fills: dict[str, Fill | None], unsupported_kinds: set[str]) -> Rendered:
    """Fill the slots of a surface and record spans on the resulting question.

    A slot filled with None (no time) disappears together with the space before it.
    """
    out, mentions, slots, properties, unsupported, time = "", [], [], [], [], None
    pos = 0
    for m in SURFACE_RE.finditer(surface):
        out += surface[pos : m.start()]
        pos = m.end()
        if m.group(1) is not None:  # [text](sense)
            text, sense = m.group(1), m.group(2)
            span = [len(out), len(out) + len(text)]
            out += text
            item = {"sense_id": sense, "span": span, "text": text}
            if sense in unsupported_kinds:
                unsupported.append(item)
            else:
                properties.append(item | {"implicit": False})
            continue
        fill = fills[m.group(3)]
        if fill is None:
            out = out.rstrip(" ")
            continue
        out += fill.prefix
        span = [len(out), len(out) + len(fill.text)]
        out += fill.text
        if fill.time is not None:
            start, end = fill.time
            time = {
                "start": f"{start}T00:00:00",
                "end": f"{end}T00:00:00",
                "span": span,
                "text": fill.text,
            }
        else:
            mentions.append({"span": span, "text": fill.text} | fill.mention)
            slots.append(m.group(3))
    out += surface[pos:]
    if out and out[0].islower() and not any(m["span"][0] == 0 for m in mentions):
        out = out[0].upper() + out[1:]  # a sentence starts with a capital, a name stays as is
    return Rendered(out, mentions, slots, properties, unsupported, time)


def assign_placeholders(mentions: list[dict]) -> None:
    """E1, E2, ... in order of appearance; mentions of one target share one (§6)."""
    names: dict[tuple, str] = {}
    for m in sorted(mentions, key=lambda m: m["span"][0]):
        if m["type"] in PLACEHOLDER_TYPES:
            key = (m["type"], m["target"], m.get("top_n"))
            names.setdefault(key, f"E{len(names) + 1}")
            m["placeholder"] = names[key]


def implicit_senses(properties: list[dict], mentions: list[dict], unsupported: list) -> list:
    """Senses the conventions add without words (docs/benchmark.md §2, Nhãn)."""
    if unsupported:
        return []
    said = {p["sense_id"] for p in properties}
    linked = any(m["type"] != "nil" for m in mentions)
    implicit = []
    if (
        "eth_amount" in said
        and said & {"received_by", "sent_by", "involving"}
        and linked
        and not said & {"tx_success", "tx_failed"}
    ):
        implicit.append("tx_success")
    if any(m["type"] == "token" for m in mentions) and "of_token" not in said:
        implicit.append("of_token")
    return [{"sense_id": s, "span": None, "text": None, "implicit": True} for s in implicit]


# ---- the catalog --------------------------------------------------------------------------

ALL_TIMES = ("none", "month", "day", "week", "last_week", "half", "range")
SPAN_TIMES = ("month", "week", "last_week", "half", "range")  # several days
ETH_RECEIVED = (
    "SELECT SUM(value) / 1e18 AS eth_received FROM transactions "
    "WHERE to_address IN {target} AND receipt_status = 1{time_and}"
)
ETH_SENT = (
    "SELECT SUM(value) / 1e18 AS eth_sent FROM transactions "
    "WHERE from_address IN {target} AND receipt_status = 1{time_and}"
)
CALL_COUNT = "SELECT COUNT(*) AS tx_count FROM transactions WHERE to_address IN {target}{time_and}"
DISTINCT_CALLERS = (
    "SELECT COUNT(DISTINCT from_address) AS sender_count FROM transactions "
    "WHERE to_address IN {target}{time_and}"
)
TOKEN_RECEIVED = (
    "SELECT SUM(CAST(value AS DECIMAL(38,0))) / {scale} AS amount FROM token_transfers "
    "WHERE token_address IN {token} AND to_address IN {target}{time_and}"
)
TRANSFER_COUNT = (
    "SELECT COUNT(*) AS transfer_count FROM token_transfers WHERE token_address IN {token}"
    "{time_and}"
)
TOKEN_AMOUNT = (
    "SELECT SUM(CAST(value AS DECIMAL(38,0))) / {scale} AS amount FROM token_transfers "
    "WHERE token_address IN {token}{time_and}"
)
TOKEN_SENDERS = (
    "SELECT COUNT(DISTINCT from_address) AS sender_count FROM token_transfers "
    "WHERE token_address IN {token}{time_and}"
)


def _one(slots: dict[str, str], vi: tuple[str, ...], en: tuple[str, ...], sql: str | None,
         **filters: str) -> tuple[Variant, ...]:  # fmt: skip
    return (Variant(slots, {"vi": vi, "en": en}, sql, filters),)


TEMPLATES: tuple[Template, ...] = (
    # ---- A. entities with a closed address set (feature 1, 2) ----
    Template("entity_eth_received", "entity", _one(
        {"target": "entity"},
        ("{target} [nhận](received_by) [bao nhiêu ETH](eth_amount) {time}?",
         "[Lượng ETH](eth_amount) mà {target} [nhận](received_by) {time} là bao nhiêu?"),
        ("[How much ETH](eth_amount) did {target} [receive](received_by) {time}?",
         "What was the total [ETH amount](eth_amount) [received](received_by) by {target} {time}?"),
        ETH_RECEIVED, target="eth_in"), ALL_TIMES, 14),
    Template("entity_call_count", "entity", _one(
        {"target": "entity"},
        ("Có [bao nhiêu giao dịch](tx_count) [gọi](contract_called) {target} {time}?",
         "[Số giao dịch](tx_count) [tương tác với](contract_called) {target} {time} là bao nhiêu?"),
        ("[How many transactions](tx_count) [called](contract_called) {target} {time}?",
         "What is the [number of transactions](tx_count) that [interacted with](contract_called) "
         "{target} {time}?"),
        CALL_COUNT, target="calls"), ALL_TIMES, 14),
    Template("entity_distinct_callers", "entity", _one(
        {"target": "entity"},
        ("Có bao nhiêu [địa chỉ gửi](distinct_senders) khác nhau đã [gọi](contract_called) "
         "{target} {time}?",
         "[Số người dùng](distinct_senders) đã [tương tác với](contract_called) {target} {time} "
         "là bao nhiêu?"),
        ("How many [unique senders](distinct_senders) [called](contract_called) {target} {time}?",
         "How many [users](distinct_senders) [interacted with](contract_called) {target} {time}?"),
        DISTINCT_CALLERS, target="calls"), ALL_TIMES, 14),
    Template("entity_failed_calls", "entity", _one(
        {"target": "entity"},
        ("Có [bao nhiêu giao dịch](tx_count) [gọi](contract_called) {target} bị "
         "[thất bại](tx_failed) {time}?",
         "[Số giao dịch](tx_count) [lỗi](tx_failed) khi [gọi](contract_called) {target} {time} "
         "là bao nhiêu?"),
        ("[How many transactions](tx_count) that [called](contract_called) {target} "
         "[failed](tx_failed) {time}?",
         "How many [reverted](tx_failed) [transactions](tx_count) [called](contract_called) "
         "{target} {time}?"),
        "SELECT COUNT(*) AS tx_count FROM transactions WHERE to_address IN {target} "
        "AND receipt_status = 0{time_and}", target="failed_calls"), ALL_TIMES, 14),
    Template("entity_fees_paid", "entity", _one(
        {"target": "entity"},
        ("Người [gọi](contract_called) {target} đã trả tổng bao nhiêu [phí giao dịch](tx_fee) "
         "{time}?",
         "Tổng [phí gas](tx_fee) của các giao dịch [gọi](contract_called) {target} {time} là "
         "bao nhiêu?"),
        ("What total [transaction fee](tx_fee) did [callers of](contract_called) {target} pay "
         "{time}?",
         "How much [gas fee](tx_fee) was paid by transactions that [called](contract_called) "
         "{target} {time}?"),
        "SELECT SUM(CAST(receipt_gas_used AS DECIMAL(38,0)) * receipt_effective_gas_price) "
        "/ 1e18 AS fee_eth FROM transactions WHERE to_address IN {target}{time_and}",
        target="calls"), ALL_TIMES, 14),
    Template("entity_daily_calls", "entity", _one(
        {"target": "entity"},
        ("[Số giao dịch](tx_count) [gọi](contract_called) {target} [mỗi ngày](day) {time}?",
         "Thống kê [số giao dịch](tx_count) [tương tác với](contract_called) {target} "
         "[theo ngày](day) {time}."),
        ("What was the [daily](day) [number of transactions](tx_count) that "
         "[called](contract_called) {target} {time}?",
         "Show the [transaction count](tx_count) [per day](day) for [calls to](contract_called) "
         "{target} {time}."),
        "SELECT CAST(block_timestamp AS DATE) AS day, COUNT(*) AS tx_count FROM transactions "
        "WHERE to_address IN {target}{time_and} GROUP BY CAST(block_timestamp AS DATE) "
        "ORDER BY day ASC NULLS LAST", target="calls"), SPAN_TIMES, 14),
    Template("entity_top_callers", "entity", _one(
        {"target": "entity"},
        ("5 địa chỉ nào [gọi](contract_called) {target} với [số giao dịch](tx_count) nhiều nhất "
         "{time}?",
         "Liệt kê 5 ví [gọi](contract_called) {target} nhiều nhất {time} kèm "
         "[số giao dịch](tx_count)."),
        ("Which 5 addresses [called](contract_called) {target} the most {time}? Give their "
         "[transaction count](tx_count).",
         "List the 5 addresses with the largest [number of transactions](tx_count) "
         "[calling](contract_called) {target} {time}."),
        "SELECT from_address, COUNT(*) AS tx_count FROM transactions WHERE to_address IN "
        "{target}{time_and} GROUP BY from_address "
        "ORDER BY tx_count DESC NULLS LAST, from_address ASC NULLS LAST LIMIT 5",
        target="calls"), ALL_TIMES, 14),
    Template("entity_token_received", "entity", _one(
        {"target": "entity", "token": "token"},
        ("{target} [nhận](received_by) [bao nhiêu](token_amount) {token} {time}?",
         "[Lượng](token_amount) {token} mà {target} [nhận](received_by) {time} là bao nhiêu?"),
        ("[How much](token_amount) {token} did {target} [receive](received_by) {time}?",
         "What [amount](token_amount) of {token} was [received](received_by) by {target} {time}?"),
        TOKEN_RECEIVED, target="token_in"), ALL_TIMES, 14),
    # ---- B. named wallets and contracts ----
    Template("address_eth_received", "address", _one(
        {"target": "address"},
        ("{target} [nhận](received_by) [bao nhiêu ETH](eth_amount) {time}?",
         "[Lượng ETH](eth_amount) [được gửi tới](received_by) {target} {time} là bao nhiêu?"),
        ("[How much ETH](eth_amount) did {target} [receive](received_by) {time}?",
         "What [ETH amount](eth_amount) was [sent to](received_by) {target} {time}?"),
        ETH_RECEIVED, target="eth_in"), ALL_TIMES, 13),
    Template("address_eth_sent", "address", _one(
        {"target": "address"},
        ("{target} đã [gửi](sent_by) đi [bao nhiêu ETH](eth_amount) {time}?",
         "[Lượng ETH](eth_amount) [rút ra](sent_by) từ {target} {time} là bao nhiêu?"),
        ("[How much ETH](eth_amount) did {target} [send](sent_by) {time}?",
         "What [ETH amount](eth_amount) was [sent from](sent_by) {target} {time}?"),
        ETH_SENT, target="eth_out"), ALL_TIMES, 13),
    Template("address_tx_sent_count", "address", _one(
        {"target": "address"},
        ("{target} đã [gửi](sent_by) [bao nhiêu giao dịch](tx_count) {time}?",
         "[Số giao dịch](tx_count) [gửi](sent_by) từ {target} {time} là bao nhiêu?"),
        ("[How many transactions](tx_count) did {target} [send](sent_by) {time}?",
         "What is the [number of transactions](tx_count) [sent from](sent_by) {target} {time}?"),
        "SELECT COUNT(*) AS tx_count FROM transactions WHERE from_address IN {target}{time_and}",
        target="sends"), ALL_TIMES, 13),
    Template("address_token_received", "address", _one(
        {"target": "address", "token": "token"},
        ("{target} [nhận](received_by) [bao nhiêu](token_amount) {token} {time}?",
         "[Lượng](token_amount) {token} mà {target} [nhận](received_by) {time} là bao nhiêu?"),
        ("[How much](token_amount) {token} did {target} [receive](received_by) {time}?",
         "What [amount](token_amount) of {token} was [received](received_by) by {target} {time}?"),
        TOKEN_RECEIVED, target="token_in"), ALL_TIMES, 13),
    Template("address_largest_eth_sent", "address", _one(
        {"target": "address"},
        ("Giao dịch có [lượng ETH](eth_amount) lớn nhất mà {target} [gửi](sent_by) {time} là "
         "giao dịch nào?",
         "Giao dịch nào [chuyển đi](sent_by) nhiều [ETH](eth_amount) nhất từ {target} {time}?"),
        ("Which transaction [sent from](sent_by) {target} had the largest "
         "[ETH value](eth_amount) {time}?",
         "What was the largest [ETH amount](eth_amount) that {target} [sent](sent_by) in one "
         "transaction {time}?"),
        "SELECT hash, value / 1e18 AS eth FROM transactions WHERE from_address IN {target} "
        "AND receipt_status = 1{time_and} ORDER BY value DESC NULLS LAST, hash ASC NULLS LAST "
        "LIMIT 1", target="eth_out"), ALL_TIMES, 13),
    # ---- C. tokens (feature 2: shared symbols, names shared with entities) ----
    Template("token_transfer_count", "token", _one(
        {"token": "token"},
        ("Có [bao nhiêu lượt chuyển](transfer_count) {token} {time}?",
         "[Số lượt chuyển](transfer_count) {token} {time} là bao nhiêu?"),
        ("What is the [number of transfers](transfer_count) of {token} {time}?",
         "[How many](transfer_count) [transfers of](of_token) {token} happened {time}?"),
        TRANSFER_COUNT), ALL_TIMES, 13),
    Template("token_amount_total", "token", _one(
        {"token": "token"},
        ("Tổng [lượng](token_amount) {token} được chuyển {time} là bao nhiêu?",
         "[Giá trị chuyển](token_amount) của {token} {time} là bao nhiêu?"),
        ("What total [amount](token_amount) of {token} was transferred {time}?",
         "What was the total {token} transfer [volume](token_amount) {time}?"),
        TOKEN_AMOUNT), ALL_TIMES, 13),
    Template("token_distinct_senders", "token", _one(
        {"token": "token"},
        ("Có bao nhiêu [địa chỉ gửi](distinct_senders) khác nhau chuyển {token} {time}?",
         "[Số ví hoạt động](distinct_senders) chuyển {token} {time} là bao nhiêu?"),
        ("How many [unique senders](distinct_senders) transferred {token} {time}?",
         "How many [active addresses](distinct_senders) moved {token} {time}?"),
        TOKEN_SENDERS), ALL_TIMES, 13),
    Template("token_top_receivers", "token", _one(
        {"token": "token"},
        ("5 địa chỉ nào [nhận](received_by) [lượng](token_amount) {token} lớn nhất {time}?",
         "Liệt kê 5 ví [nhận](received_by) nhiều {token} nhất {time} kèm [lượng](token_amount)."),
        ("Which 5 addresses [received](received_by) the largest [amount](token_amount) of "
         "{token} {time}?",
         "List the 5 addresses that [received](received_by) the most {token} {time}, with the "
         "[amount](token_amount)."),
        "SELECT to_address, SUM(CAST(value AS DECIMAL(38,0))) / {scale} AS amount "
        "FROM token_transfers WHERE token_address IN {token}{time_and} GROUP BY to_address "
        "ORDER BY amount DESC NULLS LAST, to_address ASC NULLS LAST LIMIT 5"), ALL_TIMES, 13),
    Template("token_largest_transfer", "token", _one(
        {"token": "token"},
        ("Lượt chuyển {token} có [giá trị chuyển](token_amount) lớn nhất {time} là lượt nào?",
         "Lượt chuyển {token} lớn nhất {time} chuyển bao nhiêu [lượng token](token_amount)?"),
        ("Which {token} transfer had the largest [amount](token_amount) {time}?",
         "What was the largest single {token} transfer [amount](token_amount) {time}?"),
        "SELECT transaction_hash, log_index, CAST(value AS DECIMAL(38,0)) / {scale} AS amount "
        "FROM token_transfers WHERE token_address IN {token}{time_and} "
        "ORDER BY CAST(value AS DECIMAL(38,0)) DESC NULLS LAST, transaction_hash ASC NULLS LAST, "
        "log_index ASC NULLS LAST LIMIT 1"), ALL_TIMES, 13),
    Template("token_daily_transfers", "token", _one(
        {"token": "token"},
        ("[Số lượt chuyển](transfer_count) {token} [mỗi ngày](day) {time}?",
         "Thống kê [số lượt chuyển](transfer_count) {token} [theo ngày](day) {time}."),
        ("What was the [daily](day) [number of transfers](transfer_count) of {token} {time}?",
         "Show the [number of transfers](transfer_count) of {token} [per day](day) {time}."),
        "SELECT CAST(block_timestamp AS DATE) AS day, COUNT(*) AS transfer_count "
        "FROM token_transfers WHERE token_address IN {token}{time_and} "
        "GROUP BY CAST(block_timestamp AS DATE) ORDER BY day ASC NULLS LAST"), SPAN_TIMES, 13),
    # ---- D. classes (feature 3) ----
    Template("class_eth_received", "class", _one(
        {"target": "class"},
        ("{target} [nhận](received_by) [bao nhiêu ETH](eth_amount) {time}?",
         "Tổng [lượng ETH](eth_amount) {target} [nhận](received_by) {time} là bao nhiêu?"),
        ("[How much ETH](eth_amount) did {target} [receive](received_by) {time}?",
         "What total [ETH amount](eth_amount) did {target} [receive](received_by) {time}?"),
        ETH_RECEIVED), ALL_TIMES, 14),
    Template("class_call_count", "class", _one(
        {"target": "class"},
        ("Có [bao nhiêu giao dịch](tx_count) [gọi](contract_called) {target} {time}?",
         "[Số giao dịch](tx_count) [tương tác với](contract_called) {target} {time} là bao nhiêu?"),
        ("[How many transactions](tx_count) [called](contract_called) {target} {time}?",
         "What is the [number of transactions](tx_count) that [interacted with](contract_called) "
         "{target} {time}?"),
        CALL_COUNT), ALL_TIMES, 14),
    Template("class_distinct_callers", "class", _one(
        {"target": "class"},
        ("Có bao nhiêu [địa chỉ gửi](distinct_senders) khác nhau đã [gọi](contract_called) "
         "{target} {time}?",
         "[Số người dùng](distinct_senders) đã [tương tác với](contract_called) {target} {time} "
         "là bao nhiêu?"),
        ("How many [unique senders](distinct_senders) [interacted with](contract_called) "
         "{target} {time}?",
         "How many [users](distinct_senders) [called](contract_called) {target} {time}?"),
        DISTINCT_CALLERS), ALL_TIMES, 14),
    Template("class_token_received", "class", _one(
        {"target": "class", "token": "token"},
        ("{target} [nhận](received_by) [bao nhiêu](token_amount) {token} {time}?",
         "[Lượng](token_amount) {token} mà {target} [nhận](received_by) {time} là bao nhiêu?"),
        ("[How much](token_amount) {token} did {target} [receive](received_by) {time}?",
         "What [amount](token_amount) of {token} was [received](received_by) by {target} {time}?"),
        TOKEN_RECEIVED, target="token_in"), ALL_TIMES, 14),
    Template("class_top_contracts", "class", _one(
        {"target": "class"},
        ("5 contract nào của {target} được [gọi](contract_called) nhiều nhất {time}, xếp theo "
         "[số giao dịch](tx_count)?",
         "Liệt kê 5 contract của {target} có [số giao dịch](tx_count) [gọi](contract_called) "
         "tới nhiều nhất {time}."),
        ("Which 5 contracts of {target} were [called](contract_called) the most {time}? Rank by "
         "[transaction count](tx_count).",
         "List the 5 contracts of {target} with the largest [number of transactions](tx_count) "
         "[calling](contract_called) them {time}."),
        "SELECT to_address, COUNT(*) AS tx_count FROM transactions WHERE to_address IN "
        "{target}{time_and} GROUP BY to_address "
        "ORDER BY tx_count DESC NULLS LAST, to_address ASC NULLS LAST LIMIT 5"), ALL_TIMES, 14),
    # ---- E. ambiguous property phrases (feature 4): the target type decides the sense ----
    Template("ambiguous_volume", "ambiguous", (
        Variant({"target": "entity"}, {
            "vi": ("[Khối lượng](eth_amount) [chảy vào](received_by) {target} {time} là bao "
                   "nhiêu?",),
            "en": ("What was the [volume](eth_amount) [sent to](received_by) {target} {time}?",),
        }, ETH_RECEIVED, {"target": "eth_in"}),
        Variant({"token": "token"}, {
            "vi": ("[Khối lượng](token_amount) {token} được chuyển {time} là bao nhiêu?",),
            "en": ("What was the [volume](token_amount) of {token} transferred {time}?",),
        }, TOKEN_AMOUNT),
    ), ALL_TIMES, 15),
    Template("ambiguous_value_received", "ambiguous", (
        Variant({"target": "address"}, {
            "vi": ("Tổng [giá trị](eth_amount) {target} [nhận](received_by) được {time} là bao "
                   "nhiêu?",),
            "en": ("What total [value](eth_amount) did {target} [receive](received_by) {time}?",),
        }, ETH_RECEIVED, {"target": "eth_in"}),
        Variant({"target": "address", "token": "token"}, {
            "vi": ("Tổng [giá trị](token_amount) {token} mà {target} [nhận](received_by) {time} "
                   "là bao nhiêu?",),
            "en": ("What total [value](token_amount) of {token} did {target} "
                   "[receive](received_by) {time}?",),
        }, TOKEN_RECEIVED, {"target": "token_in"}),
    ), ALL_TIMES, 15),
    Template("ambiguous_times", "ambiguous", (
        Variant({"target": "entity"}, {
            "vi": ("[Số lượt](tx_count) {target} được [gọi](contract_called) {time} là bao "
                   "nhiêu?",),
            "en": ("[How many times](tx_count) was {target} [called](contract_called) {time}?",),
        }, CALL_COUNT, {"target": "calls"}),
        Variant({"token": "token"}, {
            "vi": ("[Số lượt](transfer_count) {token} được chuyển {time} là bao nhiêu?",),
            "en": ("[How many times](transfer_count) was {token} transferred {time}?",),
        }, TRANSFER_COUNT),
    ), ALL_TIMES, 15),
    Template("ambiguous_users", "ambiguous", (
        Variant({"target": "entity"}, {
            "vi": ("[Số người dùng](distinct_senders) của {target} {time} là bao nhiêu?",),
            "en": ("How many [users](distinct_senders) did {target} have {time}?",),
        }, DISTINCT_CALLERS, {"target": "calls"}),
        Variant({"token": "token"}, {
            "vi": ("[Số người dùng](distinct_senders) của {token} {time} là bao nhiêu?",),
            "en": ("How many [users](distinct_senders) did {token} have {time}?",),
        }, TOKEN_SENDERS),
    ), ALL_TIMES, 15),
    # ---- F. no named entity: typed addresses and chain-wide questions ----
    Template("literal_eth_received", "plain", _one(
        {"target": "literal"},
        ("Địa chỉ {target} [nhận](received_by) [bao nhiêu ETH](eth_amount) {time}?",),
        ("[How much ETH](eth_amount) did {target} [receive](received_by) {time}?",),
        "SELECT SUM(value) / 1e18 AS eth_received FROM transactions "
        "WHERE to_address = {literal} AND receipt_status = 1{time_and}",
        target="eth_in"), ALL_TIMES, 12),
    Template("literal_tx_sent_count", "plain", _one(
        {"target": "literal"},
        ("{target} đã [gửi](sent_by) [bao nhiêu giao dịch](tx_count) {time}?",),
        ("[How many transactions](tx_count) did {target} [send](sent_by) {time}?",),
        "SELECT COUNT(*) AS tx_count FROM transactions WHERE from_address = {literal}{time_and}",
        target="sends"), ALL_TIMES, 12),
    Template("daily_avg_gas_price", "plain", _one(
        {},
        ("[Giá gas](gas_price_paid) trung bình [mỗi ngày](day) {time} là bao nhiêu?",
         "Thống kê [giá gas](gas_price_paid) trung bình [theo ngày](day) {time}."),
        ("What was the average [gas price](gas_price_paid) [per day](day) {time}?",
         "Show the [daily](day) average [gas price](gas_price_paid) {time}."),
        "SELECT CAST(block_timestamp AS DATE) AS day, AVG(receipt_effective_gas_price) / 1e9 "
        "AS avg_gwei FROM transactions{time_where} GROUP BY CAST(block_timestamp AS DATE) "
        "ORDER BY day ASC NULLS LAST"), SPAN_TIMES, 12),
    Template("blob_tx_count", "plain", _one(
        {},
        ("[Số lượng](tx_count) [giao dịch blob](tx_blob) {time} là bao nhiêu?",
         "Có [bao nhiêu giao dịch](tx_count) loại [EIP-4844](tx_blob) {time}?"),
        ("[How many](tx_count) [blob transactions](tx_blob) were there {time}?",
         "What is the [number of transactions](tx_count) of type [EIP-4844](tx_blob) {time}?"),
        "SELECT COUNT(*) AS tx_count FROM transactions WHERE transaction_type = 3{time_and}"),
        ALL_TIMES, 12),
    Template("contract_creation_count", "plain", _one(
        {},
        ("Có [bao nhiêu giao dịch](tx_count) [tạo contract](contract_creation) "
         "[thành công](tx_success) {time}?",
         "[Số giao dịch](tx_count) [triển khai contract](contract_creation) "
         "[thành công](tx_success) {time} là bao nhiêu?"),
        ("[How many](tx_count) [successful](tx_success) [contract creation](contract_creation) "
         "transactions were there {time}?",
         "What is the [number of transactions](tx_count) that [deploy contract](contract_creation) "
         "code and were [successful](tx_success) {time}?"),
        "SELECT COUNT(*) AS tx_count FROM transactions WHERE to_address IS NULL "
        "AND receipt_status = 1{time_and}"), ALL_TIMES, 12),
    Template("top_block_producers", "plain", _one(
        {},
        ("5 [fee recipient](block_producer) nào có [số block](block_count) nhiều nhất {time}?",
         "Liệt kê 5 [người tạo block](block_producer) có [số block](block_count) lớn nhất "
         "{time}."),
        ("Which 5 [fee recipients](block_producer) produced the largest "
         "[number of blocks](block_count) {time}?",
         "List the top 5 [validators](block_producer) by [number of blocks](block_count) {time}."),
        "SELECT miner, COUNT(*) AS block_count FROM blocks{block_time_where} GROUP BY miner "
        "ORDER BY block_count DESC NULLS LAST, miner ASC NULLS LAST LIMIT 5"), ALL_TIMES, 12),
    # ---- G. NIL: the name is not in the reviewed knowledge base ----
    Template("nil_eth_received", "nil", _one(
        {"target": "nil_entity"},
        ("{target} [nhận](received_by) [bao nhiêu ETH](eth_amount) {time}?",),
        ("[How much ETH](eth_amount) did {target} [receive](received_by) {time}?",),
        None), ALL_TIMES, 8),
    Template("nil_call_count", "nil", _one(
        {"target": "nil_entity"},
        ("Có [bao nhiêu giao dịch](tx_count) [gọi](contract_called) {target} {time}?",),
        ("[How many transactions](tx_count) [called](contract_called) {target} {time}?",),
        None), ALL_TIMES, 8),
    Template("nil_token_transfer_count", "nil", _one(
        {"token": "nil_token"},
        ("Có [bao nhiêu lượt chuyển](transfer_count) token {token} {time}?",),
        ("What is the [number of transfers](transfer_count) of the {token} token {time}?",),
        None), ALL_TIMES, 8),
    # ---- H. not answerable from the data ----
    Template("unsupported_balance", "unsupported", _one(
        {"target": "address"},
        ("[Số dư](balance) của {target} là bao nhiêu?",
         "{target} còn [số dư](balance) bao nhiêu ETH?"),
        ("What is the [balance](balance) of {target}?",
         "How much ETH is left in the [balance](balance) of {target}?"),
        None), ("none",), 8, unsupported=True),
    Template("unsupported_usd_value", "unsupported", _one(
        {"target": "entity"},
        ("{target} [nhận](received_by) [bao nhiêu ETH](eth_amount) tính theo "
         "[giá trị USD](usd_value) {time}?",),
        ("What [USD value](usd_value) did {target} [receive](received_by) {time}?",),
        None), ALL_TIMES, 8, unsupported=True),
    Template("unsupported_internal_eth", "unsupported", _one(
        {"target": "entity"},
        ("{target} đã [chuyển đi](sent_by) [bao nhiêu ETH](eth_amount) qua "
         "[giao dịch nội bộ](internal_eth) {time}?",),
        ("[How much ETH](eth_amount) did {target} pay out through "
         "[internal transactions](internal_eth) {time}?",),
        None), ALL_TIMES, 8, unsupported=True),
)  # fmt: skip

TEMPLATES_BY_ID = {t.template_id: t for t in TEMPLATES}
GROUPS = ("entity", "address", "token", "class", "ambiguous", "plain", "nil", "unsupported")
