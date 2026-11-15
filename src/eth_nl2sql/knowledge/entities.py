"""Draft and check the entity knowledge base used for entity linking.

The reviewed knowledge base is a set of CSV files in data/entity_knowledge_base/ (see
docs/linking-method.md). `draft` regenerates candidates from a pinned eth-labels snapshot and
one month of BigQuery data. It only adds rows and refreshes evidence columns, so review
decisions are never overwritten. BigQuery data is the source of truth: activity, contract
status and token names come from the month's Parquet files, eth-labels only contributes names
for addresses.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

ETH_LABELS_REPO = "dawsbot/eth-labels"
ETH_LABELS_COMMIT = "3446e22d803b399aae93945ddbdd474c8d60297b"  # data of 2026-07-10
ETH_LABELS_FILES = ("accounts.csv", "tokens.csv")

STATUSES = ("auto", "pending", "approved", "rejected")
USABLE = ("auto", "approved")
TARGET_TYPES = ("entity", "address", "token", "class")
ADDRESS_RE = re.compile(r"0x[0-9a-f]{40}")
# Name tags that describe a contract; such an address must be a contract in BigQuery.
CONTRACT_TAG_RE = re.compile(
    r"(?i)\b(router|pool|vault|token|factory|proxy|contract|registry|controller|gauge|"
    r"minter|governance|oracle|adapter|escrow)\b"
)


class KnowledgeBaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sheet:
    file: str
    key: tuple[str, ...]
    columns: tuple[str, ...]
    evidence: tuple[str, ...]  # refreshed by every draft; the other columns belong to review

    def sort_key(self, row: dict) -> tuple:
        return tuple(row[k] for k in self.key)


ENTITIES = Sheet(
    "entities.csv",
    ("entity_id",),
    (
        "entity_id",
        "name",
        "parent_id",
        "same_as",
        "kind",
        "classes",
        "address_set",
        "rank",
        "n_addresses",
        "n_active",
        "activity",
        "status",
        "note",
    ),
    evidence=("rank", "n_addresses", "n_active", "activity"),
)
ENTITY_ADDRESSES = Sheet(
    "entity_addresses.csv",
    ("entity_id", "address"),
    (
        "entity_id",
        "address",
        "name_tag",
        "role",
        "classes",
        "label_slugs",
        "is_contract",
        "tx_to",
        "tx_from",
        "callers",
        "transfers",
        "status",
        "note",
    ),
    evidence=(
        "name_tag",
        "label_slugs",
        "is_contract",
        "tx_to",
        "tx_from",
        "callers",
        "transfers",
    ),
)
ALIASES = Sheet(
    "entity_aliases.csv",
    ("alias", "target_type", "target_id"),
    (
        "alias",
        "alias_norm",
        "lang",
        "alias_type",
        "target_type",
        "target_id",
        "source",
        "callers",
        "status",
        "note",
    ),
    evidence=("alias_norm", "callers"),
)
CLASSES_FILE = "entity_classes.csv"


def normalize_alias(text: str) -> str:
    """Matching form of an alias: NFKC, casefolded, without diacritics, punctuation as spaces.

    Symbols and unassigned code points are kept, so homoglyph tokens ('꒤5DT') stay distinct.
    """
    s = unicodedata.normalize("NFKC", text).casefold().replace("đ", "d")
    s = "".join(
        " " if unicodedata.category(c)[0] in "PZ" else c
        for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )
    return " ".join(s.split())


def slug(name: str) -> str:
    return normalize_alias(name).replace(" ", "-")


def entity_name(tag: str) -> str:
    """'Tornado.Cash: Router' -> 'Tornado.Cash'; 'Coinbase 10' -> 'Coinbase'."""
    head = tag.split(":", 1)[0].strip()
    return re.sub(r"\s+\d+$", "", head) or head


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_eth_labels(out_dir: Path, commit: str = ETH_LABELS_COMMIT) -> dict:
    """Download the pinned eth-labels CSVs and record where they came from."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ETH_LABELS_FILES:
        url = f"https://raw.githubusercontent.com/{ETH_LABELS_REPO}/{commit}/data/csv/{name}"
        log.info("fetch %s", url)
        part = out_dir / (name + ".part")
        with urllib.request.urlopen(url, timeout=120) as resp:
            part.write_bytes(resp.read())
        part.replace(out_dir / name)
        files[name] = {"url": url, "sha256": _sha256(out_dir / name)}
    source = {
        "repo": ETH_LABELS_REPO,
        "commit": commit,
        "origin": "Etherscan label cloud (scraped by eth-labels)",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
    }
    (out_dir / "source.json").write_text(json.dumps(source, indent=2) + "\n")
    return source


