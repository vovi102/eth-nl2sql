from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eth_nl2sql import entity_knowledge_base as knowledge_base


def addr(i: int) -> str:
    return "0x" + f"{i:040x}"


ROUTER, POOL, CB10, UNI3, UNI, DEAD, USDT, FAKE = (addr(i) for i in range(1, 9))
USER1, USER2 = addr(100), addr(101)
CB44, CB7, CB8 = addr(10), addr(11), addr(12)

RAW: dict[str, dict[str, list]] = {
    "transactions": {
        "from_address": [USER1, USER2, CB10, USER1, USER1, USER2],
        "to_address": [ROUTER, ROUTER, USER1, UNI3, USDT, USDT],
    },
    "token_transfers": {"from_address": [USER1], "to_address": [UNI]},
    # UNI is left out: a contract the month never called looks like any other address.
    "contracts": {"address": [ROUTER, POOL, UNI3, USDT, FAKE]},
    "tokens": {
        "address": [USDT, FAKE],
        "symbol": ["USDT", "USDT"],
        "name": ["Tether USD", "Tether USD"],
    },
}

ACCOUNTS = [
    # Mixed-case address: labels are matched case-insensitively.
    (ROUTER.upper().replace("0X", "0x"), "1", "tornado-cash", "Tornado.Cash: Router"),
    (POOL, "1", "mixer", "Tornado.Cash: 10 ETH"),
    (CB10, "1", "coinbase", "Coinbase 10"),
    (CB10, "1", "coinbase", "Coinbase 44"),  # Etherscan sometimes gives one wallet two tags
    (CB44, "1", "coinbase", "Coinbase 44"),  # ... and the old one can be another wallet's name
    (CB7, "1", "coinbase", "Coinbase 7"),  # renumbered in a chain: 7|8 and 8|9
    (CB7, "1", "coinbase", "Coinbase 8"),
    (CB8, "1", "coinbase", "Coinbase 8"),
    (CB8, "1", "coinbase", "Coinbase 9"),
    (UNI3, "1", "dex", "Uniswap V3: Router"),
    (UNI, "1", "dex", "Uniswap: Universal Router"),
    (DEAD, "1", "old-contract", "Dead: Thing"),  # never active -> no entity
    (USER2, "1", "airdrop-hunter", "null"),  # no usable name tag
    (CB10, "56", "coinbase", "Coinbase BSC"),  # other chain
]

CLASSES = [
    {
        "class_id": "dex",
        "name_en": "decentralized exchange",
        "name_vi": "sàn phi tập trung",
        "eth_labels_slugs": "dex",
        "description": "",
        "status": "approved",
        "note": "",
    },
    {
        "class_id": "mixer",
        "name_en": "mixer",
        "name_vi": "dịch vụ trộn coin",
        "eth_labels_slugs": "mixer",
        "description": "",
        "status": "approved",
        "note": "",
    },
]


def write_csv(path: Path, header: list[str], rows: list) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    raw = tmp_path / "raw"
    for table, cols in RAW.items():
        (raw / table).mkdir(parents=True)
        pq.write_table(pa.table(cols), raw / table / f"{table}.parquet")

    labels = tmp_path / "labels"
    labels.mkdir()
    write_csv(labels / "accounts.csv", ["address", "chainId", "label", "nameTag"], ACCOUNTS)
    write_csv(
        labels / "tokens.csv",
        ["address", "chainId", "label", "name", "symbol", "website", "image"],
        [(USDT, "1", "tether", "Tether USD", "USDT", "", "")],
    )
    files = {
        n: {"sha256": hashlib.sha256((labels / n).read_bytes()).hexdigest()}
        for n in knowledge_base.ETH_LABELS_FILES
    }
    (labels / "source.json").write_text(json.dumps({"files": files}))

    knowledge_base_dir = tmp_path / "entity_knowledge_base"
    knowledge_base_dir.mkdir()
    write_csv(
        knowledge_base_dir / knowledge_base.CLASSES_FILE,
        list(CLASSES[0]),
        [list(c.values()) for c in CLASSES],
    )
    return raw, labels, knowledge_base_dir


