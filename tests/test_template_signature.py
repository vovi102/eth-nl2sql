"""Template signatures and the leakage checks R1-R3, R5 of phase 2 (docs/phase2-plan.md §3)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from eth_nl2sql.benchmark_templates import TEMPLATES, TEMPLATES_BY_ID, Template, Variant
from eth_nl2sql.template_signature import (
    canonical_sql,
    check,
    frame,
    load_manifest,
    report,
    surface_senses,
    wording,
)
from eth_nl2sql.train_templates import TRAIN_TEMPLATES

MANIFEST = Path(__file__).parent.parent / "data/benchmark/manifest.json"


def variant_of(template_id: str, i: int = 0) -> Variant:
    return TEMPLATES_BY_ID[template_id].variants[i]


def train(template_id: str, variant: Variant) -> Template:
    return Template(template_id, "entity", (variant,), ("month",), 2)


def kinds(findings: list, other: str) -> set[str]:
    return {f.kind for f in findings if f.other == other}


def respelled(template_id: str, sql: str, **slots: str) -> Variant:
    """The test template's variant with other SQL and other words, same senses."""
    v = variant_of(template_id)
    return replace(v, sql=sql, slots=slots or v.slots,
                   surfaces={"vi": (v.surfaces["vi"][0] + " Trả lời ngắn gọn.",)})  # fmt: skip


# ---- canonical SQL ------------------------------------------------------------------------

CALLS = (
    "SELECT COUNT(*) AS tx_count FROM transactions WHERE to_address IN (ph_addresses) AND ph_time"
)
ETH_IN = (
    "SELECT SUM(value) / 1e18 AS eth_received FROM transactions "
    "WHERE to_address IN (ph_addresses) AND receipt_status = 1 AND ph_time"
)
TOP = (
    "SELECT from_address, COUNT(*) AS tx_count FROM transactions WHERE to_address IN "
    "(ph_addresses) AND ph_time GROUP BY from_address "
    "ORDER BY tx_count DESC NULLS LAST, from_address ASC NULLS LAST LIMIT 5"
)
DAILY = (
    "SELECT CAST(block_timestamp AS DATE) AS day, COUNT(*) AS n FROM transactions "
    "WHERE ph_time GROUP BY CAST(block_timestamp AS DATE) ORDER BY day ASC NULLS LAST"
)


@pytest.mark.parametrize(
    ("original", "same"),
    [
        (CALLS, CALLS.replace("COUNT(*) AS tx_count", "COUNT(1)")),
        (CALLS, "SELECT COUNT(*) FROM transactions AS t WHERE (t.to_address IN (ph_addresses))"),
        (CALLS, CALLS.replace("IN (ph_addresses)", "= (ph_addresses)")),
        (CALLS, CALLS.replace(" AND ph_time", "")),  # a question without time
        (ETH_IN, ETH_IN.replace("SUM(value) / 1e18", "SUM(value / 1e18)")),
        (ETH_IN, "SELECT SUM(value) / 1e18 FROM transactions WHERE ph_time AND receipt_status = 1 "
                 "AND to_address IN (ph_addresses)"),
        (TOP, TOP.replace("ORDER BY tx_count", "ORDER BY COUNT(*)")),
        (TOP, TOP.replace("GROUP BY from_address", "GROUP BY 1")),
        (TOP, "SELECT COUNT(*), from_address FROM transactions WHERE to_address IN (ph_addresses) "
              "GROUP BY from_address ORDER BY 1 DESC, from_address LIMIT 5"),
        (DAILY, DAILY.replace("CAST(block_timestamp AS DATE)",
                              "date_trunc('day', block_timestamp)")),
        (DAILY, DAILY.replace("GROUP BY CAST(block_timestamp AS DATE)", "GROUP BY day")),
    ],
)  # fmt: skip
def test_spellings_ex_cannot_tell_apart_are_one_frame(original: str, same: str) -> None:
    assert canonical_sql(original) == canonical_sql(same)


def test_canonical_sql_keeps_what_matters() -> None:
    assert canonical_sql(CALLS) != canonical_sql(CALLS.replace("to_address", "from_address"))
    assert canonical_sql(ETH_IN) != canonical_sql(ETH_IN.replace("= 1", "= 0"))
    # a result named after a source column: the column stays a column
    a = "SELECT hash, value / 1e18 AS value FROM transactions ORDER BY value DESC LIMIT 1"
    b = "SELECT hash, gas / 1e18 AS gas FROM transactions ORDER BY gas DESC LIMIT 1"
    assert canonical_sql(a) != canonical_sql(b)
    assert "miner" in canonical_sql("SELECT miner AS miner, COUNT(*) FROM blocks GROUP BY miner")
    # GROUP BY binds a column before an alias of the same name (DuckDB)
    grouped = "SELECT value % 20 AS value, COUNT(*) FROM transactions GROUP BY value"
    assert "GROUP BY value" in canonical_sql(grouped)


