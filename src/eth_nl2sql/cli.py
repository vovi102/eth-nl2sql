from __future__ import annotations

import argparse
import hashlib
import json
import logging
import statistics
import sys
import time
from collections import Counter
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
from eth_nl2sql.execution_evaluation import (
    GoldError,
    gold_as_predictions,
    held_out_targets,
    report,
    run_questions,
    train_exposure,
)
from eth_nl2sql.execution_evaluation import format_report as format_ex_report
from eth_nl2sql.extract_bigquery import ExtractError, Extractor, Window, verify_extract
from eth_nl2sql.prompt_builder import SCHEMA_MODES, PromptBuilder, PromptOptions, load_schema
from eth_nl2sql.sql_execution import SqlRunner
from eth_nl2sql.sql_generation import (
    MAX_ATTEMPTS,
    ClientError,
    OpenAICompatibleClient,
    ReplayClient,
    adapter_tag,
    generate,
    messages_key,
    served_adapter,
    shared_prefix,
    summary,
)

DEFAULT_DB = Path("data/db/ethereum.duckdb")
DEFAULT_MONTH_DIR = Path("data/raw/2026-09")
DEFAULT_SCHEMA_FILE = Path("data/schema/bigquery_crypto_ethereum.json")
DEFAULT_PROPERTY_SENSES_DIR = Path("data/schema")
DEFAULT_LABELS_DIR = Path("data/raw/labels/eth-labels")
DEFAULT_KNOWLEDGE_BASE_DIR = Path("data/entity_knowledge_base")
DEFAULT_LINKING_DIR = Path("data/linking")
DEFAULT_PROMPTS_DIR = Path("data/prompts")
DEFAULT_PREDICTIONS_DIR = Path("data/predictions")
DEFAULT_SERVER = "http://127.0.0.1:8080"
DEFAULT_MANIFEST = Path("data/benchmark/manifest.json")


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

    if args.command == "build-prompts":
        return _build_prompts(args)
    if args.command == "evaluate-sql":
        return _evaluate_sql(args)
    if args.command == "generate-sql":
        return _generate_sql(args)

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
    if args.prompt:
        prompt = PromptBuilder(linker, load_schema(args.db)).build(record)
        if prompt["decision"] != "sql":
            print(f"\n[{prompt['decision']}] {prompt['answer']}")
        for m in prompt.get("messages", []):
            print(f"\n===== {m['role']}\n{m['content']}")
    return 0


def _prompt_options(args: argparse.Namespace) -> tuple[PromptOptions, str]:
    """The options and the file-name tag of a prompt variant (proposed-method.md §7)."""
    options = PromptOptions(
        evidence=not args.no_evidence, placeholders=not args.literal, schema=args.schema
    )
    tags = [t for t, on in (("no-evidence", args.no_evidence), ("literal", args.literal),
                            ("linked-schema", args.schema == "linked")) if on]  # fmt: skip
    return options, ".".join(["prompts", *tags])


