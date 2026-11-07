"""Build the phase-2 training data (docs/phase2-plan.md §2-3, step S2).

    uv run python -m eth_nl2sql.train_build [--count-scale 0.1] [--out-dir data/train]

1. Leakage check of the catalog (template_signature: R1-R3); any violation stops the build.
2. Fillers of the benchmark (benchmark_build.load_fillers), plus `token_out` (the tokens a
   target sent), without the held-out entities (decision 27): 20 entities drawn by a seed among
   those no train prompt has to name (not a top-5 member of a class, no parent or child in the
   pool, no name that is a common word), and every entity, named address or token whose name
   contains one of their names. A question whose prompt still names one is dropped.
3. Questions by benchmark_build.generate, variant by variant, with the benchmark's checks: the
   gold SQL runs fast with an informative result, the soft tx_success preference, no tie at the
   LIMIT. Questions equal to a dev question are dropped (R4).
4. The real linker and the L1 filter (decision 17): decision sql, the gold's placeholders, the
   gold rows from the linker's addresses; no evidence line keeps a bare {E}.
5. The prompt of generate-sql (PromptBuilder, default options) with the gold SQL as the answer
   (decision 19); 5% of every template kept apart to watch the loss (split val).

Writes to the output directory: train.jsonl (gold records kept), dropped.jsonl (with the
reason), sft.jsonl (chat examples, readable by count-prompt-tokens) and manifest.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path

import duckdb

from eth_nl2sql import benchmark_build as bb
from eth_nl2sql import benchmark_validate as validate
from eth_nl2sql import template_signature
from eth_nl2sql.benchmark_templates import TEMPLATES, Template, Variant
from eth_nl2sql.entity_knowledge_base import normalize_alias
from eth_nl2sql.entity_linking import STOPWORDS, EntityLinker
from eth_nl2sql.entity_linking_resources import load_resources
from eth_nl2sql.execution_evaluation import same_rows
from eth_nl2sql.prompt_builder import PromptBuilder, load_schema
from eth_nl2sql.train_templates import TRAIN_TEMPLATES

log = logging.getLogger(__name__)

SEED = 20261104
HOLDOUT_ENTITIES = 20  # decision 27
VAL_SHARE = 0.05
DEFAULT_OUT_DIR = Path("data/train")
DEFAULT_BENCHMARK_MANIFEST = Path("data/benchmark/manifest.json")
DEFAULT_DEV = Path("data/benchmark/dev.jsonl")
CATALOG = Path(__file__).with_name("train_templates.py")


# ---- fillers ------------------------------------------------------------------------------


def holdout_entities(entity_ids: list[str], n: int, seed: int) -> list[str]:
    """n entities drawn by a seed of their own, never used to fill a train question."""
    ids = sorted(entity_ids)
    return sorted(random.Random(f"{seed}:holdout").sample(ids, min(n, len(ids))))


def entity_names(link: bb.LinkTables, entity_id: str) -> set[str]:
    """Every loaded alias of an entity and its name, normalized: what a prompt could print."""
    names = {
        a["alias_norm"]
        for a in link.aliases
        if a["target_type"] == "entity" and a["target_id"] == entity_id
    }
    names.add(normalize_alias(link.entities[entity_id]["name"]))
    return {n for n in names if n}


def family(link: bb.LinkTables, entity_id: str) -> set[str]:
    """Ancestors and descendants of an entity."""
    out = set(bb.subtree(link, entity_id)) - {entity_id}
    parent = link.entities[entity_id]["parent"]
    while parent and parent not in out:
        out.add(parent)
        parent = link.entities.get(parent, {}).get("parent")
    return out


def holdout_candidates(fillers: bb.Fillers, link: bb.LinkTables) -> list[str]:
    """Entity fillers that a train prompt never has to name once they are held out: not a
    top-5 member of a class (the evidence lists members by name), no parent or child among the
    fillers, and no name that is a common word (the linker's stopwords: "Curve", "Base")."""
    pool = {e["entity_id"] for e in fillers.entities}
    members = {eid for ranked in fillers.classes.values() for eid, _ in ranked[: bb.DEFAULT_TOP_N]}
    return [
        eid
        for eid in sorted(pool)
        if eid not in members
        and not family(link, eid) & pool
        and not any(n in STOPWORDS for n in entity_names(link, eid))
    ]


def _padded(text: str | None) -> str:
    return " " + " ".join(normalize_alias(text or "").split()) + " "


def names_any(names: set[str], *texts: str | None) -> bool:
    """Does one of the texts contain one of the names, as whole words?"""
    padded = [_padded(t) for t in texts]
    return any(f" {n} " in p for n in names for p in padded)


def hold_out(fillers: bb.Fillers, link: bb.LinkTables, held: list[str]) -> tuple:
    """The fillers without the held-out entities and without any entity, named address or token
    whose name contains one of theirs. Returns (fillers, {kind: removed ids})."""
    names = set().union(*(entity_names(link, h) for h in held)) if held else set()
    owner = {a["address"]: eid for eid, rows in link.addresses.items() for a in rows}

    def owner_name(address: str) -> str | None:
        return link.entities.get(owner.get(address), {}).get("name")

    entities = [
        e for e in fillers.entities
        if e["entity_id"] not in held
        and not names_any(names, link.entities[e["entity_id"]]["name"],
                          *(alias for alias, _ in e["aliases"]))
    ]  # fmt: skip
    addresses = [
        a for a in fillers.addresses
        if owner.get(a["address"]) not in held
        and not names_any(names, a["alias"], owner_name(a["address"]))
    ]  # fmt: skip
    tokens = [
        t for t in fillers.tokens
        if not names_any(names, t["alias"], link.tokens[t["address"]]["name"],
                         link.tokens[t["address"]]["symbol"])
    ]  # fmt: skip
    removed = {
        "entities": sorted(
            {e["entity_id"] for e in fillers.entities} - {e["entity_id"] for e in entities}
        ),
        "addresses": sorted(
            {a["address"] for a in fillers.addresses} - {a["address"] for a in addresses}
        ),
        "tokens": sorted({t["address"] for t in fillers.tokens} - {t["address"] for t in tokens}),
    }
    return replace(fillers, entities=entities, addresses=addresses, tokens=tokens), removed


def token_sends(con: duckdb.DuckDBPyConnection, fillers: bb.Fillers) -> dict[str, set[str]]:
    """sender address -> filler tokens it sent in September, for the knowledge base addresses
    and typed-address candidates of load_fillers (its temp table benchmark_addresses)."""
    tokens = sorted({t["address"] for t in fillers.tokens})
    con.execute(
        "CREATE OR REPLACE TEMP TABLE train_tokens AS SELECT unnest(?::VARCHAR[]) AS address",
        [tokens],
    )
    sends: dict[str, set[str]] = defaultdict(set)
    for sender, token in con.execute(
        """SELECT DISTINCT from_address, token_address FROM main.token_transfers
        WHERE token_address IN (SELECT address FROM train_tokens)
          AND from_address IN (SELECT address FROM benchmark_addresses)"""
    ).fetchall():
        sends[sender].add(token)
    return dict(sends)


def shadow(variant: Variant) -> tuple[Variant, bool]:
    """The variant as benchmark_build fills it: `token_out` becomes `token_in`, to be read on
    fillers whose token_receipts are the tokens sent. Returns (variant, uses token_out)."""
    out = any(f == "token_out" for f in variant.filters.values())
    filters = {s: ("token_in" if f == "token_out" else f) for s, f in variant.filters.items()}
    return replace(variant, filters=filters), out


def variant_counts(count: int, n_variants: int) -> list[int]:
    """Questions per variant, as benchmark_build.generate cycles them (one VI/EN pair each)."""
    counts = [0] * n_variants
    for i in range(count):
        counts[i // 2 % n_variants] += 1
    return counts


# ---- linking, L1 and the example ----------------------------------------------------------


def placeholder_targets(record: dict) -> dict[str, tuple]:
    return {
        m["placeholder"]: (m["type"], m["target"], m.get("top_n"))
        for m in record["mentions"]
        if m.get("placeholder")
    }


def l1_reason(
    gold: dict, linked: dict, prompt: dict, evidence: str, link: bb.LinkTables,
    linker: EntityLinker, con: duckdb.DuckDBPyConnection, timeout: float,
) -> str | None:  # fmt: skip
    """Why the L1 filter drops a question (docs/phase2-plan.md §2.3); None to keep it."""
    if prompt["decision"] != "sql":
        return f"decision {prompt['decision']}"
    if placeholder_targets(linked) != placeholder_targets(gold):
        return "placeholders differ"
    if "{E}" in evidence:
        return "bare {E} in evidence"
    gold_addresses = bb.placeholder_addresses(link, gold)
    linked_addresses = linker.placeholder_addresses(linked)
    if gold_addresses == linked_addresses:
        return None  # the same query
    try:
        rows, _ = validate.run_sql(
            con, validate.substitute_placeholders(gold["gold_sql"], gold_addresses), timeout
        )
        mine, _ = validate.run_sql(
            con, validate.substitute_placeholders(gold["gold_sql"], linked_addresses), timeout
        )
    except validate.GoldQueryError:
        return "linker addresses: query failed"
    return None if same_rows(rows, mine) else "linker addresses: other rows"


def linked_reading(linked: dict, decision: str) -> dict:
    """What the linker made of a dropped question, to tell its errors apart."""
    keys = ("text", "type", "target", "placeholder", "top_n")
    return {
        "decision": decision,
        "mentions": [{k: m[k] for k in keys if k in m} for m in linked["mentions"]],
        "senses": sorted({p["sense_id"] for p in linked["properties"]}),
    }


def drop_cause(gold: dict, linked: dict) -> str | None:
    """The known linker errors of plan §2.3: a name read as the other kind (entity/token)."""
    for m in gold["mentions"]:
        for other in linked["mentions"]:
            if other["span"][0] < m["span"][1] and m["span"][0] < other["span"][1]:
                if m["type"] in ("entity", "address", "class") and other["type"] == "token":
                    return "entity read as token"
                if m["type"] == "token" and other["type"] in ("entity", "address", "class"):
                    return "token read as entity"
    return None


def example(record: dict, prompt: dict, group: str, split: str) -> dict:
    """One chat example: the generate-sql prompt and the gold SQL, placeholders kept."""
    answer = {"role": "assistant", "content": f"```sql\n{record['gold_sql']}\n```"}
    return {
        "question_id": record["question_id"],
        "template_id": record["template_id"],
        "lang": record["lang"],
        "group": group,
        "split": split,
        "decision": "sql",
        "messages": prompt["messages"] + [answer],
    }


# ---- build --------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(path: Path, rows: list[dict]) -> dict:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    return {"sha256": _sha256(path), "lines": len(rows)}


def build(
    db: Path, knowledge_base_dir: Path, property_senses_dir: Path, out_dir: Path, *,
    seed: int = SEED, count_scale: float = 1.0, timeout: float = bb.QUERY_TIMEOUT,
    memory_limit: str = "4GB", benchmark_manifest: Path = DEFAULT_BENCHMARK_MANIFEST,
    dev: Path = DEFAULT_DEV, holdout: int = HOLDOUT_ENTITIES, val_share: float = VAL_SHARE,
    templates: tuple[Template, ...] = TRAIN_TEMPLATES,
) -> dict:  # fmt: skip
    # 1. leakage of the catalog: the build stops on any violation
    benchmark = template_signature.load_manifest(benchmark_manifest)
    splits = {tid: info["split"] for tid, info in benchmark["templates"].items()}
    questions = {tid: info["questions"] for tid, info in benchmark["templates"].items()}
    leakage = template_signature.report(templates, TEMPLATES, splits, questions)
    if leakage["violations"]:
        raise ValueError(f"leakage check: {leakage['violations']}")

    # the linker and the schema open their own connections first (one DuckDB configuration)
    linker = EntityLinker(load_resources(db, knowledge_base_dir, memory_limit=memory_limit))
    builder = PromptBuilder(linker, load_schema(db))
    con = duckdb.connect(str(db), read_only=True)
    con.execute(f"SET memory_limit = '{memory_limit}'")

    # 2. fillers
    files = bb.knowledge_base_files(knowledge_base_dir, property_senses_dir)
    version = bb.knowledge_base_version(files)
    link = bb.load_link_tables(con)
    fillers = bb.load_fillers(con, link, knowledge_base_dir)
    candidates = holdout_candidates(fillers, link)
    held = holdout_entities(candidates, holdout, seed)
    held_names = set().union(*(entity_names(link, h) for h in held)) if held else set()
    fillers, removed = hold_out(fillers, link, held)
    sending = replace(fillers, token_receipts=token_sends(con, fillers))
    log.info(
        "fillers: %d entities (%d held out), %d named addresses, %d tokens",
        len(fillers.entities), len(held), len({a["address"] for a in fillers.addresses}),
        len({t["address"] for t in fillers.tokens}),
    )  # fmt: skip
    dev_questions = {
        normalize_alias(json.loads(line)["question"])
        for line in Path(dev).read_text(encoding="utf-8").splitlines()
    }

    rng = random.Random(seed)
    kept, dropped, examples = [], [], []
    seen: set[str] = set()
    stats: dict[str, dict] = {}
    n = 0
    for template in templates:
        count = max(2, round(template.count * count_scale / 2) * 2)
        rejected: Counter = Counter()
        drops: Counter = Counter()
        causes: Counter = Counter()
        records = []
        for variant, n_variant in zip(
            template.variants, variant_counts(count, len(template.variants)), strict=True
        ):
            if not n_variant:
                continue
            filled, out = shadow(variant)
            single = replace(template, variants=(filled,))
            made = bb.generate(single, sending if out else fillers, link, con, rng, n_variant,
                               timeout, rejected, seed=seed)  # fmt: skip
            if out and rejected["no token received by this target"]:
                rejected["no token sent by this target"] += rejected.pop(
                    "no token received by this target"
                )
            records += made
        mismatched = 0
        mine = []
        for r in records:
            n += 1
            r["question_id"] = f"t{n:05d}"
            r["knowledge_base_version"] = version
            question = normalize_alias(r["question"])
            if question in dev_questions:
                reason = "same as a dev question"
            elif question in seen:
                reason = "same as another train question"
            else:
                seen.add(question)
                linked = linker.link(r["question"], r["question_id"])
                prompt = builder.build(linked)
                lines = builder.evidence(linked) if prompt["decision"] == "sql" else []
                evidence = "\n".join(lines)
                reason = l1_reason(r, linked, prompt, evidence, link, linker, con, timeout)
                if not reason and names_any(held_names, evidence, r["question"]):
                    reason = "names a held-out entity"  # a route hold_out did not close
            if reason:
                drops[reason] += 1
                row = r | {"dropped": reason}
                if reason not in ("same as a dev question", "same as another train question"):
                    row["linked"] = linked_reading(linked, prompt["decision"])
                    cause = drop_cause(r, linked) if reason == "placeholders differ" else None
                    if cause:
                        row["cause"] = cause
                        causes[cause] += 1
                dropped.append(row)
                continue
            senses = {p["sense_id"] for p in r["properties"]}
            r["linked_senses_match"] = senses == {p["sense_id"] for p in linked["properties"]}
            mismatched += not r["linked_senses_match"]
            mine.append((r, prompt))
        k = round(len(mine) * val_share)
        val = set(random.Random(f"{seed}:val:{template.template_id}").sample(
            [r["question_id"] for r, _ in mine], k))  # fmt: skip
        for r, prompt in mine:
            r["split"] = "val" if r["question_id"] in val else "train"
            kept.append(r)
            examples.append(example(r, prompt, template.group, r["split"]))
        stats[template.template_id] = {
            "group": template.group,
            "target": count,
            "generated": len(records),
            "rejected": dict(rejected),
            "dropped": dict(drops),
            "known_losses": dict(causes),
            "kept": len(mine),
            "val": k,
            "senses_mismatch": mismatched,
        }
        log.info("%-28s generated %3d/%-3d kept %3d dropped %s", template.template_id,
                 len(records), count, len(mine), dict(drops))  # fmt: skip
    con.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    chars = sorted(sum(len(m["content"]) for m in e["messages"]) for e in examples)
    out_files = {
        "train.jsonl": _write_jsonl(out_dir / "train.jsonl", kept),
        "dropped.jsonl": _write_jsonl(out_dir / "dropped.jsonl", dropped),
        "sft.jsonl": _write_jsonl(out_dir / "sft.jsonl", examples),
    }
    totals = Counter()
    for s in stats.values():
        totals.update(target=s["target"], generated=s["generated"], kept=s["kept"],
                      val=s["val"], senses_mismatch=s["senses_mismatch"])  # fmt: skip
    manifest = {
        "created": date.today().isoformat(),
        "seed": seed,
        "count_scale": count_scale,
        "timeout": timeout,
        "memory_limit": memory_limit,
        "val_share": val_share,
        "knowledge_base_version": version,
        "knowledge_base_files": files,
        "benchmark_manifest_sha256": _sha256(Path(benchmark_manifest)),
        "catalog_sha256": _sha256(CATALOG),
        "holdout_entities": held,
        "holdout": {
            "candidates": candidates,
            "names": sorted(held_names),
            "removed": removed,
        },
        "leakage": {
            "violations": len(leakage["violations"]),
            "near": {tid: info["near"] for tid, info in leakage["templates"].items()},
            "similar_wording": {
                tid: info["similar_wording"] for tid, info in leakage["templates"].items()
            },
            "held_out": leakage["held_out"],
        },
        "totals": dict(totals),
        "by_lang": dict(Counter(f"{r['split']}/{r['lang']}" for r in kept)),
        "by_group": dict(Counter(stats[r["template_id"]]["group"] for r in kept)),
        "dropped": dict(Counter(r["dropped"] for r in dropped)),
        "known_losses": dict(Counter(r["cause"] for r in dropped if "cause" in r)),
        "example_chars": (
            {"min": chars[0], "median": statistics.median(chars), "max": chars[-1]} if chars else {}
        ),
        "templates": stats,
        "files": out_files,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eth_nl2sql.train_build")
    parser.add_argument("--db", type=Path, default=bb.DEFAULT_DB)
    parser.add_argument("--knowledge-base-dir", type=Path, default=bb.DEFAULT_KNOWLEDGE_BASE_DIR)
    parser.add_argument("--property-senses-dir", type=Path, default=bb.DEFAULT_PROPERTY_SENSES_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--count-scale", type=float, default=1.0, help="e.g. 0.1 for a dry run")
    parser.add_argument("--timeout", type=float, default=bb.QUERY_TIMEOUT, help="seconds per query")
    parser.add_argument("--memory-limit", default="4GB")
    parser.add_argument("--holdout", type=int, default=HOLDOUT_ENTITIES,
                        help="entities never used to fill a train question")  # fmt: skip
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    manifest = build(
        args.db, args.knowledge_base_dir, args.property_senses_dir, args.out_dir,
        seed=args.seed, count_scale=args.count_scale, timeout=args.timeout,
        memory_limit=args.memory_limit, holdout=args.holdout,
    )  # fmt: skip
    t = manifest["totals"]
    print(f"  generated {t['generated']}/{t['target']}, kept {t['kept']} ({t['val']} val), "
          f"senses differ from the linker's in {t['senses_mismatch']}")  # fmt: skip
    print(f"  dropped: {manifest['dropped']}; known linker errors: {manifest['known_losses']}")
    removed = {k: len(v) for k, v in manifest["holdout"]["removed"].items()}
    held, candidates = manifest["holdout_entities"], manifest["holdout"]["candidates"]
    print(f"  held out: {len(held)} of {len(candidates)} candidates; removed fillers {removed}")
    print(f"  by group: {manifest['by_group']}")
    print(f"  example chars: {manifest['example_chars']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
