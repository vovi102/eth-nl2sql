"""Checks of the phase-2 training before any GPU time (docs/phase2-plan.md §4, §7 S5).

- the data and config.json still match the S3 freeze;
- the training sees the prompt exactly as llama-server renders it for generate-sql, and the
  loss covers exactly the reply and its end of turn, on every one of the 1,968 examples, with
  the tokenizer of the pinned revision and the tokenization of TRL itself;
- TRL's fused LM head gives the loss of the full logits;
- a short CPU training of a tiny Qwen2 runs end to end: checkpoints, adapters, GGUF conversion
  with convert_lora_to_gguf.py of llama.cpp b12032.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest
import train_lora
from conftest import FREEZE, LLAMA_CPP, SFT, TRAIN_DIR, need, phase1_meta

CONFIG = json.loads((TRAIN_DIR / "config.json").read_text())
FROZEN = train_lora.frozen_hashes(json.loads(FREEZE.read_text()))


def test_the_freeze_holds_and_catches_a_change() -> None:
    config_text = (TRAIN_DIR / "config.json").read_text()
    train_lora.check_freeze(SFT, config_text, FROZEN)
    with pytest.raises(SystemExit, match="config.json"):
        train_lora.check_freeze(SFT, config_text.replace('"r": 16', '"r": 8'), FROZEN)


def test_examples() -> None:
    train, val = train_lora.load_examples(SFT)
    assert (len(train), len(val)) == (1870, 98)
    ids = [r["question_id"] for r in train + val]
    assert len(set(ids)) == len(ids)
    assert all([m["role"] for m in r["prompt"]] == ["system", "user"] for r in train + val)
    assert all(r["completion"][0]["role"] == "assistant" for r in train + val)
    assert all(r["completion"][0]["content"].startswith("```sql\n") for r in train + val)
    assert [len(x) for x in train_lora.load_examples(SFT, limit=3)] == [3, 3]


def test_the_lock_pins_the_stack_but_not_torch() -> None:
    lock = (TRAIN_DIR / "requirements.lock").read_text()
    versions = train_lora.locked_versions(lock)
    assert versions["trl"] == "1.17.1" and versions["peft"] == "0.21.2"
    assert versions["transformers"] == "5.22.1" and versions["accelerate"] == "1.15.0"
    assert "torch" not in versions and "sympy" not in versions
    entries = [line for line in lock.splitlines() if "==" in line and not line[0].isspace()]
    hashed = [line for line in lock.splitlines() if line.lstrip().startswith("--hash=sha256:")]
    assert len(entries) == len(versions) == 55 and len(hashed) >= len(entries)
    assert train_lora.check_versions(lock)["torch"].startswith("2.11.0")


def test_the_chat_template_is_the_runtime_one(tokenizer) -> None:
    """Training renders every prompt exactly as llama-server did in phase 1."""
    import trl

    assert train_lora.sha256_text(tokenizer.chat_template) == train_lora.CHAT_TEMPLATE_SHA256
    bundled = Path(trl.__file__).parent / "chat_templates/qwen2_5.jinja"
    assert bundled.read_text() == tokenizer.chat_template  # what TRL recognizes as Qwen2.5
    train, val = train_lora.load_examples(SFT)
    for size in ("1.5b", "3b"):
        meta = phase1_meta(size)
        need(meta)
        gguf_template = json.loads(meta.read_text())["server"]["chat_template"]
        for row in train + val:
            for messages, generation in ((row["prompt"], True),
                                         (row["prompt"] + row["completion"], False)):  # fmt: skip
                hf = tokenizer.apply_chat_template(
                    messages, add_generation_prompt=generation, tokenize=False
                )
                gguf = tokenizer.apply_chat_template(
                    messages, add_generation_prompt=generation, tokenize=False,
                    chat_template=gguf_template,
                )  # fmt: skip
                assert hf == gguf, (size, row["question_id"])


@pytest.fixture(scope="module")
def full_trainer(tiny_base: Path, tmp_path_factory):
    """The trainer of all 1,968 examples on the tiny model: TRL's own tokenization."""
    from transformers import AutoModelForCausalLM, AutoTokenizer

    train, val = train_lora.load_examples(SFT)
    tokenizer = AutoTokenizer.from_pretrained(tiny_base)
    model = AutoModelForCausalLM.from_pretrained(tiny_base)
    out = tmp_path_factory.mktemp("trainer")
    trainer = train_lora.make_trainer(model, tokenizer, CONFIG, train, val, out, gpu=False)
    return trainer, tokenizer, train, val


def test_the_loss_covers_exactly_the_reply_on_every_example(full_trainer) -> None:
    trainer, tokenizer, train, val = full_trainer
    assert trainer.chat_template is None  # the original template, no training variant
    stats = {
        "train": train_lora.check_tokenized(train, trainer.train_dataset, tokenizer, 6144),
        "val": train_lora.check_tokenized(val, trainer.eval_dataset, tokenizer, 6144),
    }
    assert stats["train"]["examples"] == 1870 and stats["val"]["examples"] == 98
    lengths = [stats[s]["tokens"][k] for s in stats for k in ("min", "max")]
    # count-prompt-tokens (llama-server's tokenizer) measured 4,277-4,624 (phase2-plan §2.6)
    assert 4200 <= min(lengths) and max(lengths) <= 4700 < 6144
    print(json.dumps(stats))
    # the last trained tokens are the end of turn and the newline after it
    example = trainer.train_dataset[0]
    tail = tokenizer.convert_ids_to_tokens(example["labels"][-2:])
    assert tail == ["<|im_end|>", "Ċ"]


