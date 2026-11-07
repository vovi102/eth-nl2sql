"""Template signatures and the leakage checks of phase 2 (docs/phase2-plan.md §3: R1-R3, R5).

The signature of a surface is what the SQL model is given and must write for it:

- the property senses the surface says, plus the implicit ones (benchmark_templates.implicit_
  senses). A sense is identified by the SQL it means on the query's table (property_senses.csv),
  so "nhận" (received_by) and "gọi" (contract_called) on transactions, both `to_address IN {E}`,
  are one sense;
- the kinds of its mentions;
- the SQL frame of its variant in canonical form: placeholders and the time condition made
  uniform, result names and the order of select items, conjuncts and GROUP BY keys dropped,
  SUM(x / k) written SUM(x) / k, COUNT(1) written COUNT(*), date_trunc('day', t) written
  CAST(t AS DATE), ordinals and alias references resolved the way DuckDB binds them.

Wording is not part of it, so a train surface with the signature of a dev or test surface is
that template reworded, which decision 14 forbids. Signatures are computed from the code of the
templates and the split in data/benchmark/manifest.json; no question file is read.

    uv run python -m eth_nl2sql.template_signature [--report PATH]

Checks, train against dev and test:

- violations (exit status 1): a benchmark template id (R1); the same signature (R2); the same
  mentions with the same senses (the same evidence, another query) or with the same frame (the
  same query, other words for it); a surface with the same words, or with wording at least
  SIMILAR_WORDING alike to a test surface (R3);
- near (R5, listed for review; entity, wallet, class and typed address count as one kind of
  address set): another kind of address set (mention); the other direction (to/from), status
  (= 1 / = 0) or time grain (day/hour); another transaction type; the same question per day or
  per hour of a total, or the reverse (dimension); wording at least SIMILAR_WORDING alike to a
  dev surface.
"""

from __future__ import annotations

import argparse
import difflib
import functools
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp

from eth_nl2sql import property_senses
from eth_nl2sql.benchmark_templates import SURFACE_RE, TEMPLATES, Template, Variant, implicit_senses
from eth_nl2sql.bigquery_schema import column_descriptions
from eth_nl2sql.entity_knowledge_base import normalize_alias
from eth_nl2sql.train_templates import TRAIN_TEMPLATES

DEFAULT_MANIFEST = Path("data/benchmark/manifest.json")
DEFAULT_SENSES_DIR = Path("data/schema")
DEFAULT_SCHEMA_FILE = Path("data/schema/bigquery_crypto_ethereum.json")
# Share of words in common (difflib ratio): near for a dev surface, a violation for a test one.
SIMILAR_WORDING = 0.8
MENTION_TYPES = {
    "entity": "entity", "address": "address", "class": "class", "token": "token",
    "literal": "literal_address", "nil_entity": "nil", "nil_token": "nil",
}  # fmt: skip
ADDRESS_SETS = {"entity", "address", "class", "literal_address"}
PLACEHOLDERS = ("ph_addresses", "ph_token")
DAY = "CAST(block_timestamp AS DATE)"
HOUR = "DATE_TRUNC('HOUR', block_timestamp)"
TIME_SENSES = {"day", "hour"}
VIOLATIONS = ("template_id", "signature", "same_evidence", "same_sql", "same_wording")
NEAR = ("mention", "direction", "status", "grain", "type", "dimension", "similar_wording")
STRUCTURAL = ("mention", "direction", "status", "grain", "type", "dimension")


def _swapper(pairs: dict[str, str]) -> Callable[[str], str]:
    """Swap every key for its value in one pass; keys are whole words."""
    mapping = pairs | {v: k for k, v in pairs.items()}
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w'])(?:" + "|".join(re.escape(k) for k in keys) + r")(?![\w'])")
    return lambda text: pattern.sub(lambda m: mapping[m.group(0)], text)


