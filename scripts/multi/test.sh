#!/usr/bin/env bash
# M5 of docs/multi-month-plan.md: the phase-1 models and the phase-2 adapters on the two-month
# question set (data/benchmark-multi), once each, unattended. About 3 hours on the GTX 1650;
# nothing leaves the machine. Nothing is tuned on this set (§1, principle 3).
#
#   bash scripts/multi/test.sh           # the whole run
#   bash scripts/multi/test.sh --check   # only the preconditions (no GPU)
#
# 1. Preconditions: the two-month database and question set are those of the manifest; the
#    linker's records of the set exist (M4: evaluate-linking into data/linking-multi); the S3
#    freeze holds and the adapters are the GGUFs chosen on dev (phase 2); the port is free.
# 2. Runtime check: 3B + its adapter on the September dev prompts must give exactly the replies
#    of the S6 dev run (--class-rank data, the September database). Otherwise it stops.
# 3. Prompts of the set (--class-rank question, the default) into data/prompts-multi.
# 4. Generates 3B, 3B + LoRA, 1.5B, 1.5B + LoRA on dev and test of the set. A run whose
#    predictions are complete is skipped, so running the script again after a crash resumes.
# 5. Stops llama-server, scores every run, and writes a summary: EX per run, by time stratum of
#    the question (September, October, across the two months, all the data), by template,
#    and each adapter against its base model (McNemar).
#
# Log: data/predictions/multi/multi.log; summary: multi.summary.json beside it.
set -euo pipefail
cd "$(dirname "$0")/../.."

LLAMA=${LLAMA:-$HOME/opt/llama.cpp/src-b12032/build/bin/llama-server}
PORT=8080
DB=data/db/ethereum_2026-09_2026-10.duckdb
B=data/benchmark-multi
LK=data/linking-multi
PR=data/prompts-multi
P=data/predictions/multi
LOG=$P/multi.log
M3=qwen2.5-coder-3b-instruct-q4_k_m
M15=qwen2.5-coder-1.5b-instruct-q4_k_m
SERVER_PID=""
SERVER_ARGS=""
FAILED=()

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

healthy() { curl -s -m 2 "http://127.0.0.1:$PORT/health" | grep -q '"ok"'; }

stop_server() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID"
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
  SERVER_ARGS=""
}
trap stop_server EXIT

ensure_server() {  # tag, model, then the LoRA flags; (re)starts unless it serves exactly them
  local tag=$1 model=$2
  shift 2
  if [ "$SERVER_ARGS" = "$model $*" ] && healthy; then return 0; fi
  stop_server
  "$LLAMA" -m "$PWD/models/$model.gguf" "$@" -c 8192 -ngl 99 -fa on -np 1 --cache-ram 0 \
    --port $PORT > "$P/server-multi-$tag.log" 2>&1 &
  SERVER_PID=$!
  SERVER_ARGS="$model $*"
  for _ in $(seq 1 300); do
    if healthy; then log "llama-server ready: $model $*"; return 0; fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then log "llama-server died: $tag"; return 1; fi
    sleep 1
  done
  log "llama-server not ready after 300 s: $tag"
  return 1
}

# ---- 1. preconditions --------------------------------------------------------------------
mkdir -p "$P" "$PR"
log "two-month set (M5): preconditions"
CHOSEN=$(uv run python - "$DB" "$B" "$LK" <<'PY'
import hashlib, json, sys
from pathlib import Path

import duckdb

db, bench, linking = (Path(a) for a in sys.argv[1:])

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

manifest = json.loads((bench / "manifest.json").read_text())
for name, info in manifest["files"].items():
    if sha(bench / name) != info["sha256"]:
        sys.exit(f"{bench / name} is not the file of its manifest")
with duckdb.connect(str(db), read_only=True) as con:
    build = dict(con.execute("SELECT key, value FROM meta.build").fetchall())
if [build["window_start"], build["window_end_exclusive"]] != manifest["data_window"]:
    sys.exit(f"{db} holds {build['window_start']}..{build['window_end_exclusive']}, "
             f"the set was built on {manifest['data_window']}")
for split in ("dev", "test"):
    records = [json.loads(l) for l in open(linking / f"{split}.predictions.jsonl")]
    gold = [json.loads(l)["question_id"] for l in open(bench / f"{split}.jsonl")]
    if [r["question_id"] for r in records] != gold:
        sys.exit(f"{linking / split}.predictions.jsonl does not link the questions of {split}")
freeze = json.loads(Path("data/train/freeze.json").read_text())
bad = [n for n, h in freeze["train"].items() if sha(Path("data/train") / n) != h]
bad += [n for n, h in freeze["training_config"].items() if sha(n) != h]
if bad:
    sys.exit(f"the S3 freeze no longer matches: {bad}")
chosen = []
for key in ("3b", "1.5b"):
    name = json.loads(Path(f"data/predictions/phase2/dev/dev-phase2-{key}.summary.json").read_text())["chosen"]
    run = json.loads(Path(f"models/lora/{key}-run/run.json").read_text())
    trained = {c["name"]: c for c in run["checkpoints"]}[name]
    if sha(f"models/lora/{key}-run/gguf/{name}.gguf") != trained["gguf_sha256"]:
        sys.exit(f"{name}.gguf is not the GGUF of run.json")
    chosen.append(name)
print(f"set and database match; S3 freeze holds; adapters {', '.join(chosen)}", file=sys.stderr)
print(" ".join(chosen))
PY
)
read -r A3 A15 <<< "$CHOSEN"
log "adapters: 3B $A3, 1.5B $A15"
L3=models/lora/3b-run/gguf/$A3.gguf
L15=models/lora/1.5b-run/gguf/$A15.gguf
if healthy; then
  log "port $PORT is busy: stop the running llama-server first"
  exit 1