def test_check_tokenized_catches_a_wrong_mask(full_trainer) -> None:
    trainer, tokenizer, train, _ = full_trainer
    rows, good = train[:1], trainer.train_dataset[0]
    train_lora.check_tokenized(rows, [good], tokenizer, 6144)
    n = good["labels"].index(next(t for t in good["labels"] if t != -100))
    prompt_in_loss = {**good, "labels": good["input_ids"]}
    reply_cut = {**good, "labels": good["labels"][:-1] + [-100]}
    reply_short = {**good, "labels": good["labels"][:n] + [-100] + good["labels"][n + 1 :]}
    for bad, message in ((prompt_in_loss, "prompt token"), (reply_cut, "left out"),
                         (reply_short, "left out")):  # fmt: skip
        with pytest.raises(ValueError, match=message):
            train_lora.check_tokenized(rows, [bad], tokenizer, 6144)
    with pytest.raises(ValueError, match="max_length"):
        train_lora.check_tokenized(rows, [good], tokenizer, 4000)


def test_the_fused_head_gives_the_loss_of_the_full_logits(full_trainer) -> None:
    import torch

    trainer, *_ = full_trainer
    out = train_lora.check_fused_head(trainer.model, trainer.train_dataset[0],
                                      torch.device("cpu"))  # fmt: skip
    assert out["tokens"] > 0 and math.isfinite(out["fused_loss"])


def _run(tiny_base: Path, out: Path, *extra: str) -> dict:
    need(LLAMA_CPP)
    argv = ["train", "--model", "1.5b", "--base", str(tiny_base), "--data", str(SFT),
            "--out", str(out), "--llama-cpp", str(LLAMA_CPP), *extra]  # fmt: skip
    assert train_lora.main(argv) == 0
    return json.loads((out / "run.json").read_text())


def _is_gguf_adapter(path: Path) -> bool:
    data = path.read_bytes()
    return data[:4] == b"GGUF" and b"adapter.lora.alpha" in data


def test_a_short_run_keeps_the_last_step(tiny_base: Path, tmp_path: Path) -> None:
    run = _run(tiny_base, tmp_path, "--limit", "4", "--max-steps", "2")
    assert [s["step"] for s in run["steps"]] == [1, 2]
    losses = [entry["loss"] for entry in run["logs"] if "loss" in entry]
    assert len(losses) == 2 and all(math.isfinite(x) for x in losses)
    assert run["stopped_early"] is None and "eval_loss" in run["eval_after"]
    assert run["parameters"]["trainable_dtypes"] == ["torch.float32"]
    assert run["llama_cpp"]["commit"] == train_lora.LLAMA_CPP["commit"]
    (checkpoint,) = run["checkpoints"]
    assert checkpoint["name"] == "1.5b-s2"
    assert _is_gguf_adapter(tmp_path / checkpoint["gguf"])
    assert run["tokenized"]["train"]["examples"] == 4
    assert run["sft_config"]["max_length"] == 6144 and run["sft_config"]["fp16"] is False


def test_a_full_run_keeps_one_adapter_per_epoch(tiny_base: Path, tmp_path: Path) -> None:
    run = _run(tiny_base, tmp_path, "--limit", "3")
    names = [c["name"] for c in run["checkpoints"]]
    assert names == ["1.5b-e1", "1.5b-e2"]
    assert [round(c["epoch"]) for c in run["checkpoints"]] == [1, 2]
    evals = [entry for entry in run["log_history"] if "eval_loss" in entry]
    assert len(evals) == 2  # the 5% val examples at the end of each epoch
    for c in run["checkpoints"]:
        assert _is_gguf_adapter(tmp_path / c["gguf"])
        config = json.loads((tmp_path / "adapters" / c["name"] / "adapter_config.json").read_text())
        assert config["r"] == 16 and config["lora_alpha"] == 32
        assert sorted(config["target_modules"]) == sorted(CONFIG["lora"]["target_modules"])
    # two different adapters: the second epoch changed the weights
    assert run["checkpoints"][0]["adapter_sha256"] != run["checkpoints"][1]["adapter_sha256"]