# The same template in a mirror: a rewrite of canonical SQL (frame and sense keys), and of the
# ids of senses that do not apply to the query's table.
MIRRORS = {
    "direction": (
        _swapper({"to_address": "from_address"}),
        {"received_by": "sent_by", "sent_by": "received_by",
         "distinct_senders": "distinct_receivers", "distinct_receivers": "distinct_senders"},
    ),
    "status": (
        _swapper({"receipt_status = 1": "receipt_status = 0"}),
        {"tx_success": "tx_failed", "tx_failed": "tx_success"},
    ),
    "grain": (_swapper({DAY: HOUR}), {"day": "hour", "hour": "day"}),
}  # fmt: skip


def _any_type(text: str) -> str:
    return re.sub(r"\btransaction_type = \d\b", "transaction_type = ph_type", text)


# ---- canonical SQL ------------------------------------------------------------------------


@functools.cache
def table_columns(schema_file: Path = DEFAULT_SCHEMA_FILE) -> dict[str, frozenset[str]]:
    columns: dict[str, set[str]] = {}
    for table, column in column_descriptions(schema_file):
        columns.setdefault(table, set()).add(column)
    return {t: frozenset(c) for t, c in columns.items()}


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _constant(node: exp.Expression) -> bool:
    node = _unparen(node)
    if isinstance(node, exp.Literal):
        return not node.is_string
    return isinstance(node, exp.Column) and node.name == "ph_scale"


def _placeholder(node: exp.Expression) -> bool:
    node = _unparen(node)
    return isinstance(node, exp.Column) and node.name in PLACEHOLDERS


def _rewrite(node: exp.Expression) -> exp.Expression:
    """Spellings EX treats alike (execution_evaluation.py) become one."""
    if isinstance(node, exp.Count) and isinstance(node.this, exp.Literal):
        return exp.Count(this=exp.Star())
    if isinstance(node, (exp.Sum, exp.Avg)) and isinstance(_unparen(node.this), exp.Div):
        div = _unparen(node.this)
        if _constant(div.expression):  # SUM(x / k) = SUM(x) / k
            aggregate = node.__class__(this=div.this.copy())
            return exp.Div(this=aggregate, expression=div.expression.copy())
    if isinstance(node, exp.TimestampTrunc) and node.unit and node.unit.name.upper() == "DAY":
        return exp.Cast(this=node.this.copy(), to=exp.DataType.build("DATE"))
    if isinstance(node, exp.EQ) and _placeholder(node.expression):  # = {E1} is IN {E1}
        return exp.In(this=node.this.copy(), expressions=[_unparen(node.expression).copy()])
    return node


def _sql(node: exp.Expression) -> str:
    return node.sql(dialect="duckdb", normalize=True)


def _connector_parts(node: exp.Expression, kind: type) -> list[exp.Expression]:
    node = _unparen(node)
    if isinstance(node, kind):
        return _connector_parts(node.this, kind) + _connector_parts(node.expression, kind)
    return [node]


def _canonical_condition(node: exp.Expression) -> exp.Expression | None:
    """Conjuncts and disjuncts sorted, the time condition dropped (decision 15 allows any)."""
    parts = []
    for part in _connector_parts(node, exp.And):
        if isinstance(part, exp.Or):
            part = exp.or_(*sorted(_connector_parts(part, exp.Or), key=_sql))
        if not (isinstance(part, exp.Column) and part.name == "ph_time"):
            parts.append(part)
    parts.sort(key=_sql)
    return exp.and_(*parts) if parts else None