def check_labels_source(labels_dir: Path) -> dict:
    source = json.loads((labels_dir / "source.json").read_text())
    for name, info in source["files"].items():
        if _sha256(labels_dir / name) != info["sha256"]:
            raise KnowledgeBaseError(f"{labels_dir / name}: SHA256 differs from source.json")
    return source


def read_sheet(path: Path, columns: tuple[str, ...] | None = None) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if columns and rows and set(columns) - set(rows[0]):
        raise KnowledgeBaseError(f"{path}: missing columns {sorted(set(columns) - set(rows[0]))}")
    return rows


def write_sheet(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    tmp.replace(path)


def merge_sheet(existing: list[dict], generated: list[dict], sheet: Sheet) -> list[dict]:
    """Add new rows and refresh evidence; reviewed columns stay as they are.

    Rows still 'auto' were never reviewed, so they belong to the draft: they are replaced
    whole, and dropped when the draft no longer produces them.
    """
    fresh = {sheet.sort_key(r): r for r in generated}
    merged = {}
    for row in existing:
        k = sheet.sort_key(row)
        if k in fresh and row["status"] == "auto":
            merged[k] = {c: fresh[k].get(c, "") for c in sheet.columns}
        elif k in fresh:
            merged[k] = dict(row) | {c: fresh[k][c] for c in sheet.evidence if c in fresh[k]}
        elif row["status"] != "auto":
            merged[k] = dict(row)
    for k, row in fresh.items():
        merged.setdefault(k, {c: row.get(c, "") for c in sheet.columns})
    return list(merged.values())


def _connect(memory_limit: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(config={"memory_limit": memory_limit, "TimeZone": "UTC"})


def _load_activity(con: duckdb.DuckDBPyConnection, raw_dir: Path, labels_dir: Path) -> None:
    """Per-address evidence from the month for every labeled address and every token."""
    parquet = {
        t: f"read_parquet({_lit(str(raw_dir / t / '*.parquet'))})"
        for t in ("transactions", "token_transfers", "contracts", "tokens")
    }
    con.execute(
        f"""CREATE TABLE labels AS
        SELECT lower(address) AS address, label AS slug,
          nullif(nullif(trim(nameTag), ''), 'null') AS tag
        FROM read_csv({_lit(str(labels_dir / "accounts.csv"))}, all_varchar=true, escape='\\')
        WHERE chainId = '1'"""
    )
    con.execute(f"CREATE TABLE tokens AS SELECT address, symbol, name FROM {parquet['tokens']}")
    con.execute(
        "CREATE TABLE cand AS SELECT address FROM labels WHERE tag IS NOT NULL "
        "UNION SELECT address FROM tokens"
    )
    log.info("activity: transactions")
    con.execute(
        f"""CREATE TABLE tx_to AS SELECT to_address AS address, count(*) AS n,
          count(DISTINCT from_address) AS callers
        FROM {parquet["transactions"]} t SEMI JOIN cand ON cand.address = t.to_address
        GROUP BY 1"""
    )
    con.execute(
        f"""CREATE TABLE tx_from AS SELECT from_address AS address, count(*) AS n
        FROM {parquet["transactions"]} t SEMI JOIN cand ON cand.address = t.from_address
        GROUP BY 1"""
    )
    log.info("activity: token_transfers")
    con.execute(
        f"""CREATE TABLE transfers AS SELECT address, sum(n) AS n FROM (
          SELECT to_address AS address, count(*) AS n FROM {parquet["token_transfers"]} t
            SEMI JOIN cand ON cand.address = t.to_address GROUP BY 1
          UNION ALL
          SELECT from_address, count(*) FROM {parquet["token_transfers"]} t
            SEMI JOIN cand ON cand.address = t.from_address GROUP BY 1
        ) GROUP BY 1"""
    )
    con.execute(
        f"CREATE TABLE contracts AS SELECT DISTINCT address FROM {parquet['contracts']} "
        "SEMI JOIN cand USING (address)"
    )
    con.execute(
        """CREATE TABLE activity AS SELECT cand.address,
          coalesce(tx_to.n, 0) AS tx_to, coalesce(tx_from.n, 0) AS tx_from,
          coalesce(tx_to.callers, 0) AS callers, coalesce(transfers.n, 0) AS transfers,
          -- contracts only holds contracts the month touched; an address that signs a
          -- transaction is an EOA; anything else is unknown.
          CASE WHEN contracts.address IS NOT NULL THEN 'true'
            WHEN tx_from.n > 0 THEN 'false' ELSE '' END AS is_contract
        FROM cand LEFT JOIN tx_to USING (address) LEFT JOIN tx_from USING (address)
          LEFT JOIN transfers USING (address) LEFT JOIN contracts USING (address)"""
    )


def _class_slugs(knowledge_base_dir: Path) -> dict[str, str]:
    slugs = {}
    for row in read_sheet(knowledge_base_dir / CLASSES_FILE):
        if row["status"] == "rejected":
            continue
        for s in filter(None, row["eth_labels_slugs"].split("|")):
            slugs[s] = row["class_id"]
    return slugs


def draft(
    raw_dir: Path,
    labels_dir: Path,
    knowledge_base_dir: Path,
    *,
    min_token_callers: int = 100,
    memory_limit: str = "4GB",
) -> dict[str, int]:
    """Regenerate candidate entities, addresses and aliases; return row counts per sheet."""
    raw_dir, labels_dir, knowledge_base_dir = (
        Path(raw_dir),
        Path(labels_dir),
        Path(knowledge_base_dir),
    )
    check_labels_source(labels_dir)
    class_of = _class_slugs(knowledge_base_dir)

    with _connect(memory_limit) as con:
        _load_activity(con, raw_dir, labels_dir)
        labeled = con.execute(
            """SELECT l.address, l.slug, l.tag, a.tx_to, a.tx_from, a.callers, a.transfers,
              a.is_contract
            FROM labels l JOIN activity a USING (address) WHERE l.tag IS NOT NULL
            ORDER BY l.address, l.tag, l.slug"""
        ).fetchall()
        tokens = con.execute(
            """SELECT t.address, t.symbol, t.name, a.callers
            FROM tokens t JOIN activity a USING (address) WHERE a.callers >= ?
            ORDER BY a.callers DESC, t.address""",
            [min_token_callers],
        ).fetchall()

    # One row per (entity, address); an address with two tags can land in two entities.
    addresses: dict[tuple[str, str], dict] = {}
    names: dict[str, Counter] = defaultdict(Counter)
    for address, label_slug, tag, tx_to, tx_from, callers, transfers, is_contract in labeled:
        name = entity_name(tag)
        eid = slug(name)
        if not eid or not ADDRESS_RE.fullmatch(address):
            continue
        names[eid][name] += 1
        row = addresses.setdefault(
            (eid, address),
            {
                "entity_id": eid,
                "address": address,
                "role": tag.split(":", 1)[1].strip() if ":" in tag else tag,
                "tags": set(),
                "slugs": set(),
                "is_contract": is_contract,
                "tx_to": tx_to,
                "tx_from": tx_from,
                "callers": callers,
                "transfers": transfers,
                "status": "auto",
            },
        )
        row["tags"].add(tag)
        row["slugs"].add(label_slug)
    for row in addresses.values():
        row["name_tag"] = " | ".join(sorted(row["tags"]))
        row["label_slugs"] = "|".join(sorted(row["slugs"]))
        row["classes"] = "|".join(sorted({class_of[s] for s in row["slugs"] if s in class_of}))

    activity: dict[str, list[dict]] = defaultdict(list)
    for row in addresses.values():
        activity[row["entity_id"]].append(row)
    entities = []
    for eid, rows in activity.items():
        n_active = sum(1 for r in rows if r["tx_to"] or r["tx_from"] or r["transfers"])
        if not n_active:
            continue
        entities.append(
            {
                "entity_id": eid,
                # Most frequent spelling; ties broken by the text itself, so drafts are stable.
                "name": max(names[eid].items(), key=lambda kv: (kv[1], kv[0]))[0],
                "classes": "|".join(
                    sorted({c for r in rows for c in r["classes"].split("|") if c})
                ),
                "n_addresses": len(rows),
                "n_active": n_active,
                "activity": sum(r["tx_to"] + r["tx_from"] + r["transfers"] for r in rows),
                "status": "pending",
            }
        )
    entities.sort(key=lambda e: (-e["activity"], e["entity_id"]))
    kept = {e["entity_id"] for e in entities}
    by_name = {normalize_alias(e["name"]): e["entity_id"] for e in entities}
    for i, e in enumerate(entities, 1):
        e["rank"] = i
        # Parent hint: the longest other entity whose name is a word prefix of this one
        # ('Uniswap V3' -> 'Uniswap', 'Binance Dep' -> 'Binance').
        words = normalize_alias(e["name"]).split()
        for n in range(len(words) - 1, 0, -1):
            parent = by_name.get(" ".join(words[:n]))
            if parent:
                e["parent_id"] = parent
                break

    rank = {e["entity_id"]: e["rank"] for e in entities}
    address_rows = sorted(
        (r for r in addresses.values() if r["entity_id"] in kept),
        key=lambda r: (rank[r["entity_id"]], -(r["tx_to"] + r["tx_from"]), r["address"]),
    )

    aliases = []
    for c in read_sheet(knowledge_base_dir / CLASSES_FILE):
        for text, lang in ((c["name_en"], "en"), (c["name_vi"], "vi")):
            if text:
                aliases.append(
                    _alias(text, "name", "class", c["class_id"], "entity_classes.csv", lang)
                )
    for e in entities:
        aliases.append(_alias(e["name"], "name", "entity", e["entity_id"], "eth-labels"))
    # Etherscan renames and renumbers wallets, and eth-labels keeps the old tag next to the new
    # one, so a tag can sit on two addresses. When exactly one of them carries it as its only
    # tag, the tag is that address's name: an only tag cannot be a leftover old name.
    holders: dict[str, set[str]] = defaultdict(set)
    norms_of: dict[str, set[str]] = defaultdict(set)
    for r in address_rows:
        for tag in r["tags"]:
            holders[normalize_alias(tag)].add(r["address"])
            norms_of[r["address"]].add(normalize_alias(tag))
    owner_of = {}
    for norm, holding in holders.items():
        sole = [a for a in holding if norms_of[a] == {norm}]
        if len(holding) > 1 and len(sole) == 1:
            owner_of[norm] = sole[0]
    for r in address_rows:
        for tag in sorted(r["tags"]):
            alias = _alias(tag, "name_tag", "address", r["address"], "eth-labels")
            owner = owner_of.get(alias["alias_norm"])
            if owner and owner != r["address"]:
                alias.update(status="rejected", note=f"Tên cũ: đây là tag duy nhất của {owner}")
            aliases.append(alias)
    for address, symbol, name, callers in tokens:
        for text, kind in ((symbol, "symbol"), (name, "name")):
            if text and normalize_alias(text):
                aliases.append(_alias(text, kind, "token", address, "bigquery", callers=callers))

    knowledge_base_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for sheet, generated in (
        (ENTITIES, entities),
        (ENTITY_ADDRESSES, address_rows),
        (ALIASES, aliases),
    ):
        path = knowledge_base_dir / sheet.file
        merged = merge_sheet(read_sheet(path, sheet.columns), generated, sheet)
        if sheet is ENTITIES:
            merged.sort(key=lambda r: (int(r["rank"] or 10**9), r["entity_id"]))
        write_sheet(path, sheet.columns, merged)
        counts[sheet.file] = len(merged)
    return counts


def _alias(
    text: str,
    alias_type: str,
    target_type: str,
    target_id: str,
    source: str,
    lang: str = "any",
    callers: int = 0,
) -> dict:
    return {
        "alias": text,
        "alias_norm": normalize_alias(text),
        "lang": lang,
        "alias_type": alias_type,
        "target_type": target_type,
        "target_id": target_id,
        "source": source,
        "callers": callers or "",
        "status": "auto",
    }


@dataclass
class CheckReport:
    errors: list[str]
    warnings: list[str]
    counts: dict[str, dict[str, int]]


@dataclass
class Usable:
    """The rows entity linking may use; entities merged by same_as resolve to canonical_id."""

    classes: list[dict]  # approved
    entities: list[dict]  # approved
    addresses: list[dict]  # 'auto' or approved, of an approved entity
    aliases: list[dict]  # 'auto' or approved, pointing at a usable target or at a token


def usable(knowledge_base_dir: Path) -> Usable:
    knowledge_base_dir = Path(knowledge_base_dir)
    classes = read_sheet(knowledge_base_dir / CLASSES_FILE)
    entities = read_sheet(knowledge_base_dir / ENTITIES.file, ENTITIES.columns)
    addresses = read_sheet(knowledge_base_dir / ENTITY_ADDRESSES.file, ENTITY_ADDRESSES.columns)
    aliases = read_sheet(knowledge_base_dir / ALIASES.file, ALIASES.columns)

    same_as = {e["entity_id"]: e["same_as"] for e in entities if e["same_as"]}

    def canonical(eid: str) -> str:
        seen = set()
        while same_as.get(eid) and eid not in seen:
            seen.add(eid)
            eid = same_as[eid]
        return eid

    approved = {e["entity_id"] for e in entities if e["status"] == "approved"}
    out_entities = [
        e | {"canonical_id": canonical(e["entity_id"])}
        for e in entities
        if e["status"] == "approved"
    ]
    out_addresses = [
        a | {"canonical_id": canonical(a["entity_id"])}
        for a in addresses
        if a["status"] in USABLE and a["entity_id"] in approved
    ]
    out_classes = [c for c in classes if c["status"] == "approved"]
    targets = {
        "entity": approved,
        "address": {a["address"] for a in out_addresses},
        "class": {c["class_id"] for c in out_classes},
    }
    out_aliases = []
    for al in aliases:
        if al["status"] not in USABLE or al["target_type"] not in TARGET_TYPES:
            continue
        if al["target_type"] != "token" and al["target_id"] not in targets[al["target_type"]]:
            continue  # target not approved yet: the alias is simply not used
        if al["target_type"] == "entity":
            al = al | {"target_id": canonical(al["target_id"])}
        out_aliases.append(al)
    return Usable(out_classes, out_entities, out_addresses, out_aliases)


def check(knowledge_base_dir: Path) -> CheckReport:
    """Validate references and review statuses; list what a reviewer should look at."""
    knowledge_base_dir = Path(knowledge_base_dir)
    errors: list[str] = []
    warnings: list[str] = []
    classes = read_sheet(knowledge_base_dir / CLASSES_FILE)
    entities = read_sheet(knowledge_base_dir / ENTITIES.file, ENTITIES.columns)
    addresses = read_sheet(knowledge_base_dir / ENTITY_ADDRESSES.file, ENTITY_ADDRESSES.columns)
    aliases = read_sheet(knowledge_base_dir / ALIASES.file, ALIASES.columns)

    counts = {}
    for name, rows in (
        ("classes", classes),
        ("entities", entities),
        ("entity_addresses", addresses),
        ("aliases", aliases),
    ):
        counts[name] = dict(Counter(r["status"] for r in rows))
        for r in rows:
            if r["status"] not in STATUSES:
                errors.append(f"{name}: unknown status {r['status']!r} in {r}")

    class_ids = {c["class_id"] for c in classes}
    entity_ids = {e["entity_id"] for e in entities}
    approved = {e["entity_id"] for e in entities if e["status"] == "approved"}
    for e in entities:
        for col in ("parent_id", "same_as"):
            if e[col] and e[col] not in entity_ids:
                errors.append(f"entities: {e['entity_id']}.{col} = {e[col]!r} does not exist")
        if e["status"] == "approved" and e["same_as"] and e["same_as"] not in approved:
            errors.append(f"entities: approved {e['entity_id']} is same_as an unapproved entity")
        for c in filter(None, e["classes"].split("|")):
            if c not in class_ids:
                errors.append(f"entities: {e['entity_id']} has unknown class {c!r}")
        if e["status"] == "approved" and e["address_set"] not in ("closed", "open"):
            errors.append(f"entities: approved {e['entity_id']} needs address_set closed|open")
    for a in addresses:
        if a["entity_id"] not in entity_ids:
            errors.append(f"entity_addresses: unknown entity {a['entity_id']!r}")
        if not ADDRESS_RE.fullmatch(a["address"]):
            errors.append(f"entity_addresses: bad address {a['address']!r}")
        for c in filter(None, a["classes"].split("|")):
            if c not in class_ids:
                errors.append(f"entity_addresses: {a['address']} has unknown class {c!r}")
    for al in aliases:
        if al["target_type"] not in TARGET_TYPES:
            errors.append(f"aliases: bad target_type {al['target_type']!r} for {al['alias']!r}")
        if al["alias_norm"] != normalize_alias(al["alias"]):
            errors.append(
                f"aliases: alias_norm of {al['alias']!r} is stale (re-run draft-entities)"
            )

    use = usable(knowledge_base_dir)
    owner: dict[str, set[str]] = defaultdict(set)  # usable address -> canonical entities
    for a in use.addresses:
        owner[a["address"]].add(a["canonical_id"])
        if CONTRACT_TAG_RE.search(a["name_tag"]) and a["is_contract"] == "false":
            warnings.append(
                f"contract-like tag on an address that signs transactions: {a['name_tag']!r} "
                f"{a['address']} (reject, or approve with a note)"
            )
    seen: dict[str, set[tuple[str, str]]] = defaultdict(set)
    reviewed: dict[str, set[tuple[str, str]]] = defaultdict(set)  # explicitly approved
    for al in use.aliases:
        seen[al["alias_norm"]].add((al["target_type"], al["target_id"]))
        if al["status"] == "approved":
            reviewed[al["alias_norm"]].add((al["target_type"], al["target_id"]))
    for alias_norm, targets_ in sorted(seen.items()):
        # Tokens are ranked at link time, and an address inside an entity the alias also
        # names is that entity's own wallet: neither is a real ambiguity. An alias whose
        # targets were all approved by hand is a reviewed one-to-many alias: it links to all.
        named = {t for kind, t in targets_ if kind == "entity"}
        real = {
            (kind, t)
            for kind, t in targets_
            if kind != "token" and not (kind == "address" and owner[t] & named)
        }
        # A tag shared by addresses of one entity still links to the right entity; only the
        # wallet inside it is unclear, so such an alias simply links to all of them.
        same_entity = (
            bool(real)
            and all(kind == "address" for kind, _ in real)
            and bool(set.intersection(*(owner[t] for _, t in real)))
        )
        if len(real) > 1 and not same_entity and not real <= reviewed[alias_norm]:
            warnings.append(f"alias {alias_norm!r} points to {sorted(real)}")
    counts["usable"] = {
        "entities": len(approved),
        "addresses": len(owner),
        "aliases": sum(len(v) for v in seen.values()),
    }
    return CheckReport(errors, warnings, counts)