def test_frame_of_a_variant() -> None:
    a = Variant({"target": "entity"}, {}, "SELECT COUNT(*) AS n FROM transactions "
                "WHERE to_address IN {target}{time_and} ORDER BY n DESC NULLS LAST")  # fmt: skip
    assert frame(a) == frame(replace(a, sql=a.sql.replace(" n", " tx_count")))
    typed = Variant({"target": "literal"}, {}, a.sql.replace("IN {target}", "= {literal}"))
    assert frame(typed) == frame(a)  # a typed address is one more address set


def test_surface_senses_add_the_implicit_ones() -> None:
    v = variant_of("entity_eth_received")
    assert surface_senses(v.surfaces["vi"][0], v) == {"received_by", "eth_amount", "tx_success"}
    v = variant_of("token_transfer_count")
    assert surface_senses(v.surfaces["vi"][0], v) == {"transfer_count", "of_token"}


def test_wording_drops_slots_markup_and_accents() -> None:
    surface = "{target} [nhận](received_by) [bao nhiêu ETH](eth_amount) {time}?"
    assert wording(surface) == ("nhan", "bao", "nhieu", "eth")


# ---- violations ---------------------------------------------------------------------------


def test_a_reworded_copy_is_a_violation() -> None:
    """New words do not hide a test template: the signature is the same."""
    v = variant_of("entity_call_count")
    copy = replace(v, surfaces={"vi": ("{target} được [gọi](contract_called) "
                                       "[bao nhiêu giao dịch](tx_count) {time}?",),
                                "en": ("{target} got [called](contract_called) in how many "
                                       "[transactions](tx_count) {time}?",)})  # fmt: skip
    findings = check([train("copy", copy)], TEMPLATES, {"entity_call_count": "test"})
    assert kinds(findings, "entity_call_count") == {"signature"}
    assert all(f.violation for f in findings)


def test_a_synonym_sense_does_not_hide_a_copy() -> None:
    """received_by and contract_called are both to_address IN {E} on transactions."""
    v = variant_of("entity_call_count")
    copy = replace(v, surfaces={"vi": ("Đếm [số giao dịch](tx_count) mà {target} đã "
                                       "[nhận](received_by) {time}.",)})  # fmt: skip
    findings = check([train("synonym", copy)], TEMPLATES, {"entity_call_count": "test"})
    assert "signature" in kinds(findings, "entity_call_count")


@pytest.mark.parametrize(
    ("other", "sql"),
    [
        ("entity_call_count", "SELECT COUNT(1) FROM transactions WHERE {time_where_free}"),
        ("entity_eth_received", "SELECT SUM(value / 1e18) FROM transactions WHERE "
                                "receipt_status = 1 AND to_address IN {target}{time_and}"),
        ("entity_top_callers", "SELECT COUNT(*) AS n, from_address FROM transactions WHERE "
                               "to_address IN {target}{time_and} GROUP BY 2 ORDER BY 1 DESC, "
                               "from_address ASC LIMIT 5"),
    ],
)  # fmt: skip
def test_a_respelled_query_is_a_violation(other: str, sql: str) -> None:
    sql = sql.replace("{time_where_free}", "to_address IN {target}{time_and}")
    findings = check([train("respelled", respelled(other, sql))], TEMPLATES, {other: "test"})
    assert "signature" in kinds(findings, other)


def test_the_same_evidence_for_another_query_is_a_violation() -> None:
    """token_avg_transfer (plan §2.1): an average where the test template sums."""
    v = variant_of("token_amount_total")
    avg = replace(
        v,
        sql=v.sql.replace("SUM(", "AVG("),
        surfaces={"vi": ("[Lượng](token_amount) {token} trung bình mỗi lượt {time}?",)},
    )
    findings = check([train("avg", avg)], TEMPLATES, {"token_amount_total": "test"})
    assert "same_evidence" in kinds(findings, "token_amount_total")
    assert any(f.violation for f in findings)


def test_a_benchmark_id_is_a_violation() -> None:
    v = variant_of("entity_fees_paid")
    findings = check([train("entity_fees_paid", v)], TEMPLATES, {"entity_fees_paid": "dev"})
    assert {"template_id", "signature", "same_wording"} <= {f.kind for f in findings}