def _build_prompts(args: argparse.Namespace) -> int:
    """B6/B7 for every linking record of --input (linker predictions, or gold as oracle)."""
    records = read_records(args.input, allow_test=args.allow_test)
    options, tag = _prompt_options(args)
    linker = EntityLinker(
        load_resources(args.db, args.knowledge_base_dir, memory_limit=args.memory_limit)
    )
    builder = PromptBuilder(linker, load_schema(args.db))
    prompts = [builder.build(r, options) for r in records]
    out = args.output or DEFAULT_PROMPTS_DIR / f"{args.input.stem}.{tag}.jsonl"
    write_records(out, prompts)
    meta = {
        "input": str(args.input),
        "options": vars(options),
        "knowledge_base_version": knowledge_base_version(
            args.knowledge_base_dir, args.property_senses_dir
        ),
    }
    out.with_name(out.name + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    decisions = Counter(p["decision"] for p in prompts)
    chars = sorted(p["chars"] for p in prompts if p["decision"] == "sql")
    print(f"wrote {len(prompts)} prompts into {out}: {dict(decisions)}")
    if chars:
        print(f"  prompt chars: min {chars[0]:,}, median {chars[len(chars) // 2]:,}, "
              f"max {chars[-1]:,}")  # fmt: skip
    return 0


def _evaluate_sql(args: argparse.Namespace) -> int:
    """EX of generated SQL; --self-check scores the gold SQL itself (must be 1.0)."""
    gold = read_records(args.gold, allow_test=args.allow_test)
    excluded = None
    if args.self_check:
        predictions, linking = gold_as_predictions(gold), gold
    elif args.predictions:
        predictions = read_records(args.predictions, allow_test=args.allow_test)
        linking_path = args.linking or DEFAULT_LINKING_DIR / f"{args.gold.stem}.predictions.jsonl"
        linking = read_records(linking_path, allow_test=args.allow_test)
        if args.only_predicted:
            scored = {p["question_id"] for p in predictions if "skipped" not in p}
            excluded = sum(g["question_id"] not in scored for g in gold)
            gold = [g for g in gold if g["question_id"] in scored]
            print(f"--only-predicted: {len(gold)} questions scored, {excluded} left out")
    else:
        print("error: give --predictions or --self-check", file=sys.stderr)
        return 2
    linker = EntityLinker(
        load_resources(args.db, args.knowledge_base_dir, memory_limit=args.memory_limit)
    )
    with SqlRunner(args.db, timeout=args.timeout, memory_limit=args.memory_limit) as runner:
        results = run_questions(gold, predictions, linking, linker, runner)
    manifest = json.loads(args.manifest.read_text()) if args.manifest.is_file() else None
    exposure = None
    if args.train_manifest:
        held_out = held_out_targets(json.loads(args.train_manifest.read_text()))
        exposure = {g["question_id"]: train_exposure(g, held_out, linker.class_members)
                    for g in gold}  # fmt: skip
    rep = report(results, gold, manifest, exposure)
    if exposure is not None:
        rep["train_manifest"] = str(args.train_manifest)
        rep["train_manifest_sha256"] = _sha256(args.train_manifest)
    if excluded is not None:
        rep["excluded"] = excluded
    print(format_ex_report(rep))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(rep, indent=2) + "\n")
    if args.details:
        write_records(args.details, results)
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 24):
            digest.update(chunk)
    return digest.hexdigest()