def canonical_sql(sql: str) -> str:
    """One spelling per query, as far as EX cannot tell two spellings apart."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    table = tree.find(exp.Table)
    columns = table_columns().get(table.name, frozenset()) if table is not None else frozenset()
    if table is not None:
        table.set("alias", None)
    for column in tree.find_all(exp.Column):
        column.set("table", None)  # one table per query: a qualifier says nothing
    tree = tree.transform(_rewrite)
    select = list(tree.expressions)
    named = {e.alias: e.this for e in select if isinstance(e, exp.Alias)}

    def resolve(node: exp.Expression, alias_first: bool) -> exp.Expression:
        """An ordinal or an output name, as DuckDB binds it: ORDER BY looks at the select
        list first, GROUP BY and HAVING at the table's columns first."""
        if isinstance(node, exp.Literal) and node.is_int:
            item = select[int(node.this) - 1]
            return (item.this if isinstance(item, exp.Alias) else item).copy()
        if isinstance(node, exp.Column) and node.name in named:
            if alias_first or node.name not in columns:
                return named[node.name].copy()
        return node.copy()

    if tree.args.get("order"):
        keys = []
        for o in tree.args["order"].expressions:
            keys.append(exp.Ordered(this=resolve(o.this, alias_first=True),
                                    desc=bool(o.args.get("desc")),
                                    nulls_first=bool(o.args.get("nulls_first"))))  # fmt: skip
        tree.set("order", exp.Order(expressions=keys))
    if tree.args.get("group"):
        keys = [resolve(g, alias_first=False) for g in tree.args["group"].expressions]
        tree.set("group", exp.Group(expressions=sorted(keys, key=_sql)))
    if tree.args.get("having"):
        tree.set("having", exp.Having(this=resolve(tree.args["having"].this, alias_first=False)))
    items = [(e.this if isinstance(e, exp.Alias) else e).copy() for e in select]
    tree.set("expressions", sorted(items, key=_sql))  # EX ignores result names and their order
    if tree.args.get("where"):
        condition = _canonical_condition(tree.args["where"].this)
        tree.set("where", exp.Where(this=condition) if condition is not None else None)
    return _sql(tree)


def without_time_bucket(sql: str) -> str | None:
    """The canonical query with its day or hour bucket removed; None if it has none."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    bucket = {DAY, HOUR}
    if not any(_sql(e) in bucket for e in tree.expressions):
        return None
    tree.set("expressions", [e for e in tree.expressions if _sql(e) not in bucket])
    if tree.args.get("group"):
        keys = [g for g in tree.args["group"].expressions if _sql(g) not in bucket]
        tree.set("group", exp.Group(expressions=keys) if keys else None)
    if tree.args.get("order"):
        keys = [o for o in tree.args["order"].expressions if _sql(o.this) not in bucket]
        tree.set("order", exp.Order(expressions=keys) if keys else None)
    return canonical_sql(_sql(tree))


def frame_sql(variant: Variant) -> str:
    """The variant's SQL with every slot made uniform; a typed address is one more address set."""
    sql = re.sub(r"= \{literal\}", "IN {literal}", variant.sql)
    values = {
        slot: "(ph_token)" if kind == "token" else "(ph_addresses)"
        for slot, kind in variant.slots.items()
    }
    values |= {
        "literal": "(ph_addresses)",
        "scale": "ph_scale",
        "time_and": " AND ph_time",
        "time_where": " WHERE ph_time",
        "block_time_where": " WHERE ph_time",
    }
    return sql.format(**values)


def frame(variant: Variant) -> str:
    return canonical_sql(frame_sql(variant))


# ---- senses -------------------------------------------------------------------------------


@functools.cache
def sense_meanings(senses_dir: Path = DEFAULT_SENSES_DIR) -> dict[str, tuple[frozenset, str]]:
    """sense_id -> (tables, canonical SQL of its expression) for the approved senses."""
    out = {}
    for s in property_senses.usable(senses_dir).senses:
        if not s["expression"]:
            continue
        expression = s["expression"].replace("1e{decimals}", "ph_scale")
        expression = expression.replace("{E}", "(ph_addresses)")
        tree = _unparen(sqlglot.parse_one(expression, read="duckdb").transform(_rewrite))
        if isinstance(tree, (exp.And, exp.Or)):
            tree = _canonical_condition(tree)
        tables = frozenset(filter(None, s["table_name"].split("|")))
        out[s["sense_id"]] = (tables, _sql(tree))
    return out


def sense_key(sense_id: str, table: str) -> str:
    """What a sense means on a table: "table:SQL"; its id if it does not apply there."""
    tables, expression = sense_meanings().get(sense_id, (frozenset(), ""))
    return f"{table}:{expression}" if table in tables else sense_id