def test_prepare_bakes_the_run_into_the_kernel(tmp_path: Path) -> None:
    assert train_lora.main(["prepare", "--model", "1.5b", "--max-steps", "50",
                            "--out", str(tmp_path)]) == 0  # fmt: skip
    meta = json.loads((tmp_path / "kernel-metadata.json").read_text())
    assert meta["code_file"] == "train_lora.py" and meta["is_private"] == "true"
    assert meta["enable_gpu"] == "true" and meta["enable_internet"] == "true"
    assert meta["machine_shape"] == "NvidiaTeslaT4"
    assert meta["dataset_sources"] == ["aohkvd/eth-nl2sql-phase2-sft"]
    spec = importlib.util.spec_from_file_location("kernel", tmp_path / "train_lora.py")
    kernel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kernel)
    assert kernel.KERNEL["argv"] == ["train", "--model", "1.5b", "--install", "--max-steps", "50"]
    assert kernel.KERNEL["frozen"] == FROZEN
    assert kernel.KERNEL["config"] == (TRAIN_DIR / "config.json").read_text()
    source = (TRAIN_DIR / "train_lora.py").read_text()
    assert kernel.KERNEL["source_sha256"] == train_lora.sha256_text(source)
    baked = (tmp_path / "train_lora.py").read_text().splitlines()
    assert sum(a != b for a, b in zip(baked, source.splitlines(), strict=True)) == 1


def test_prepare_dataset_holds_only_the_train_data(tmp_path: Path) -> None:
    assert train_lora.main(["prepare-dataset", "--out", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dataset-metadata.json", "sft.jsonl"]
    assert train_lora.sha256(tmp_path / "sft.jsonl") == FROZEN["sft.jsonl"]
    meta = json.loads((tmp_path / "dataset-metadata.json").read_text())
    assert meta["id"] == "aohkvd/eth-nl2sql-phase2-sft"


def test_a_failing_triton_head_falls_back_to_the_torch_tiles(monkeypatch) -> None:
    import torch
    import trl.kernels.chunked_logprob as chunked

    monkeypatch.setattr(chunked, "_TRITON_AVAILABLE", True)
    calls = []

    def check(model, example, device):
        calls.append(chunked._TRITON_AVAILABLE)
        if chunked._TRITON_AVAILABLE:
            raise train_lora.FusedHeadError("disagrees")
        return {"fused_loss": 1.0}

    out = train_lora.checked_fused_head(None, {}, torch.device("cuda"), check=check)
    assert calls == [True, False] and out["triton"] is False and "disagrees" in out["triton_error"]

    # without Triton to begin with, a failure stops the run
    def fail(model, example, device):
        raise train_lora.FusedHeadError("disagrees")

    with pytest.raises(train_lora.FusedHeadError):
        train_lora.checked_fused_head(None, {}, torch.device("cpu"), check=fail)


def test_an_old_optional_backend_that_peft_refuses_is_removed() -> None:
    """Kaggle's torchao 0.10.0 made PEFT 0.21.2 raise in pilot 1."""
    from types import SimpleNamespace

    installed = {"torchao"}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd[-1] if cmd[1] == "-c" else " ".join(cmd[1:]))
        if cmd[1] == "-m":  # pip uninstall
            installed.discard(cmd[-1])
            return SimpleNamespace(returncode=0)
        broken = "is_torchao_available" in cmd[-1] and "torchao" in installed
        stderr = "ImportError: Found an incompatible version of torchao. Found version 0.10.0"
        return SimpleNamespace(returncode=1 if broken else 0, stderr=stderr if broken else "")

    removed = train_lora.remove_incompatible_backends(run=run)
    assert [r["package"] for r in removed] == ["torchao"] and "0.10.0" in removed[0]["reason"]
    assert "-m pip uninstall --yes torchao" in calls
    assert train_lora.remove_incompatible_backends(run=run) == []  # nothing left to remove


def test_repeated_key_value_heads_give_the_same_loss(tiny_base: Path, tmp_path: Path) -> None:
    """The T4 fix of pilot 2: transformers' repeat_kv branch instead of enable_gqa."""
    import torch
    import transformers.integrations.sdpa_attention as sdpa
    from transformers import AutoModelForCausalLM, AutoTokenizer

    train, val = train_lora.load_examples(SFT, limit=1)
    tokenizer = AutoTokenizer.from_pretrained(tiny_base)
    model = AutoModelForCausalLM.from_pretrained(tiny_base)
    trainer = train_lora.make_trainer(model, tokenizer, CONFIG, train, val, tmp_path, gpu=False)
    assert model.model.layers[0].self_attn.num_key_value_groups == 2  # the tiny model has GQA
    repeats = []
    original = sdpa.repeat_kv

    def counting(hidden, n_rep):
        repeats.append(n_rep)
        return original(hidden, n_rep)

    sdpa.repeat_kv = counting
    try:
        from torch.nn.attention import SDPBackend

        # a forced kernel, as on the GPU (memory-efficient there; math is the one every CPU has)
        out = train_lora.check_attention(trainer.model, trainer.train_dataset[0],
                                         torch.device("cpu"), SDPBackend.MATH)  # fmt: skip
        assert math.isclose(out["gqa_loss"], out["repeat_kv_loss"], rel_tol=1e-5)
        # two forwards on each path, two layers, keys and values: repeats only on the second path
        assert repeats == [2] * 8
        assert sdpa.use_gqa_in_sdpa is train_lora._never
    finally:
        sdpa.repeat_kv = original
        train_lora.repeat_kv_in_sdpa(False)
    assert sdpa.use_gqa_in_sdpa is sdpa._original_use_gqa_in_sdpa