def _spread(records: list[dict], n: int) -> list[dict]:
    """n records evenly spaced over the file, for a trial run on every template."""
    if n >= len(records):
        return records
    return [records[i * len(records) // n] for i in range(n)]


def _generate_sql(args: argparse.Namespace) -> int:
    """The model of a running server on every prompt of --prompts, then B8 (one repair)."""
    prompts = read_records(args.prompts, allow_test=args.allow_test)
    prompt_meta_path = args.prompts.with_name(args.prompts.name + ".meta.json")
    prompt_meta = json.loads(prompt_meta_path.read_text())
    linking_path = args.linking or Path(prompt_meta["input"])
    linking = {r["question_id"]: r for r in read_records(linking_path, allow_test=args.allow_test)}

    if args.replies:
        if not args.no_repair or args.adapter or args.limit:
            raise ClientError("--replies plays back one reply per prompt: give --no-repair, "
                              "and neither --adapter nor --limit")  # fmt: skip
        client = ReplayClient(args.replies)
        asked = {messages_key(p["messages"]) for p in prompts if p["decision"] == "sql"}
        if asked != set(client.replies):
            missing, extra = len(asked - set(client.replies)), len(set(client.replies) - asked)
            raise ClientError(
                f"{args.replies} does not answer exactly the sql prompts of "
                f"{args.prompts}: {missing} without reply, {extra} too many"
            )
    else:
        client = OpenAICompatibleClient(
            args.server, model=args.model, max_tokens=args.max_tokens, seed=args.seed
        )
    # Of the whole file, so that a trial run primes the same tokens as the full run.
    prefix = [] if args.no_fixed_prefix else shared_prefix(client, prompts)
    if args.limit:
        prompts = _spread(prompts, args.limit)
    server = client.server_info()
    model_path = Path(server["model_path"]) if server["model_path"] else None
    tag = model_path.stem if model_path else (server["models"] or ["model"])[0]
    served = served_adapter(server["lora_adapters"], args.adapter)
    file_tag = tag if served is None else f"{tag}.{adapter_tag(args.adapter, served['scale'])}"
    out = args.output or DEFAULT_PREDICTIONS_DIR / f"{args.prompts.stem}.{file_tag}.jsonl"
    max_attempts = 1 if args.no_repair else MAX_ATTEMPTS
    adapter = "" if served is None else f", LoRA {args.adapter.name} at scale {served['scale']}"
    print(f"model {tag}{adapter}, n_ctx {server['n_ctx']}, {len(prompts)} prompts, "
          f"fixed prefix {len(prefix)} tokens -> {out}")  # fmt: skip

    linker = EntityLinker(
        load_resources(args.db, args.knowledge_base_dir, memory_limit=args.memory_limit)
    )

    def progress(i: int, p: dict) -> None:
        kinds = [a["error_kind"] or "ran" for a in p["attempts"]]
        seconds = sum(a["seconds"] for a in p["attempts"])
        detail = p.get("skipped") or (f"{' -> '.join(kinds)} ({seconds:.1f} s)" if kinds else "")
        print(f"  [{i}/{len(prompts)}] {p['question_id']} {p['decision']} {detail}", flush=True)

    started = time.time()
    predictions = []
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with SqlRunner(args.db, timeout=args.timeout, memory_limit=args.memory_limit) as runner, \
            tmp.open("w", encoding="utf-8") as f:  # fmt: skip
        for p in generate(prompts, linking, linker, runner, client, progress=progress,
                          placeholder_check=not args.no_placeholder_check,
                          prefix=prefix, max_attempts=max_attempts):  # fmt: skip
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
            f.flush()
            predictions.append(p)
    tmp.replace(out)

    stats = summary(predictions)
    meta = {
        "prompts": str(args.prompts),
        "prompts_sha256": _sha256(args.prompts),
        "prompt_options": prompt_meta.get("options"),
        "linking": str(linking_path),
        "linking_sha256": _sha256(linking_path),
        "limit": args.limit,
        "model": tag,
        "model_sha256": _sha256(model_path) if model_path and model_path.is_file() else None,
        "adapter": None
        if served is None
        else {
            "path": str(args.adapter),
            "sha256": _sha256(args.adapter),
            "scale": served["scale"],
            "served": served,
        },
        "server": server,
        "generation": client.generation_parameters()
        | {
            "max_attempts": max_attempts,
            "placeholder_check": not args.no_placeholder_check,
            "fixed_prefix_tokens": len(prefix),
        },
        "sql_timeout": args.timeout,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "wall_seconds": round(time.time() - started, 1),
        "summary": stats,
    }
    out.with_name(out.name + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(stats, indent=2))
    return 0


def _count_prompt_tokens(args: argparse.Namespace) -> int:
    """Tokens of every sql prompt (chat template included) by the server's tokenizer."""
    client = OpenAICompatibleClient(args.server, max_tokens=args.max_tokens)
    n_ctx = client.context_size()
    room = n_ctx - args.max_tokens if n_ctx else None
    print(f"n_ctx {n_ctx}, reply budget {args.max_tokens}: a prompt fits up to {room} tokens")
    report = {}
    for path in args.prompts:
        prompts = [p for p in read_records(path, allow_test=args.allow_test)
                   if p["decision"] == "sql"]  # fmt: skip
        tokens = sorted(client.prompt_tokens(p["messages"]) for p in prompts)
        if None in tokens:
            print("error: the server has no /tokenize", file=sys.stderr)
            return 1
        report[str(path)] = {
            "prompts": len(tokens),
            "min": tokens[0],
            "median": statistics.median(tokens),
            "max": tokens[-1],
            "over_context": sum(t > room for t in tokens) if room else None,
        }
        r = report[str(path)]
        print(
            f"  {path.name:<48} {r['prompts']:>4} prompts  min {r['min']:>7,}  "
            f"median {r['median']:>9,.1f}  max {r['max']:>7,}  over {r['over_context']}"
        )
    if args.report:
        args.report.write_text(json.dumps({"n_ctx": n_ctx, "files": report}, indent=2) + "\n")
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
    p.add_argument("--prompt", action="store_true", help="also print the prompt of the question")

    p = sub.add_parser(
        "build-prompts", help="B6/B7: the SQL model's prompt for every record of a linking file"
    )
    p.add_argument(
        "--input", type=Path, required=True,
        help="linking records: data/linking/dev.predictions.jsonl, or gold for oracle linking",
    )  # fmt: skip
    p.add_argument("--output", type=Path, help="default: data/prompts/<input>.prompts[.tags].jsonl")
    p.add_argument("--no-evidence", action="store_true", help="ablation: schema and question only")
    p.add_argument("--literal", action="store_true", help="ablation: addresses instead of {E1}")
    p.add_argument("--schema", choices=SCHEMA_MODES, default="full", help="B6 (default: full)")

    p = sub.add_parser(
        "evaluate-linking", help="score linking against gold (docs/linking-labels.md §9)"
    )
    p.add_argument("--gold", type=Path, required=True, help="e.g. data/benchmark/dev.jsonl")
    p.add_argument("--predictions", type=Path, help="default: link the gold questions now")
    p.add_argument("--output", type=Path, help="where to write predictions made now")
    p.add_argument("--report", type=Path, help="write the full report as JSON")
    p = sub.add_parser("evaluate-sql", help="B8 + EX: run generated and gold SQL, compare results")
    p.add_argument("--gold", type=Path, required=True, help="e.g. data/benchmark/dev.jsonl")
    p.add_argument("--predictions", type=Path, help="JSON Lines {question_id, decision, sql}")
    p.add_argument(
        "--linking", type=Path,
        help="linking records the prompts came from (default: data/linking/<gold>.predictions"
        ".jsonl; the gold file for oracle linking)",
    )  # fmt: skip
    p.add_argument("--self-check", action="store_true", help="score the gold SQL itself")
    p.add_argument(
        "--only-predicted", action="store_true",
        help="score only questions with a prediction that is not skipped (trial run, decision 7)",
    )  # fmt: skip
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument(
        "--train-manifest", type=Path,
        help="data/train/manifest.json: also split EX by targets held out of training (R8)",
    )  # fmt: skip
    p.add_argument("--timeout", type=float, default=30.0, help="seconds per query")
    p.add_argument("--report", type=Path, help="write the report as JSON")
    p.add_argument("--details", type=Path, help="write one scored line per question")
    p = sub.add_parser(
        "generate-sql", help="the SQL model of a running server on a prompt file, then B8"
    )
    p.add_argument("--prompts", type=Path, required=True, help="a file of data/prompts/")
    p.add_argument(
        "--linking", type=Path,
        help="linking records the prompts came from (default: `input` of the prompts' meta)",
    )  # fmt: skip
    p.add_argument("--output", type=Path, help="default: data/predictions/<prompts>.<model>.jsonl")
    p.add_argument("--server", default=DEFAULT_SERVER, help="OpenAI-compatible base URL")
    p.add_argument("--model", help="model name to send (llama-server serves one model)")
    p.add_argument("--max-tokens", type=int, default=512, help="tokens per reply")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--limit", type=int, help="only N prompts, evenly spaced (trial run)")
    p.add_argument(
        "--no-placeholder-check", action="store_true",
        help="accept a query that runs but leaves out a placeholder of the prompt (ablation)",
    )  # fmt: skip
    p.add_argument(
        "--no-fixed-prefix", action="store_true",
        help="let the prompt cache reuse what it shares with the question before",
    )  # fmt: skip
    p.add_argument(
        "--adapter", type=Path,
        help="the GGUF LoRA adapter the server loads (--lora/--lora-scaled): checked against "
        "/lora-adapters, hashed into the meta file and named in the output file",
    )  # fmt: skip
    p.add_argument(
        "--no-repair", action="store_true", help="one attempt per question, no repair (ablation)"
    )
    p.add_argument(
        "--replies", type=Path,
        help="play back the replies of `train_lora.py generate` (transformers fp16 on Kaggle, "
        "phase2-plan.md §5.4) instead of asking a server; needs --no-repair",
    )  # fmt: skip
    p.add_argument("--timeout", type=float, default=30.0, help="seconds per query")

    p = sub.add_parser(
        "count-prompt-tokens", help="tokens of prompt files by the tokenizer of llama-server"
    )
    p.add_argument("prompts", type=Path, nargs="+")
    p.add_argument("--server", default=DEFAULT_SERVER)
    p.add_argument("--max-tokens", type=int, default=512, help="room left for the reply")
    p.add_argument("--report", type=Path, help="write the counts as JSON")
    p.add_argument("--allow-test", action="store_true", help="final evaluation only")

    for name in ("link-question", "evaluate-linking", "build-prompts", "evaluate-sql",
                 "generate-sql"):  # fmt: skip
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

    if args.command == "count-prompt-tokens":
        try:
            return _count_prompt_tokens(args)
        except (EvaluationError, ClientError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if args.command in ("link-question", "evaluate-linking", "build-prompts", "evaluate-sql",
                        "generate-sql"):  # fmt: skip
        try:
            return _linking(args)
        except (EvaluationError, GoldError, ClientError) as e:
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