def surface_senses(surface: str, variant: Variant) -> frozenset[str]:
    """The senses a surface says and those the conventions add (benchmark.md §2, Nhãn)."""
    properties, mentions = [], []
    for m in SURFACE_RE.finditer(surface):
        if m.group(1) is not None:
            properties.append({"sense_id": m.group(2)})
        elif m.group(3) != "time":
            mentions.append({"type": MENTION_TYPES[variant.slots[m.group(3)]]})
    implicit = implicit_senses(properties, mentions, [])
    return frozenset(p["sense_id"] for p in properties + implicit)


def wording(surface: str) -> tuple[str, ...]:
    """The words of a surface without its slots: markup kept as text, accents and case gone."""
    text = SURFACE_RE.sub(lambda m: m.group(1) if m.group(1) is not None else " ", surface)
    return tuple(normalize_alias(text).split())


# ---- signatures ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signature:
    senses: frozenset[str]  # sense keys (sense_key)
    mentions: tuple[str, ...]  # mention types, sorted
    frame: str  # canonical SQL

    @property
    def family(self) -> tuple[str, ...]:
        return tuple(sorted("address_set" if m in ADDRESS_SETS else m for m in self.mentions))


def signature(senses: Iterable[str], kinds: tuple[str, ...], sql: str) -> Signature:
    """`senses` are sense ids; `sql` is a canonical frame."""
    table = sqlglot.parse_one(sql, read="duckdb").find(exp.Table).name
    return Signature(frozenset(sense_key(s, table) for s in senses), kinds, sql)


def mirror(sig: Signature, kind: str) -> Signature:
    rewrite, ids = MIRRORS[kind]
    senses = frozenset(rewrite(s) if ":" in s else ids.get(s, s) for s in sig.senses)
    return Signature(senses, sig.mentions, canonical_sql(rewrite(sig.frame)))


def typeless(sig: Signature) -> Signature:
    return Signature(
        frozenset(_any_type(s) for s in sig.senses), sig.mentions, _any_type(sig.frame)
    )


def _time_free(senses: frozenset[str]) -> frozenset[str]:
    times = {sense_key(s, t) for s in TIME_SENSES for t in ("transactions", "token_transfers")}
    return frozenset(s for s in senses if s not in times | TIME_SENSES)


@dataclass(frozen=True)
class Surface:
    template_id: str
    split: str  # train, dev or test
    variant: int
    lang: str
    surface: str  # with its markup
    sense_ids: frozenset[str]
    signature: Signature
    wording: tuple[str, ...]  # the words, without slots, normalized

    @functools.cached_property
    def mirrors(self) -> dict[str, Signature]:
        out = {kind: mirror(self.signature, kind) for kind in MIRRORS}
        return {kind: sig for kind, sig in out.items() if sig != self.signature}

    @functools.cached_property
    def bucketless(self) -> str | None:
        return without_time_bucket(self.signature.frame)


def surfaces(templates: Iterable[Template], splits: dict[str, str]) -> list[Surface]:
    """Every surface of the answerable variants of templates that have a split."""
    out = []
    for t in templates:
        split = splits.get(t.template_id)
        if split is None:
            continue
        for i, v in enumerate(t.variants):
            if v.sql is None:
                continue
            kinds = tuple(sorted(MENTION_TYPES[k] for k in v.slots.values()))
            sql = frame(v)
            for lang, texts in v.surfaces.items():
                for text in texts:
                    senses = surface_senses(text, v)
                    out.append(Surface(t.template_id, split, i, lang, text, senses,
                                       signature(senses, kinds, sql), wording(text)))  # fmt: skip
    return out


# ---- checks -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    kind: str  # one of VIOLATIONS or NEAR
    template_id: str  # the train template
    variant: int | None
    lang: str | None
    split: str  # of the other template: dev or test
    other: str  # the dev or test template
    detail: str = ""

    @property
    def violation(self) -> bool:
        return self.kind in VIOLATIONS or (self.kind == "similar_wording" and self.split == "test")


