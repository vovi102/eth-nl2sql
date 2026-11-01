from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from eth_nl2sql import entity_knowledge_base as knowledge_base
from eth_nl2sql import property_senses
from eth_nl2sql.bigquery_schema import fetch_schema
from eth_nl2sql.build_duckdb import (
    DatabaseError,
    build_database,
    load_knowledge_base,
    report_path,
    verify_database,
)
from eth_nl2sql.entity_linking import EntityLinker
from eth_nl2sql.entity_linking_evaluation import (
    EvaluationError,
    evaluate,
    format_report,
    knowledge_base_version,
    read_records,
    write_records,
)
from eth_nl2sql.entity_linking_resources import load_resources
from eth_nl2sql.extract_bigquery import ExtractError, Extractor, Window, verify_extract

DEFAULT_DB = Path("data/db/ethereum.duckdb")
DEFAULT_MONTH_DIR = Path("data/raw/2026-09")
DEFAULT_SCHEMA_FILE = Path("data/schema/bigquery_crypto_ethereum.json")
DEFAULT_PROPERTY_SENSES_DIR = Path("data/schema")
DEFAULT_LABELS_DIR = Path("data/raw/labels/eth-labels")
DEFAULT_KNOWLEDGE_BASE_DIR = Path("data/entity_knowledge_base")
DEFAULT_LINKING_DIR = Path("data/linking")


def _knowledge_base(args: argparse.Namespace) -> int:
    if args.command == "fetch-labels":
        source = knowledge_base.fetch_eth_labels(args.labels_dir, args.commit)
        print(f"fetched {source['repo']}@{source['commit'][:10]} into {args.labels_dir}")
    elif args.command == "draft-entities":
        counts = knowledge_base.draft(
            args.raw_dir,
            args.labels_dir,
            args.knowledge_base_dir,
            min_token_callers=args.min_token_callers,
            memory_limit=args.memory_limit,
        )
        for file, n in counts.items():
            print(f"  {file:<22} {n:>8,} rows")
    else:
        report = knowledge_base.check(args.knowledge_base_dir)
        for name, by_status in report.counts.items():
            print(f"  {name:<18} {by_status}")
        for w in report.warnings:
            print(f"warning: {w}")
        for e in report.errors:
            print(f"error: {e}", file=sys.stderr)
        return 1 if report.errors else 0
    return 0


def _link_file(linker: EntityLinker, records: list[dict], out: Path, args) -> list[dict]:
    """Link every question of `records`; the output keeps their question_id."""
    predictions = [
        linker.link(r["question"], r.get("question_id", f"q{i:06d}"))
        for i, r in enumerate(records, 1)
    ]
    write_records(out, predictions)
    version = knowledge_base_version(args.knowledge_base_dir, args.property_senses_dir)
    meta = out.with_name(out.name + ".meta.json")
    meta.write_text(json.dumps({"knowledge_base_version": version}, indent=2) + "\n")
    print(f"linked {len(predictions)} questions into {out}")
    return predictions


def _linking(args: argparse.Namespace) -> int:
    if args.command == "evaluate-linking":
        gold = read_records(args.gold, allow_test=args.allow_test)
        if args.predictions:
            predictions = read_records(args.predictions, allow_test=args.allow_test)
        else:
            linker = EntityLinker(
                load_resources(args.db, args.knowledge_base_dir, memory_limit=args.memory_limit)
            )
            out = args.output or DEFAULT_LINKING_DIR / f"{args.gold.stem}.predictions.jsonl"
            predictions = _link_file(linker, gold, out, args)
        report = evaluate(gold, predictions)
        print(format_report(report))
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        return 0

    if bool(args.question) == bool(args.input):
        print("error: give either a question or --input", file=sys.stderr)
        return 2
    linker = EntityLinker(
        load_resources(args.db, args.knowledge_base_dir, memory_limit=args.memory_limit)
    )
    if args.input:
        records = read_records(args.input, allow_test=args.allow_test)
        out = args.output or DEFAULT_LINKING_DIR / f"{args.input.stem}.predictions.jsonl"
        _link_file(linker, records, out, args)
        return 0
    record = linker.link(args.question, args.question_id)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if args.expand:
        for name, addresses in linker.placeholder_addresses(record).items():
            print(f"{{{name}}} = {len(addresses)} addresses: {', '.join(addresses[:3])}")
    return 0


