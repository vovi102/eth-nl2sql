"""M6: analysis of the M5 run on the two-month set (docs/multi-month-plan.md).

Reads only files already written by scripts/multi/test.sh and by the September test runs
(S4, S8); runs no SQL. Run from the repository root:

    python3 data/predictions/multi/analyze.py

Writes data/predictions/multi/multi.analysis.json and prints the tables.
"""

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

P, B = Path("data/predictions/multi"), Path("data/benchmark-multi")
M3, M15 = "qwen2.5-coder-3b-instruct-q4_k_m", "qwen2.5-coder-1.5b-instruct-q4_k_m"
RUNS = {"3b": M3, "3b_lora": f"{M3}.3b-e2", "1.5b": M15, "1.5b_lora": f"{M15}.1.5b-e2"}
# The September test runs of the same four configurations (S4 for the base models, S8 for LoRA).
SEPT = {
    "3b": Path(f"data/predictions/phase1/test/test.predictions.prompts.{M3}.ex-details.jsonl"),
    "3b_lora": Path(f"data/predictions/phase2/test/test.predictions.prompts.{M3}.3b-e2"
                    ".ex-details.jsonl"),
    "1.5b": Path(f"data/predictions/phase1/test/test.predictions.prompts.{M15}.ex-details.jsonl"),
    "1.5b_lora": Path(f"data/predictions/phase2/test/test.predictions.prompts.{M15}.1.5b-e2"
                      ".ex-details.jsonl"),
}  # fmt: skip
STRATA = ("september", "october", "across", "all_data")
DATE = re.compile(r"20\d\d-\d\d-\d\d")


def jsonl(path):
    return [json.loads(line) for line in open(path)]


def stratum(time):
    if time is None:
        return "all_data"
    start, end = time["start"][:10], time["end"][:10]
    return "september" if end <= "2026-10-01" else "october" if start >= "2026-10-01" else "across"


def ex(hits):
    hits = list(hits)
    return {"correct": sum(hits), "questions": len(hits),
            "ex": round(sum(hits) / len(hits), 3) if hits else None}  # fmt: skip


def mcnemar(base, other, ids):
    better = sum(1 for q in ids if not base[q] and other[q])
    worse = sum(1 for q in ids if base[q] and not other[q])
    n, k = better + worse, min(better, worse)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
    return {"better": better, "worse": worse, "p": round(p, 4)}


def time_use(record, sql):
    """How the predicted SQL uses the question's time range.

    exact: both bounds of the gold range appear as dates; none: no date at all; other: some
    other dates. A question without a time range is exact when the SQL has no date.
    """
    dates = set(DATE.findall(sql or ""))
    if record["time"] is None:
        return "exact" if not dates else "added"
    want = {record["time"]["start"][:10], record["time"]["end"][:10]}
    return "exact" if want <= dates else "none" if not dates else "other"