def compare(train: Surface, other: Surface) -> list[str]:
    """The kinds of VIOLATIONS and NEAR that tie a train surface to a dev or test one."""
    a, b = train.signature, other.signature
    kinds = []
    if a == b:
        kinds.append("signature")
    elif a.mentions == b.mentions and a.senses == b.senses:
        kinds.append("same_evidence")  # the same evidence, another query
    elif a.mentions == b.mentions and a.frame == b.frame:
        kinds.append("same_sql")  # the same query, other words for it
    elif a.family == b.family:
        if (a.senses, a.frame) == (b.senses, b.frame):
            kinds.append("mention")
        kinds += [
            kind
            for kind, mirrored in train.mirrors.items()
            if (mirrored.senses, mirrored.frame) == (b.senses, b.frame)
        ]
        ta, tb = typeless(a), typeless(b)
        if (ta.senses, ta.frame) == (tb.senses, tb.frame) != (a.senses, a.frame):
            kinds.append("type")  # only the transaction type differs
        if (
            (train.bucketless is None) != (other.bucketless is None)
            and _time_free(a.senses) == _time_free(b.senses)
            and (train.bucketless or a.frame) == (other.bucketless or b.frame)
        ):
            kinds.append("dimension")  # per day (or hour) of the same total
    if train.lang == other.lang:
        if train.wording == other.wording:
            kinds.append("same_wording")
        elif similarity(train.wording, other.wording) >= SIMILAR_WORDING:
            kinds.append("similar_wording")
    return kinds


def similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def check(
    train_templates: Iterable[Template], benchmark_templates: Iterable[Template],
    splits: dict[str, str],
) -> list[Finding]:  # fmt: skip
    """Findings of R1-R3 and R5 for the train templates against the dev and test ones.

    `splits` maps benchmark template ids to dev or test (manifest.json).
    """
    train_templates = list(train_templates)
    findings = [
        Finding("template_id", t.template_id, None, None, splits[t.template_id], t.template_id)
        for t in train_templates
        if t.template_id in splits
    ]
    held_out = surfaces(benchmark_templates, {k: v for k, v in splits.items() if v != "train"})
    train = surfaces(train_templates, {t.template_id: "train" for t in train_templates})
    seen = set()
    for s in train:
        for o in held_out:
            for kind in compare(s, o):
                key = (kind, s.template_id, s.variant, s.lang, o.split, o.template_id)
                if key not in seen:
                    seen.add(key)
                    detail = ""
                    if kind == "similar_wording":
                        detail = f"{similarity(s.wording, o.wording):.2f}"
                    findings.append(Finding(*key, detail))
    return findings


def closest_wording(train: Surface, held_out: list[Surface]) -> tuple[float, str | None]:
    """The most alike dev or test surface of the same language: (ratio, split:template)."""
    best = (0.0, None)
    for o in held_out:
        if o.lang == train.lang:
            ratio = similarity(train.wording, o.wording)
            if ratio > best[0]:
                best = (ratio, f"{o.split}:{o.template_id}")
    return best


def load_manifest(manifest: Path) -> dict:
    return json.loads(Path(manifest).read_text(encoding="utf-8"))


def load_splits(manifest: Path) -> dict[str, str]:
    return {tid: info["split"] for tid, info in load_manifest(manifest)["templates"].items()}


# ---- report -------------------------------------------------------------------------------