def run_draft(dirs: tuple[Path, Path, Path]) -> dict[str, list[dict]]:
    raw, labels, knowledge_base_dir = dirs
    knowledge_base.draft(raw, labels, knowledge_base_dir, min_token_callers=2)
    return {
        name: knowledge_base.read_sheet(knowledge_base_dir / sheet.file)
        for name, sheet in (
            ("entities", knowledge_base.ENTITIES),
            ("addresses", knowledge_base.ENTITY_ADDRESSES),
            ("aliases", knowledge_base.ALIASES),
        )
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tornado.Cash", "tornado cash"),
        ("Sàn Đà Nẵng", "san da nang"),
        ("ＵＳＤＴ", "usdt"),
        ("  Uniswap   V3: Router ", "uniswap v3 router"),
        ("꒤5DT", "꒤5dt"),  # homoglyphs are not folded into ASCII
    ],
)
def test_normalize_alias(text: str, expected: str) -> None:
    assert knowledge_base.normalize_alias(text) == expected


@pytest.mark.parametrize(
    ("tag", "name"),
    [
        ("Tornado.Cash: Router", "Tornado.Cash"),
        ("Coinbase 10", "Coinbase"),
        ("Coinbase Prime 2", "Coinbase Prime"),
        ("Uniswap V3: Router", "Uniswap V3"),
        ("jaredfromsubway", "jaredfromsubway"),
    ],
)
def test_entity_name(tag: str, name: str) -> None:
    assert knowledge_base.entity_name(tag) == name


def test_draft_builds_entities_from_name_tags(dirs) -> None:
    knowledge_base_ = run_draft(dirs)
    entities = {e["entity_id"]: e for e in knowledge_base_["entities"]}
    assert set(entities) == {"tornado-cash", "coinbase", "uniswap-v3", "uniswap"}
    tc = entities["tornado-cash"]
    # The inactive pool stays in the entity so that its address set is complete.
    assert (tc["n_addresses"], tc["n_active"], tc["classes"]) == ("2", "1", "mixer")
    assert entities["uniswap-v3"]["parent_id"] == "uniswap"
    assert entities["uniswap"]["activity"] == "1"  # token transfers count as activity
    assert all(e["status"] == "pending" for e in entities.values())

    rows = {(a["entity_id"], a["address"]): a for a in knowledge_base_["addresses"]}
    router = rows["tornado-cash", ROUTER]
    assert (router["role"], router["is_contract"], router["callers"]) == ("Router", "true", "2")
    assert rows["coinbase", CB10]["is_contract"] == "false"  # it signs transactions
    assert rows["uniswap", UNI]["is_contract"] == ""  # no evidence either way
    assert rows["tornado-cash", POOL]["classes"] == "mixer"


def test_draft_aliases(dirs) -> None:
    aliases = {
        (a["alias"], a["target_type"], a["target_id"]): a for a in run_draft(dirs)["aliases"]
    }
    assert ("Tornado.Cash", "entity", "tornado-cash") in aliases
    assert ("Coinbase 10", "address", CB10) in aliases
    assert aliases["sàn phi tập trung", "class", "dex"]["lang"] == "vi"
    # Only the token that enough distinct wallets call gets aliases; the copycat does not.
    assert aliases["USDT", "token", USDT]["callers"] == "2"
    assert not any(t == FAKE for _, _, t in aliases)