fi
[ -x "$LLAMA" ] || { log "no llama-server at $LLAMA"; exit 1; }
for m in $M3 $M15; do [ -f "models/$m.gguf" ] || { log "missing models/$m.gguf"; exit 1; }; done
if [ "${1:-}" = "--check" ]; then
  log "preconditions hold (--check: stopping here)"
  exit 0
fi
START=$(date +%s)

# ---- 2. runtime check on the September dev run ---------------------------------------------
if [ -f "$P/multi.started" ]; then
  log "resuming a run started on $(cat "$P/multi.started"): runtime check passed then"
else
  ensure_server dev $M3 --lora "$PWD/$L3"
  mkdir -p "$P/recheck"
  RECHECK=$P/recheck/dev.predictions.prompts.$M3.$A3.jsonl
  log "runtime check: 3B + $A3 on the September dev prompts (about 14 min)"
  uv run eth-nl2sql generate-sql --class-rank data --prompts data/prompts/dev.predictions.prompts.jsonl \
    --adapter "$L3" --output "$RECHECK" > "$P/recheck/dev.$A3.log" 2>&1
  uv run python - "data/predictions/phase2/dev/dev.predictions.prompts.$M3.$A3.jsonl" "$RECHECK" <<'PY' | tee -a "$LOG"
import json, sys