def report(
    train_templates: Iterable[Template], benchmark_templates: Iterable[Template],
    splits: dict[str, str], questions: dict[str, int] | None = None,
) -> dict:  # fmt: skip
    """Findings per train template, and the near/far buckets of the held-out templates (R5).

    `questions` (template id -> question count, from the manifest) weighs the buckets.
    """
    train_templates, benchmark_templates = list(train_templates), list(benchmark_templates)
    findings = check(train_templates, benchmark_templates, splits)
    held_out = surfaces(benchmark_templates, splits)
    train = surfaces(train_templates, {t.template_id: "train" for t in train_templates})
    templates = {}
    for t in train_templates:
        mine = [s for s in train if s.template_id == t.template_id]
        variants = []
        for i in range(len(t.variants)):
            senses = sorted({sid for s in mine if s.variant == i for sid in s.sense_ids})
            kinds = next(list(s.signature.mentions) for s in mine if s.variant == i)
            variants.append({"senses": senses, "mentions": kinds})
        ratio, other = max((closest_wording(s, held_out) for s in mine), key=lambda r: r[0])
        mine_findings = [f for f in findings if f.template_id == t.template_id]
        templates[t.template_id] = {
            "group": t.group,
            "count": t.count,
            "variants": variants,
            "near": sorted({f"{f.split}:{f.other}:{f.kind}" for f in mine_findings
                            if f.kind in STRUCTURAL}),
            "similar_wording": sorted({f"{f.split}:{f.other}:{f.detail}" for f in mine_findings
                                       if f.kind == "similar_wording"}),
            "closest_wording": {"ratio": round(ratio, 2), "template": other},
        }  # fmt: skip
    # R5 buckets: a held-out template is near when a train template mirrors it.
    buckets = {}
    for tid, split in sorted(splits.items()):
        if not any(s.template_id == tid for s in held_out):
            continue  # NIL and unsupported: the SQL model is never asked
        near = sorted({f"{f.template_id}:{f.kind}" for f in findings
                       if f.other == tid and f.kind in STRUCTURAL})  # fmt: skip
        buckets[tid] = {"split": split, "near": near, "questions": (questions or {}).get(tid)}
    # The benchmark's own split, for scale: dev checked against test the same way.
    dev = [t for t in benchmark_templates if splits.get(t.template_id) == "dev"]
    test_only = {k: v for k, v in splits.items() if v == "test"}
    baseline = check(dev, benchmark_templates, test_only)
    return {
        "violations": [f.__dict__ for f in findings if f.violation],
        "near": [f.__dict__ for f in findings if not f.violation],
        "templates": templates,
        "held_out": buckets,
        "benchmark_dev_vs_test": sorted(
            {f"{f.template_id}~{f.other}:{f.kind}" for f in baseline if f.kind != "template_id"}
        ),
    }


def format_report(rep: dict) -> str:
    lines = [f"{'template':<28} {'group':<9} {'n':>3}  closest wording       near (structural)"]
    for tid, info in rep["templates"].items():
        closest = f"{info['closest_wording']['ratio']:.2f} {info['closest_wording']['template']}"
        near = ", ".join(info["near"]) or "-"
        lines.append(f"{tid:<28} {info['group']:<9} {info['count']:>3}  {closest:<21} {near}")
    for split in ("dev", "test"):
        held = {t: b for t, b in rep["held_out"].items() if b["split"] == split}
        near = [t for t, b in held.items() if b["near"]]
        n_near = sum(held[t]["questions"] or 0 for t in near)
        n_all = sum(b["questions"] or 0 for b in held.values())
        far = sorted(set(held) - set(near))
        lines.append(
            f"{split}: {len(near)}/{len(held)} answerable templates near a train "
            f"template ({n_near}/{n_all} questions); far: {', '.join(far) or '-'}"
        )
    kinds: dict[str, int] = {}
    for pair in rep["benchmark_dev_vs_test"]:
        kind = pair.rsplit(":", 1)[1]
        kinds[kind] = kinds.get(kind, 0) + 1
    lines.append(f"benchmark itself, dev vs test pairs by kind: {kinds}")
    lines.append(f"violations: {len(rep['violations'])}")
    lines += [f"  {v['kind']}: {v['template_id']} ~ {v['split']}:{v['other']} {v['detail']}"
              for v in rep["violations"]]  # fmt: skip
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eth_nl2sql.template_signature")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, help="write the full report as JSON")
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    splits = {tid: info["split"] for tid, info in manifest["templates"].items()}
    questions = {tid: info["questions"] for tid, info in manifest["templates"].items()}
    rep = report(TRAIN_TEMPLATES, TEMPLATES, splits, questions)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(format_report(rep))
    return 1 if rep["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
