"""The train-only catalog of phase 2: counts, languages, filters, every surface's record, and
what the real phrase table makes of each surface."""

from __future__ import annotations

from collections import Counter
from itertools import product
from pathlib import Path

import duckdb
import pytest
from conftest import ROUTER, TOKEN
from test_benchmark_build import dummy_fill

from eth_nl2sql import entity_knowledge_base as knowledge_base
from eth_nl2sql import property_senses
from eth_nl2sql.benchmark_build import build_record
from eth_nl2sql.benchmark_templates import (
    SPAN_TIMES,
    TEMPLATES,
    TEMPLATES_BY_ID,
    Template,
    Variant,
    time_fill,
)
from eth_nl2sql.benchmark_validate import check_record, check_sql, substitute_placeholders
from eth_nl2sql.build_duckdb import load_knowledge_base
from eth_nl2sql.entity_linking import EntityLinker
from eth_nl2sql.entity_linking_resources import load_resources
from eth_nl2sql.prompt_builder import PromptBuilder, load_schema
from eth_nl2sql.template_signature import (
    MENTION_TYPES,
    Surface,
    compare,
    frame,
    load_splits,
    signature,
    surface_senses,
    surfaces,
    wording,
)
from eth_nl2sql.train_templates import DAY_TIMES, GROUP_QUESTIONS, TRAIN_GROUPS, TRAIN_TEMPLATES

REPO = Path(__file__).parent.parent
MANIFEST = REPO / "data/benchmark/manifest.json"
FILTERS = {"calls", "eth_in", "failed_calls", "sends", "eth_out", "token_in", "token_out"}


def senses_table() -> dict[str, str]:
    return {s["sense_id"]: s["kind"] for s in property_senses.usable(REPO / "data/schema").senses}


def test_counts_follow_the_group_shares() -> None:
    """2,000 questions in the benchmark's group shares, as many in Vietnamese as in English."""
    ids = [t.template_id for t in TRAIN_TEMPLATES]
    assert len(ids) == len(set(ids)) == 30
    assert not set(ids) & set(TEMPLATES_BY_ID)
    totals = Counter()
    for t in TRAIN_TEMPLATES:
        assert t.group in TRAIN_GROUPS and not t.unsupported
        assert t.count % 2 == 0, t.template_id
        totals[t.group] += t.count
    assert dict(totals) == GROUP_QUESTIONS
    assert sum(totals.values()) == 2000


def test_variants_speak_both_languages_with_one_sense_set() -> None:
    """Every surface of a variant says the same senses: a variant has one signature."""
    for t in TRAIN_TEMPLATES:
        for v in t.variants:
            assert set(v.surfaces) == {"vi", "en"} and all(v.surfaces.values()), t.template_id
            senses = {surface_senses(s, v) for texts in v.surfaces.values() for s in texts}
            assert len(senses) == 1, (t.template_id, senses)


def test_time_kinds_fit_the_grain() -> None:
    """Hourly questions cover one day; daily ones several."""
    for t in TRAIN_TEMPLATES:
        senses = set().union(*(surface_senses(s, v) for v in t.variants
                               for texts in v.surfaces.values() for s in texts))  # fmt: skip
        if "hour" in senses:
            assert t.times == DAY_TIMES, t.template_id
        elif "day" in senses:
            assert t.times == SPAN_TIMES, t.template_id


def test_filters_are_known_and_fit_the_slots() -> None:
    for t in TRAIN_TEMPLATES:
        for v in t.variants:
            assert set(v.filters) <= set(v.slots), t.template_id
            assert set(v.filters.values()) <= FILTERS, t.template_id
            if {"token_in", "token_out"} & set(v.filters.values()):
                assert "token" in v.slots.values(), t.template_id


def test_every_surface_gives_a_valid_record_and_bindable_sql(db: Path) -> None:
    """All surfaces, slots and time kinds of the train catalog, against the fixture's tables."""
    senses = senses_table()
    problems, n = [], 0
    with duckdb.connect(str(db), read_only=True) as con:
        for template in TRAIN_TEMPLATES:
            for variant in template.variants:
                for lang, texts in variant.surfaces.items():
                    for surface, kind in product(texts, template.times):
                        fills = {slot: dummy_fill(k, lang) for slot, k in variant.slots.items()}
                        fills["time"] = time_fill(kind, lang, 2, 4)
                        extra = {"scale": "1e6", "literal": f"'{ROUTER}'"}
                        r = build_record(template, variant, lang, surface, fills, extra, senses)
                        r["question_id"] = "t00001"
                        n += 1
                        errors = check_record(r, senses) + check_sql(r["gold_sql"])
                        names = {m["placeholder"] for m in r["mentions"] if "placeholder" in m}
                        sql = substitute_placeholders(r["gold_sql"], dict.fromkeys(names, [TOKEN]))
                        try:  # binds every table and column without reading the data
                            con.execute("EXPLAIN " + sql)
                        except duckdb.Error as e:
                            errors.append(str(e).splitlines()[0])
                        if errors:
                            problems.append((r["template_id"], r["question"], errors))
    assert n > 500
    assert problems == []


