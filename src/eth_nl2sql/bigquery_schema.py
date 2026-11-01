"""BigQuery's own column descriptions, saved so builds do not need BigQuery.

Schema linking describes each column with the text bigquery-public-data.crypto_ethereum
publishes for it. Columns the extract derives (blob_count, '|'-joined arrays) have no
BigQuery counterpart and get a description written here, marked as the project's.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from eth_nl2sql.extract_bigquery import SOURCE

# Table in the database -> view in bigquery-public-data.crypto_ethereum.
SOURCE_TABLES = {
    "blocks": "blocks",
    "transactions": "transactions",
    "token_transfers": "token_transfers",
    "contracts": "contracts",
    "tokens": "tokens_latest",
    "amended_tokens": "amended_tokens",
}

# BigQuery publishes no table descriptions; these state what the database holds.
TABLE_DESCRIPTIONS = {
    "blocks": "Every Ethereum block of September 2026 (UTC).",
    "transactions": "Every transaction of September 2026 (UTC), with its receipt.",
    "token_transfers": "Every decoded ERC-20/ERC-721 Transfer event of September 2026 (UTC).",
    "contracts": (
        "Contracts that September 2026 touched (called, created, or emitting token transfers); "
        "one row per deployment."
    ),
    "tokens": (
        "Metadata of every token with a transfer in September 2026, as of the extraction "
        "(tokens_latest)."
    ),
    "amended_tokens": "Hand-corrected symbol, name and decimals of tokens.",
}

PROJECT_COLUMNS = {
    ("transactions", "blob_count"): (
        "Number of blob versioned hashes of the transaction (length of BigQuery's "
        "blob_versioned_hashes)"
    ),
    ("tokens", "token_standards"): (
        "Token standards the contract implements, joined with '|' (an ARRAY in BigQuery)"
    ),
    ("tokens", "protocols"): "Protocols the token supports, joined with '|' (an ARRAY in BigQuery)",
}


def fetch_schema(path: Path, project: str | None = None) -> dict:
    """Save the type and description of every column of the source views."""
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    tables = {}
    for table, view in SOURCE_TABLES.items():
        schema = client.get_table(f"{SOURCE}.{view}").schema
        tables[table] = {
            "source": f"{SOURCE}.{view}",
            "columns": {
                f.name: {"type": f.field_type, "mode": f.mode, "description": f.description}
                for f in schema
            },
        }
    data = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **tables}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tables": data}, indent=2, ensure_ascii=False) + "\n")
    return data


def column_descriptions(path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """(table, column) -> (description, 'bigquery' | 'project')."""
    tables = json.loads(Path(path).read_text())["tables"]
    out = {
        (table, column): (info["description"], "bigquery")
        for table, spec in tables.items()
        if isinstance(spec, dict)
        for column, info in spec["columns"].items()
        if info.get("description")
    }
    out.update({k: (v, "project") for k, v in PROJECT_COLUMNS.items()})
    return out
