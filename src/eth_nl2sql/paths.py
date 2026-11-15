"""Default locations of the data, relative to the repository root.

Commands run from the repository root (the scripts cd there first), so every path is relative.
README.md, section "Cấu trúc repo", describes what each folder holds.
"""

from __future__ import annotations

from pathlib import Path

DATA = Path("data")

# Inputs: the month's extract, the label snapshot, the reviewed knowledge base and dictionaries.
RAW_DIR = DATA / "raw"
RAW_MONTH_DIR = RAW_DIR / "2026-09"
LABELS_DIR = RAW_DIR / "labels" / "eth-labels"
KNOWLEDGE_BASE_DIR = DATA / "entity_knowledge_base"
SCHEMA_DIR = DATA / "schema"  # property_senses.csv, property_phrases.csv and the BigQuery schema
SCHEMA_FILE = SCHEMA_DIR / "bigquery_crypto_ethereum.json"

# Built from the inputs.
DB = DATA / "db" / "ethereum.duckdb"
BENCHMARK_DIR = DATA / "benchmark"
BENCHMARK_MANIFEST = BENCHMARK_DIR / "manifest.json"
DEV = BENCHMARK_DIR / "dev.jsonl"
TRAIN_DIR = DATA / "train"

# Outputs of the pipeline stages, one file per input file.
LINKING_DIR = DATA / "linking"
PROMPTS_DIR = DATA / "prompts"
PREDICTIONS_DIR = DATA / "predictions"