def test_wording_close_to_test_is_a_violation_close_to_dev_is_listed() -> None:
    v = variant_of("token_transfer_count")
    other = replace(v, sql=v.sql.replace("COUNT(*)", "COUNT(DISTINCT to_address)"),
                    surfaces={"vi": ("Có bao nhiêu lượt chuyển {token} {time} vậy?",)})  # fmt: skip
    test = check([train("close", other)], TEMPLATES, {"token_transfer_count": "test"})
    assert "similar_wording" in kinds(test, "token_transfer_count")
    assert any(f.violation for f in test)
    dev = check([train("close", other)], TEMPLATES, {"token_transfer_count": "dev"})
    assert not any(f.violation for f in dev if f.kind == "similar_wording")


# ---- near ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("other", "split", "change", "kind"),
    [
        # sent instead of received: the direction mirror
        ("entity_token_received", "test",
         lambda v: replace(v, sql=v.sql.replace("to_address", "from_address"), surfaces={
             "vi": ("{target} [gửi](sent_by) [bao nhiêu](token_amount) {token} {time}?",)}),
         "direction"),
        # ... also for another kind of address set
        ("entity_failed_calls", "test",
         lambda v: replace(v, slots={"target": "address"},
                           sql=v.sql.replace("to_address", "from_address"), surfaces={
             "vi": ("{target} có [số giao dịch](tx_count) [gửi](sent_by) đi bị [lỗi](tx_failed) "
                    "{time} là bao nhiêu?",)}),
         "direction"),
        # successful instead of failed calls: the status mirror
        ("entity_failed_calls", "test",
         lambda v: replace(v, sql=v.sql.replace("= 0", "= 1"), surfaces={
             "vi": ("[Số giao dịch](tx_count) [thành công](tx_success) [gọi](contract_called) "
                    "{target} {time}?",)}),
         "status"),
        # per hour instead of per day: the time grain
        ("entity_daily_calls", "test",
         lambda v: replace(v, sql=v.sql.replace(
             "CAST(block_timestamp AS DATE)", "date_trunc('hour', block_timestamp)"), surfaces={
             "vi": ("Liệt kê [theo giờ](hour) {time} [số giao dịch](tx_count) đã "
                    "[gọi](contract_called) {target}.",)}),
         "grain"),
        # another transaction type
        ("blob_tx_count", "test",
         lambda v: replace(v, sql=v.sql.replace("= 3", "= 2"), surfaces={
             "vi": ("[EIP-1559](tx_eip1559) chiếm [bao nhiêu giao dịch](tx_count) {time}?",)}),
         "type"),
        # a class instead of an entity, same senses and SQL
        ("entity_fees_paid", "dev",
         lambda v: replace(v, slots={"target": "class"}, surfaces={
             "vi": ("Các giao dịch [gọi](contract_called) {target} trả bao nhiêu "
                    "[phí](tx_fee) {time}?",)}),
         "mention"),
        # the same question per day
        ("entity_fees_paid", "dev",
         lambda v: replace(v, sql=v.sql.replace("SELECT ", "SELECT CAST(block_timestamp AS DATE) "
             "AS day, ") + " GROUP BY CAST(block_timestamp AS DATE) ORDER BY day ASC NULLS LAST",
             surfaces={"vi": ("Người [gọi](contract_called) {target} trả bao nhiêu "
                              "[phí giao dịch](tx_fee) [mỗi ngày](day) {time}?",)}),
         "dimension"),
    ],
)  # fmt: skip
def test_mirrors_are_listed_not_forbidden(other, split, change, kind) -> None:
    variant = change(variant_of(other))
    findings = check([train("mirror", variant)], TEMPLATES, {other: split})
    assert kind in kinds(findings, other)
    assert not any(f.violation for f in findings)


def test_a_per_day_total_is_not_near_an_argmax() -> None:
    """dimension compares frames: daily amounts are no 'per day' of the largest transfer."""
    daily = next(t for t in TRAIN_TEMPLATES if t.template_id == "token_daily_amount")
    findings = check([daily], TEMPLATES, {"token_largest_transfer": "test",
                                          "token_amount_total": "test"})  # fmt: skip
    assert "dimension" in kinds(findings, "token_amount_total")
    assert "dimension" not in kinds(findings, "token_largest_transfer")


# ---- the catalog --------------------------------------------------------------------------


@pytest.mark.skipif(not MANIFEST.is_file(), reason="needs data/benchmark/manifest.json")
def test_the_train_catalog_has_no_violation() -> None:
    manifest = load_manifest(MANIFEST)
    splits = {tid: info["split"] for tid, info in manifest["templates"].items()}
    assert set(splits.values()) == {"dev", "test"}
    rep = report(TRAIN_TEMPLATES, TEMPLATES, splits)
    assert rep["violations"] == []
    assert set(rep["templates"]) == {t.template_id for t in TRAIN_TEMPLATES}
    answerable = {t.template_id for t in TEMPLATES if any(v.sql for v in t.variants)}
    assert set(rep["held_out"]) == answerable & set(splits)