# ---- what the linker reads (the real phrase table on the fixture database) ---------------


@pytest.fixture
def real_phrases(db: Path, knowledge_base_dir: Path) -> tuple[EntityLinker, PromptBuilder]:
    """The fixture KB plus a named contract, with the real property senses and phrases."""
    path = knowledge_base_dir / knowledge_base.ALIASES.file
    columns = knowledge_base.ALIASES.columns
    rows = knowledge_base.read_sheet(path, columns)
    rows.append(dict.fromkeys(columns, "") | {
        "alias": "Swapper: Router", "alias_norm": knowledge_base.normalize_alias("Swapper: Router"),
        "lang": "any", "alias_type": "name", "target_type": "address", "target_id": ROUTER,
        "status": "auto",
    })  # fmt: skip
    knowledge_base.write_sheet(path, columns, rows)
    load_knowledge_base(db, knowledge_base_dir, REPO / "data/schema")
    linker = EntityLinker(load_resources(db, knowledge_base_dir))
    return linker, PromptBuilder(linker, load_schema(db))


def rendered(template: Template, variant: Variant, lang: str, surface: str) -> dict:
    """The surface filled with names the fixture KB knows."""
    fills = {slot: dummy_fill(k, lang) for slot, k in variant.slots.items()}
    kind = {DAY_TIMES: "day", SPAN_TIMES: "week"}.get(template.times, "month")
    fills["time"] = time_fill(kind, lang, 2, 4)
    extra = {"scale": "1e6", "literal": f"'{ROUTER}'"}
    return build_record(template, variant, lang, surface, fills, extra, senses_table())


def targets(record: dict) -> list[tuple]:
    return sorted((m["type"], m.get("target")) for m in record["mentions"])


def test_the_linker_reads_every_train_surface_as_marked(real_phrases) -> None:
    """No stray or missing sense, no unknown name, the gold mentions, no bare {E} line."""
    linker, builder = real_phrases
    problems = []
    for template in TRAIN_TEMPLATES:
        for variant in template.variants:
            for lang, texts in variant.surfaces.items():
                for surface in texts:
                    gold = rendered(template, variant, lang, surface)
                    linked = linker.link(gold["question"])
                    said = {p["sense_id"] for p in gold["properties"]}
                    read = {p["sense_id"] for p in linked["properties"]}
                    errors = []
                    if read != said:
                        errors.append(f"senses {sorted(read)} != {sorted(said)}")
                    if targets(linked) != targets(gold):
                        errors.append(f"mentions {targets(linked)} != {targets(gold)}")
                    prompt = builder.build(linked)
                    if prompt["decision"] != "sql":
                        errors.append(f"decision {prompt['decision']}")
                    elif "{E}" in "\n".join(builder.evidence(linked)):
                        errors.append("evidence keeps a bare {E}")
                    if errors:
                        problems.append((template.template_id, gold["question"], errors))
    assert problems == []


@pytest.mark.skipif(not MANIFEST.is_file(), reason="needs data/benchmark/manifest.json")
def test_no_train_surface_is_a_held_out_one_as_the_linker_reads_it(real_phrases) -> None:
    """R2 on the senses of the evidence: a dev or test surface whose markup differs from what
    the linker reads must not match a train surface through the linker's reading either."""
    linker, _ = real_phrases
    splits = load_splits(MANIFEST)
    train = surfaces(TRAIN_TEMPLATES, {t.template_id: "train" for t in TRAIN_TEMPLATES})
    problems = []
    for template in TEMPLATES:
        if template.template_id not in splits:
            continue
        for i, variant in enumerate(template.variants):
            if variant.sql is None:
                continue
            kinds = tuple(sorted(MENTION_TYPES[k] for k in variant.slots.values()))
            sql = frame(variant)
            for lang, texts in variant.surfaces.items():
                for text in texts:
                    linked = linker.link(rendered(template, variant, lang, text)["question"])
                    read = frozenset(p["sense_id"] for p in linked["properties"])
                    sig = signature(read, kinds, sql)
                    split = splits[template.template_id]
                    held = Surface(template.template_id, split, i, lang, text, read, sig,
                                   wording(text))  # fmt: skip
                    for s in train:
                        clash = set(compare(s, held)) & {"signature", "same_evidence", "same_sql"}
                        if clash:
                            problems.append((s.template_id, template.template_id, sorted(clash)))
    assert problems == []