def replies(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        out[r["question_id"]] = (r["decision"], [a.get("reply") for a in r.get("attempts", [])])
    return out

frozen, now = replies(sys.argv[1]), replies(sys.argv[2])
differ = sorted(q for q in frozen if frozen[q] != now.get(q))
if differ or set(now) != set(frozen):
    print(f"runtime check FAILED: {len(differ)} questions reply differently: {differ[:10]}")
    sys.exit(1)
print(f"runtime check passed: the {len(frozen)} replies are identical to the S6 dev run")
PY
  date '+%F %T' > "$P/multi.started"
fi

# ---- 3. prompts ------------------------------------------------------------------------------
for split in dev test; do
  out=$PR/$split.predictions.prompts.jsonl
  if [ -s "$out.meta.json" ]; then continue; fi
  log "prompts: $split"
  uv run eth-nl2sql build-prompts --allow-test --db $DB --input $LK/$split.predictions.jsonl \
    --output "$out" >> "$LOG" 2>&1
done

# ---- 4. generation --------------------------------------------------------------------------
generate() {  # tag, server tag, model, adapter ("" for none), split
  local tag=$1 server=$2 model=$3 adapter=$4 split=$5
  local out=$P/$split.predictions.prompts.$model${adapter:+.$(basename "$adapter" .gguf)}.jsonl
  if [ -s "$out.meta.json" ]; then
    log "$tag $split: done already"
    return 0
  fi
  local lora=() pass=()
  if [ -n "$adapter" ]; then lora=(--lora "$PWD/$adapter"); pass=(--adapter "$adapter"); fi
  ensure_server "$server" "$model" "${lora[@]}" || { FAILED+=("$tag $split"); return 0; }
  log "$tag $split: generating -> $out"
  if uv run eth-nl2sql generate-sql --allow-test --db $DB --prompts "$PR/$split.predictions.prompts.jsonl" \
    "${pass[@]}" --output "$out" > "$P/$split.$tag.log" 2>&1; then
    log "$tag $split: done"
  else
    log "$tag $split: FAILED, see $P/$split.$tag.log"
    FAILED+=("$tag $split")
  fi
}
for split in dev test; do generate 3b 3b $M3 "" $split; done
for split in dev test; do generate 3b-lora 3b-lora $M3 "$L3" $split; done
for split in dev test; do generate 1.5b 1.5b $M15 "" $split; done
for split in dev test; do generate 1.5b-lora 1.5b-lora $M15 "$L15" $split; done
stop_server

# ---- 5. scoring (llama-server stopped: DuckDB gets the memory) -----------------------------
for split in dev test; do
  for run in $M3 $M3.$A3 $M15 $M15.$A15; do
    pred=$P/$split.predictions.prompts.$run.jsonl
    base=${pred%.jsonl}
    if [ ! -s "$pred.meta.json" ]; then log "score: $pred has no complete run, skipped"; continue; fi
    if [ -s "$base.ex.json" ]; then log "score: $base.ex.json exists"; continue; fi
    log "score: $pred"
    uv run eth-nl2sql evaluate-sql --allow-test --db $DB --gold $B/$split.jsonl --predictions "$pred" \
      --linking $LK/$split.predictions.jsonl --manifest $B/manifest.json \
      --train-manifest data/train/manifest.json --report "$base.ex.json" \
      --details "$base.ex-details.jsonl" > "$base.ex.log" 2>&1 \
      || { log "score: FAILED, see $base.ex.log"; FAILED+=("score $pred"); }
  done
done

# ---- summary --------------------------------------------------------------------------------
uv run python - "$A3" "$A15" <<'PY' | tee -a "$LOG"
import json, math, sys
from collections import defaultdict
from pathlib import Path

a3, a15 = sys.argv[1:]
P, B = Path("data/predictions/multi"), Path("data/benchmark-multi")
M3, M15 = "qwen2.5-coder-3b-instruct-q4_k_m", "qwen2.5-coder-1.5b-instruct-q4_k_m"
runs = {"3b": M3, "3b_lora": f"{M3}.{a3}", "1.5b": M15, "1.5b_lora": f"{M15}.{a15}"}
gold = {}
for split in ("dev", "test"):
    for line in open(B / f"{split}.jsonl"):
        r = json.loads(line)
        gold[r["question_id"]] = r | {"split": split}

def stratum(time):
    if time is None:
        return "all_data"
    start, end = time["start"][:10], time["end"][:10]
    return "september" if end <= "2026-10-01" else "october" if start >= "2026-10-01" else "across"

def correct(run):
    out = {}
    for split in ("dev", "test"):
        path = P / f"{split}.predictions.prompts.{run}.ex-details.jsonl"
        if path.is_file():
            out |= {json.loads(l)["question_id"]: json.loads(l)["correct"] for l in open(path)}
    return out

def ex(hits):
    return {"ex": round(sum(hits) / len(hits), 4) if hits else None, "correct": sum(hits),
            "questions": len(hits)}

def mcnemar(a, b):
    better = sum(1 for q in a if q in b and not a[q] and b[q])
    worse = sum(1 for q in a if q in b and a[q] and not b[q])
    n, k = better + worse, min(better, worse)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
    return {"better": better, "worse": worse, "p": round(p, 6)}

summary, results = {}, {}
for name, run in runs.items():
    c = correct(run)
    results[name] = c
    if not c:
        summary[name] = None
        print(f"{name:<10} no result")
        continue
    groups = defaultdict(list)
    for q, hit in c.items():
        g = gold[q]
        groups[g["split"]].append(hit)
        groups["all"].append(hit)
        if g["gold_sql"] is not None:
            groups[stratum(g["time"])].append(hit)
        groups[f"template:{g['template_id']}"].append(hit)
    summary[name] = {k: ex(v) for k, v in sorted(groups.items())}
    s = summary[name]
    print(f"{name:<10} all {s['all']['correct']}/{s['all']['questions']} = {s['all']['ex']}  "
          + "  ".join(f"{k} {s[k]['correct']}/{s[k]['questions']}"
                      for k in ("dev", "test", "september", "october", "across", "all_data") if k in s))
for base, lora in (("3b", "3b_lora"), ("1.5b", "1.5b_lora")):
    if results.get(base) and results.get(lora):
        summary[f"{lora}_vs_{base}"] = mcnemar(results[base], results[lora])
        print(f"{lora} vs {base}: {summary[f'{lora}_vs_{base}']}")
(P / "multi.summary.json").write_text(json.dumps(summary, indent=1) + "\n")
PY

END=$(date +%s)
log "done in $(( (END - START) / 60 )) min; failed: ${FAILED[*]:-none}"