def main():
    gold = {}
    for split in ("dev", "test"):
        for r in jsonl(B / f"{split}.jsonl"):
            gold[r["question_id"]] = r | {"split": split}
    held = json.load(open("data/train/manifest.json"))["leakage"]["held_out"]
    near = {t for t, b in held.items() if b["near"]}
    far = set(held) - near

    hit, sql, kind = {}, {}, {}
    for name, run in RUNS.items():
        hit[name], kind[name], sql[name] = {}, {}, {}
        for split in ("dev", "test"):
            for d in jsonl(P / f"{split}.predictions.prompts.{run}.ex-details.jsonl"):
                hit[name][d["question_id"]] = d["correct"]
                kind[name][d["question_id"]] = d["kind"]
            for p in jsonl(P / f"{split}.predictions.prompts.{run}.jsonl"):
                sql[name][p["question_id"]] = p.get("sql")

    def ids(split, pred=lambda r: True):
        return [q for q, r in gold.items() if r["split"] == split and pred(r)]

    def answerable(r):
        return r["gold_sql"] is not None

    out = {"near_templates": sorted(near), "far_templates": sorted(far)}

    # 1. EX per split: answerable questions by time stratum and by near/far template.
    for split in ("dev", "test"):
        part = out[split] = {}
        for name in RUNS:
            h = hit[name]
            row = {"all": ex(h[q] for q in ids(split)),
                   "answerable": ex(h[q] for q in ids(split, answerable))}  # fmt: skip
            for s in STRATA:
                row[s] = ex(h[q] for q in ids(split, lambda r, s=s: answerable(r)
                                               and stratum(r["time"]) == s))  # fmt: skip
            for bucket, tids in (("near", near), ("far", far)):
                row[bucket] = ex(h[q] for q in ids(split, lambda r, t=tids: r["template_id"] in t))
                for s in STRATA:
                    row[f"{bucket}:{s}"] = ex(
                        h[q] for q in ids(split, lambda r, t=tids, s=s: r["template_id"] in t
                                          and stratum(r["time"]) == s))  # fmt: skip
            part[name] = row
        part["mcnemar"] = {}
        for base, lora in (("3b", "3b_lora"), ("1.5b", "1.5b_lora")):
            m = {"all": mcnemar(hit[base], hit[lora], ids(split))}
            for s in STRATA:
                m[s] = mcnemar(hit[base], hit[lora],
                               ids(split, lambda r, s=s: answerable(r)
                                   and stratum(r["time"]) == s))  # fmt: skip
            part["mcnemar"][f"{lora}_vs_{base}"] = m
        part["mcnemar"]["3b_lora_vs_1.5b_lora"] = {
            "all": mcnemar(hit["1.5b_lora"], hit["3b_lora"], ids(split))
        }

    # 2. Test per template: two-month set against the September test (same templates, other
    #    questions and, for October and the cross-month ranges, other data).
    sept_gold = {r["question_id"]: r for r in jsonl("data/benchmark/test.jsonl")}
    sept = {name: {d["question_id"]: d["correct"] for d in jsonl(path)}
            for name, path in SEPT.items()}  # fmt: skip
    templates = sorted({r["template_id"] for r in gold.values() if r["split"] == "test"})
    table = {}
    for t in templates:
        mq = [q for q in ids("test") if gold[q]["template_id"] == t]
        sq = [q for q, r in sept_gold.items() if r["template_id"] == t]
        table[t] = {"bucket": "near" if t in near else "far" if t in far else "refuse",
                    "questions": [len(sq), len(mq)]}  # fmt: skip
        for name in RUNS:
            table[t][name] = [sum(sept[name][q] for q in sq), sum(hit[name][q] for q in mq)]
    out["test_templates"] = table
    out["sept_test"] = {name: ex(v.values()) for name, v in sept.items()}

    # 3. Time ranges: does the SQL carry the question's range (evidence line) unchanged?
    tu = out["time_use"] = {}
    for split in ("dev", "test"):
        tu[split] = {}
        for name in RUNS:
            rows = defaultdict(Counter)
            for q in ids(split, answerable):
                r = gold[q]
                rows[stratum(r["time"])][(time_use(r, sql[name][q]), hit[name][q])] += 1
            tu[split][name] = {s: {f"{u}:{'ok' if c else 'wrong'}": n
                                   for (u, c), n in sorted(rows[s].items())}
                               for s in STRATA}  # fmt: skip

    # 4. Error kinds by stratum (test, answerable).
    ek = out["error_kinds_test"] = {}
    for name in RUNS:
        ek[name] = {s: dict(Counter(kind[name][q] for q in ids(
            "test", lambda r, s=s: answerable(r) and stratum(r["time"]) == s)
            if not hit[name][q]).most_common()) for s in STRATA}  # fmt: skip

    # 5. The all-data stratum: per template, and the questions LoRA changes.
    ad = out["all_data_test"] = {}
    for t in templates:
        q_ad = [
            q
            for q in ids("test", lambda r: answerable(r) and r["time"] is None)
            if gold[q]["template_id"] == t
        ]
        if q_ad:
            ad[t] = {"questions": len(q_ad), **{n: sum(hit[n][q] for q in q_ad) for n in RUNS}}
    out["all_data_changes_test"] = {
        f"{lora}_vs_{base}": {
            "better": sorted(q for q in ids("test", lambda r: answerable(r) and r["time"] is None)
                             if not hit[base][q] and hit[lora][q]),
            "worse": sorted(q for q in ids("test", lambda r: answerable(r) and r["time"] is None)
                            if hit[base][q] and not hit[lora][q]),
        }
        for base, lora in (("3b", "3b_lora"), ("1.5b", "1.5b_lora"))
    }  # fmt: skip

    (P / "multi.analysis.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    report(out)


def report(out):
    names = list(RUNS)
    for split in ("dev", "test"):
        print(f"\n== {split}: EX (correct/questions)")
        cols = ["all", "answerable", *STRATA, "near", "far"]
        print(f"{'':10}" + "".join(f"{c:>12}" for c in cols))
        for name in names:
            row = out[split][name]
            print(
                f"{name:10}"
                + "".join(f"{row[c]['correct']:>7}/{row[c]['questions']:<4}" for c in cols)
            )
        for k, m in out[split]["mcnemar"].items():
            print(f"  {k}: " + "  ".join(f"{s} {v['better']}/{v['worse']} p={v['p']}"
                                         for s, v in m.items()))  # fmt: skip
        print("  near/far x stratum:")
        for name in names:
            row = out[split][name]
            print(
                f"  {name:10}"
                + "  ".join(
                    f"{b}:{s} {row[f'{b}:{s}']['correct']}/{row[f'{b}:{s}']['questions']}"
                    for b in ("near", "far")
                    for s in STRATA
                )
            )
    print("\n== test per template: September -> two-month (correct)")
    print(f"{'template':28}{'bucket':>7}{'n':>8}" + "".join(f"{n:>12}" for n in names))
    for t, row in out["test_templates"].items():
        print(
            f"{t:28}{row['bucket']:>7}{row['questions'][0]:>4}/{row['questions'][1]:<3}"
            + "".join(f"{row[n][0]:>6} ->{row[n][1]:>3}" for n in names)
        )
    print("  September test EX:", {n: v["ex"] for n, v in out["sept_test"].items()})
    print("\n== time use (answerable): exact/none/other/added x ok/wrong")
    for split in ("dev", "test"):
        for name in names:
            print(f"  {split} {name:10}", out["time_use"][split][name])
    print("\n== test error kinds by stratum")
    for name in names:
        print(f"  {name:10}", out["error_kinds_test"][name])
    print("\n== test all-data stratum per template")
    for t, row in out["all_data_test"].items():
        print(f"  {t:28} n={row['questions']:>2} " + " ".join(f"{n}={row[n]}" for n in names))
    print("  changes:", out["all_data_changes_test"])


if __name__ == "__main__":
    main()