def _print_tables(report: dict) -> None:
    for name, info in report["tables"].items():
        print(f"  {name:<16} {info['rows']:>12,} rows")


def _extract(args: argparse.Namespace) -> int:
    if args.command == "verify-extract":
        totals = verify_extract(args.out, memory_limit=args.memory_limit)
        print(f"verified {args.out}")
    else:
        out = args.out or Path("data/raw") / args.month
        ex = Extractor(out, Window.month(args.month), project=args.project)
        if args.dry_run:
            for name, n in ex.plan():
                print(f"  {name:<16} {n / 1e9:8.1f} GB")
            return 0
        ex.run(int(args.max_gb * 1e9), workers=args.workers)
        totals = verify_extract(out)
        ex.finish()
        print(f"extracted {out} ({ex.billed_so_far() / 1e9:.1f} GB billed)")
    for name, rows in totals.items():
        print(f"  {name:<16} {rows:>12,} rows")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eth-nl2sql")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("build-db", "load one month of Parquet into DuckDB and verify it against BigQuery"),
        ("verify-db", "re-verify an existing database against the month's Parquet"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--raw-dir", type=Path, default=DEFAULT_MONTH_DIR)
        p.add_argument("--db", type=Path, default=DEFAULT_DB)
        p.add_argument("--memory-limit", default="4GB")
        if name == "build-db":
            p.add_argument("--knowledge-base-dir", type=Path, default=DEFAULT_KNOWLEDGE_BASE_DIR)
            p.add_argument("--schema-file", type=Path, default=DEFAULT_SCHEMA_FILE)
            p.add_argument("--property-senses-dir", type=Path, default=DEFAULT_PROPERTY_SENSES_DIR)
            p.add_argument("--skip-checksums", action="store_true")

    p = sub.add_parser(
        "load-knowledge-base", help="reload only the link schema of an existing database"
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--knowledge-base-dir", type=Path, default=DEFAULT_KNOWLEDGE_BASE_DIR)
    p.add_argument("--property-senses-dir", type=Path, default=DEFAULT_PROPERTY_SENSES_DIR)
    p.add_argument("--memory-limit", default="4GB")

    p = sub.add_parser(
        "check-property-senses", help="validate data/schema/property_senses.csv and phrases"
    )
    p.add_argument("--property-senses-dir", type=Path, default=DEFAULT_PROPERTY_SENSES_DIR)

    p = sub.add_parser(
        "fetch-bigquery-schema", help="save BigQuery's column types and descriptions"
    )
    p.add_argument("--out", type=Path, default=DEFAULT_SCHEMA_FILE)
    p.add_argument("--project", help="GCP project (default: gcloud config)")

    p = sub.add_parser("extract", help="extract one month from BigQuery into Parquet")
    p.add_argument("--month", default="2026-09", help="YYYY-MM, UTC calendar month")
    p.add_argument("--out", type=Path, help="default: data/raw/<month>")
    p.add_argument("--project", help="GCP project billed (default: gcloud config)")
    p.add_argument("--max-gb", type=float, default=150, help="budget for bytes billed")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--dry-run", action="store_true", help="only estimate bytes per query")

    p = sub.add_parser("verify-extract", help="re-check Parquet files against parity.json")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--memory-limit", default="4GB")

    p = sub.add_parser("fetch-labels", help="download the pinned eth-labels snapshot")
    p.add_argument("--labels-dir", type=Path, default=DEFAULT_LABELS_DIR)
    p.add_argument("--commit", default=knowledge_base.ETH_LABELS_COMMIT)

    p = sub.add_parser(
        "draft-entities", help="regenerate entity-linking candidates in data/entity_knowledge_base"
    )
    p.add_argument("--raw-dir", type=Path, default=DEFAULT_MONTH_DIR)
    p.add_argument("--labels-dir", type=Path, default=DEFAULT_LABELS_DIR)
    p.add_argument("--knowledge-base-dir", type=Path, default=DEFAULT_KNOWLEDGE_BASE_DIR)
    p.add_argument("--min-token-callers", type=int, default=100)
    p.add_argument("--memory-limit", default="4GB")

    p = sub.add_parser(
        "check-entities",
        help="validate the reviewed entity knowledge base and list review warnings",
    )
    p.add_argument("--knowledge-base-dir", type=Path, default=DEFAULT_KNOWLEDGE_BASE_DIR)

    p = sub.add_parser(
        "link-question", help="link one question (or a JSON Lines file) without an LLM"
    )
    p.add_argument("question", nargs="?")
    p.add_argument("--question-id")
    p.add_argument("--input", type=Path, help="JSON Lines with question (and question_id)")
    p.add_argument("--output", type=Path, help="default: data/linking/<input>.predictions.jsonl")
    p.add_argument("--expand", action="store_true", help="also list each placeholder's addresses")

    p = sub.add_parser(
        "evaluate-linking", help="score linking against gold (docs/linking-labels.md §9)"
    )
    p.add_argument("--gold", type=Path, required=True, help="e.g. data/benchmark/dev.jsonl")
    p.add_argument("--predictions", type=Path, help="default: link the gold questions now")
    p.add_argument("--output", type=Path, help="where to write predictions made now")
    p.add_argument("--report", type=Path, help="write the full report as JSON")
    for name in ("link-question", "evaluate-linking"):
        p = sub.choices[name]
        p.add_argument("--db", type=Path, default=DEFAULT_DB)
        p.add_argument("--knowledge-base-dir", type=Path, default=DEFAULT_KNOWLEDGE_BASE_DIR)
        p.add_argument("--property-senses-dir", type=Path, default=DEFAULT_PROPERTY_SENSES_DIR)
        p.add_argument("--memory-limit", default="4GB")
        p.add_argument(
            "--allow-test", action="store_true", help="final evaluation only: allow test.jsonl"
        )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if args.command in ("fetch-labels", "draft-entities", "check-entities"):
        try:
            return _knowledge_base(args)
        except knowledge_base.KnowledgeBaseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if args.command in ("extract", "verify-extract"):
        try:
            return _extract(args)
        except ExtractError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if args.command in ("link-question", "evaluate-linking"):
        try:
            return _linking(args)
        except EvaluationError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if args.command == "check-property-senses":
        errors = property_senses.check(args.property_senses_dir)
        use = property_senses.usable(args.property_senses_dir)
        print(f"  {len(use.senses)} approved senses, {len(use.phrases)} approved phrases")
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1 if errors else 0

    if args.command == "fetch-bigquery-schema":
        tables = fetch_schema(args.out, project=args.project)
        print(f"saved {args.out} ({len(tables) - 1} tables)")
        return 0

    try:
        if args.command == "build-db":
            report = build_database(
                args.raw_dir,
                args.db,
                knowledge_base_dir=args.knowledge_base_dir,
                schema_file=args.schema_file,
                property_senses_dir=args.property_senses_dir,
                verify_checksums=not args.skip_checksums,
                memory_limit=args.memory_limit,
            )
            print(f"built {args.db} (report: {report_path(args.db)})")
        elif args.command == "load-knowledge-base":
            counts = load_knowledge_base(
                args.db,
                args.knowledge_base_dir,
                args.property_senses_dir,
                memory_limit=args.memory_limit,
            )
            print(f"reloaded link schema of {args.db}")
            for name, n in counts.items():
                print(f"  link.{name:<18} {n:>10,} rows")
            return 0
        else:
            report = verify_database(args.db, args.raw_dir, memory_limit=args.memory_limit)
            print(f"verified {args.db}")
    except (DatabaseError, knowledge_base.KnowledgeBaseError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_tables(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