def test_redraft_keeps_review_decisions(dirs) -> None:
    _, _, knowledge_base_dir = dirs
    run_draft(dirs)
    path = knowledge_base_dir / knowledge_base.ENTITIES.file
    rows = knowledge_base.read_sheet(path)
    for r in rows:
        if r["entity_id"] == "tornado-cash":
            r.update(status="approved", address_set="closed", kind="protocol", note="ok", rank="99")
    knowledge_base.write_sheet(
        path,
        knowledge_base.ENTITIES.columns,
        rows
        + [
            dict.fromkeys(knowledge_base.ENTITIES.columns, "") | {"entity_id": "manual", "rank": ""}
        ],
    )

    entities = {e["entity_id"]: e for e in run_draft(dirs)["entities"]}
    tc = entities["tornado-cash"]
    assert (tc["status"], tc["address_set"], tc["kind"], tc["note"]) == (
        "approved",
        "closed",
        "protocol",
        "ok",
    )
    assert tc["rank"] != "99"  # evidence is refreshed
    assert "manual" in entities  # rows added by the reviewer survive


def test_redraft_drops_only_unreviewed_stale_rows(dirs) -> None:
    _, _, knowledge_base_dir = dirs
    run_draft(dirs)
    path = knowledge_base_dir / knowledge_base.ALIASES.file
    rows = knowledge_base.read_sheet(path)
    for alias, status in (("Old Name", "auto"), ("Proposed", "pending"), ("Kept", "rejected")):
        rows.append(
            dict.fromkeys(knowledge_base.ALIASES.columns, "")
            | {"alias": alias, "target_type": "entity", "target_id": "coinbase", "status": status}
        )
    knowledge_base.write_sheet(path, knowledge_base.ALIASES.columns, rows)

    aliases = {a["alias"] for a in run_draft(dirs)["aliases"]}
    assert {"Proposed", "Kept"} <= aliases
    assert "Old Name" not in aliases


def test_every_tag_of_an_address_is_an_alias(dirs) -> None:
    knowledge_base_ = run_draft(dirs)
    row = next(a for a in knowledge_base_["addresses"] if a["address"] == CB10)
    assert row["name_tag"] == "Coinbase 10 | Coinbase 44"
    status = {
        (a["alias"], a["target_id"]): a["status"]
        for a in knowledge_base_["aliases"]
        if a["target_type"] == "address"
    }
    assert status["Coinbase 10", CB10] == "auto"
    # 'Coinbase 44' is CB44's only tag, so on CB10 it is a leftover old name.
    assert (status["Coinbase 44", CB44], status["Coinbase 44", CB10]) == ("auto", "rejected")
    # Neither holder of 'Coinbase 8' has it as its only tag: nothing to decide on.
    assert (status["Coinbase 8", CB7], status["Coinbase 8", CB8]) == ("auto", "auto")


def test_redraft_replaces_unreviewed_rows_but_not_reviewed_ones(dirs) -> None:
    _, _, knowledge_base_dir = dirs
    run_draft(dirs)
    path = knowledge_base_dir / knowledge_base.ALIASES.file
    rows = knowledge_base.read_sheet(path)
    for r in rows:
        if (r["alias"], r["target_id"]) == ("Coinbase 44", CB10):
            r.update(status="approved", note="checked on Etherscan")
        elif (r["alias"], r["target_id"]) == ("Coinbase 10", CB10):
            r["note"] = "scribble"  # still auto: the draft owns this row
    knowledge_base.write_sheet(path, knowledge_base.ALIASES.columns, rows)

    aliases = {(a["alias"], a["target_id"]): a for a in run_draft(dirs)["aliases"]}
    assert aliases["Coinbase 44", CB10]["status"] == "approved"
    assert aliases["Coinbase 44", CB10]["note"] == "checked on Etherscan"
    assert aliases["Coinbase 10", CB10]["note"] == ""


