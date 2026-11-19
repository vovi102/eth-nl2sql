"""M4: the benchmark's gold and the linker give every placeholder of the two-month set the
same addresses (two implementations of the class ranking, docs/multi-month-plan.md D3)."""

import json
import sys
from pathlib import Path

import duckdb

from eth_nl2sql.benchmark import build as bb
from eth_nl2sql.linking.linker import EntityLinker
from eth_nl2sql.linking.resources import load_resources

DB = Path("data/db/ethereum_2026-09_2026-10.duckdb")
linker = EntityLinker(load_resources(DB, Path("data/entity_knowledge_base")), class_rank="question")
with duckdb.connect(str(DB), read_only=True) as con:
    link = bb.load_link_tables(con, class_rank="question")
bad = n = 0
for split in ("dev", "test"):
    for line in open(f"data/benchmark-multi/{split}.jsonl"):
        r = json.loads(line)
        n += 1
        if bb.placeholder_addresses(link, r) != linker.placeholder_addresses(r):
            bad += 1
            print("DIFFERS", r["question_id"], r["question"])
print(f"{n - bad}/{n} records: the same placeholder addresses")
sys.exit(1 if bad else 0)