def test_check_flags_review_problems(dirs) -> None:
    _, _, knowledge_base_dir = dirs
    run_draft(dirs)
    path = knowledge_base_dir / knowledge_base.ENTITIES.file
    rows = knowledge_base.read_sheet(path)
    for r in rows:
        r["status"] = "approved"
        r["address_set"] = "" if r["entity_id"] == "coinbase" else "closed"
    knowledge_base.write_sheet(path, knowledge_base.ENTITIES.columns, rows)

    addresses = knowledge_base.read_sheet(knowledge_base_dir / knowledge_base.ENTITY_ADDRESSES.file)
    for a in addresses:
        if a["address"] == ROUTER:
            a["is_contract"] = "false"
    knowledge_base.write_sheet(
        knowledge_base_dir / knowledge_base.ENTITY_ADDRESSES.file,
        knowledge_base.ENTITY_ADDRESSES.columns,
        addresses,
    )

    report = knowledge_base.check(knowledge_base_dir)
    assert report.errors == ["entities: approved coinbase needs address_set closed|open"]
    assert any("Tornado.Cash: Router" in w for w in report.warnings)
    # 'Uniswap: Universal Router' has no contract evidence, which is not a violation.
    assert not any("Universal Router" in w for w in report.warnings)
    assert report.counts["usable"]["entities"] == 4


def approve_all(knowledge_base_dir: Path, same_as: dict[str, str] | None = None) -> None:
    path = knowledge_base_dir / knowledge_base.ENTITIES.file
    rows = knowledge_base.read_sheet(path)
    for r in rows:
        r.update(
            status="approved", address_set="closed", same_as=(same_as or {}).get(r["entity_id"], "")
        )
    knowledge_base.write_sheet(path, knowledge_base.ENTITIES.columns, rows)


def add_aliases(knowledge_base_dir: Path, *aliases: tuple[str, ...]) -> None:
    """Each alias is (alias, target_type, target_id[, status]); status defaults to approved."""
    path = knowledge_base_dir / knowledge_base.ALIASES.file
    rows = knowledge_base.read_sheet(path)
    for alias, target_type, target_id, *status in aliases:
        rows.append(
            dict.fromkeys(knowledge_base.ALIASES.columns, "")
            | {
                "alias": alias,
                "alias_norm": knowledge_base.normalize_alias(alias),
                "target_type": target_type,
                "target_id": target_id,
                "status": status[0] if status else "approved",
            }
        )
    knowledge_base.write_sheet(path, knowledge_base.ALIASES.columns, rows)


def test_check_reports_only_real_alias_ambiguity(dirs) -> None:
    _, _, knowledge_base_dir = dirs
    run_draft(dirs)
    approve_all(knowledge_base_dir, same_as={"uniswap-v3": "uniswap"})
    add_aliases(
        knowledge_base_dir,
        ("Coinbase 10", "entity", "coinbase"),  # also the tag of Coinbase's own wallet
        ("Uni", "entity", "uniswap"),
        ("Uni", "entity", "uniswap-v3"),  # merged into uniswap
        ("Swap", "entity", "uniswap"),
        ("Swap", "entity", "tornado-cash", "auto"),  # not reviewed: still a warning
        ("Pools", "entity", "uniswap"),
        ("Pools", "entity", "tornado-cash"),  # both approved: a reviewed one-to-many alias
        ("USDT", "entity", "coinbase"),  # entity vs token: tokens are ranked at link time
        ("Shared", "address", ROUTER, "auto"),  # one tag on wallets of two entities
        ("Shared", "address", UNI3, "auto"),
    )
    alias_warnings = [
        w for w in knowledge_base.check(knowledge_base_dir).warnings if w.startswith("alias")
    ]
    # 'Coinbase 8' (a renumbering chain inside Coinbase) links to Coinbase either way: no warning.
    assert alias_warnings == [
        f"alias 'shared' points to [('address', '{ROUTER}'), ('address', '{UNI3}')]",
        "alias 'swap' points to [('entity', 'tornado-cash'), ('entity', 'uniswap')]",
    ]


def test_draft_refuses_modified_labels(dirs) -> None:
    raw, labels, knowledge_base_dir = dirs
    with (labels / "accounts.csv").open("a") as f:
        f.write(f"{addr(9)},1,x,Injected\n")
    with pytest.raises(knowledge_base.KnowledgeBaseError, match="SHA256"):
        knowledge_base.draft(raw, labels, knowledge_base_dir)
