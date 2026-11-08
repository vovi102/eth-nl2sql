"""S7, the quantization ablation (phase2-plan.md §5.4): fp16 replies of the base model and of the
base model with its adapter, checked on CPU with a tiny Qwen2 model and a random adapter.

Run from eth-nl2sql/:  uv run --project kaggle/phase2-train pytest kaggle/phase2-train/tests
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import train_lora
from conftest import REPO, TRAIN_DIR, need

DEV_PROMPTS = REPO / "data/prompts/dev.predictions.prompts.jsonl"
# Replies of llama-server (3B + LoRA e2, S6): what a reply of the fine-tuned model looks like.
Q4_REPLIES = (
    REPO / "data/predictions/dev.predictions.prompts.qwen2.5-coder-3b-instruct-q4_k_m.3b-e2.jsonl"
)  # noqa: E501


def sql_prompts(n: int | None = None) -> list[dict]:
    need(DEV_PROMPTS)
    rows = [json.loads(line) for line in DEV_PROMPTS.read_text(encoding="utf-8").splitlines()]
    sql = [{"question_id": r["question_id"], "messages": r["messages"]} for r in rows
           if r["decision"] == "sql"]  # fmt: skip
    return sql[:n] if n else sql


@pytest.fixture(scope="module")
def tiny_adapter(tiny_base: Path, tmp_path_factory) -> Path:
    """A LoRA adapter of the frozen configuration with random A and B: PEFT starts B at zero,
    which would leave the model as it is."""
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM

    lora = json.loads((TRAIN_DIR / "config.json").read_text())["lora"]
    torch.manual_seed(1)
    model = get_peft_model(
        AutoModelForCausalLM.from_pretrained(tiny_base),
        LoraConfig(r=lora["r"], lora_alpha=lora["alpha"], target_modules=lora["target_modules"],
                   init_lora_weights=False, task_type="CAUSAL_LM"),
    )  # fmt: skip
    out = tmp_path_factory.mktemp("adapters") / "tiny-e2"
    model.save_pretrained(out)
    return out


def greedy_by_hand(model, tokenizer, messages: list[dict], n: int) -> list[int]:
    """Argmax of the next token, n times or until an end-of-generation token, no cache."""
    import torch

    eos = set(tokenizer.convert_tokens_to_ids(["<|im_end|>", "<|endoftext|>"]))
    ids = tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt",
                                        return_dict=True)["input_ids"]  # fmt: skip
    out = []
    with torch.no_grad():
        for _ in range(n):
            token = int(model(input_ids=ids, logits_to_keep=1).logits[0, -1].argmax())
            out.append(token)
            if token in eos:
                break
            ids = torch.cat([ids, torch.tensor([[token]])], dim=1)
    return out


def test_replies_are_greedy_without_and_with_the_adapter(tiny_base: Path, tiny_adapter: Path,
                                                         tmp_path: Path) -> None:  # fmt: skip
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    prompts = sql_prompts(2)
    prompts_file = tmp_path / "dev-prompts.jsonl"
    prompts_file.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in prompts))
    out = tmp_path / "out"
    argv = ["generate", "--model", "3b", "--base", str(tiny_base), "--adapter", str(tiny_adapter),
            "--adapter-name", "tiny-e2", "--prompts", str(prompts_file), "--out", str(out),
            "--max-new-tokens", "6", "--cpu"]  # fmt: skip
    assert train_lora.main(argv) == 0

    tokenizer = AutoTokenizer.from_pretrained(tiny_base)
    base = AutoModelForCausalLM.from_pretrained(tiny_base).eval()
    expected = {"qwen2.5-coder-3b-instruct-fp32": base}
    tuned = PeftModel.from_pretrained(AutoModelForCausalLM.from_pretrained(tiny_base),
                                      tiny_adapter).eval()  # fmt: skip
    expected["qwen2.5-coder-3b-instruct-fp32.tiny-e2"] = tuned
    replies = {}
    for name, model in expected.items():
        text = (out / f"replies.{name}.jsonl").read_text()
        rows = [json.loads(line) for line in text.splitlines()]
        assert [r["question_id"] for r in rows] == [p["question_id"] for p in prompts]
        for row, prompt in zip(rows, prompts, strict=True):
            assert row["messages_sha256"] == train_lora.messages_key(prompt["messages"])
            tokens = greedy_by_hand(model, tokenizer, prompt["messages"], 6)
            stop = tokens[-1] in (151645, 151643)
            assert row["finish"] == ("stop" if stop else "length")
            assert row["reply"] == tokenizer.decode(tokens[:-1] if stop else tokens)
            assert row["completion_tokens"] == len(tokens) - stop
        meta = json.loads((out / f"replies.{name}.jsonl.meta.json").read_text())
        assert meta["model"] == name and meta["summary"]["prompts"] == 2
        assert meta["generation"]["do_sample"] is False
        assert meta["generation"]["repetition_penalty"] == 1.0
        assert meta["generation"]["max_new_tokens"] == 6
        assert meta["replies_sha256"] == train_lora.sha256(out / f"replies.{name}.jsonl")
        replies[name] = [r["reply"] for r in rows]
    adapter_sha = train_lora.sha256(tiny_adapter / "adapter_model.safetensors")
    assert meta["adapter"] == {"name": "tiny-e2", "sha256": adapter_sha}
    assert len(set(map(tuple, replies.values()))) == 2  # the adapter changes the replies
    run = json.loads((out / "generate.json").read_text())
    assert sorted(run["runs"]) == sorted(expected) and run["attention"] is None
    assert run["inputs"]["prompts_sha256"] == train_lora.sha256(prompts_file)


def test_decoding_gives_back_the_reply_text(tokenizer) -> None:
    """decode(encode(reply)) is the reply: the text of a fp16 reply is what llama-server would
    give for the same tokens."""
    need(Q4_REPLIES)
    for line in Q4_REPLIES.read_text(encoding="utf-8").splitlines():
        for attempt in json.loads(line)["attempts"]:
            text = attempt["reply"]
            assert tokenizer.decode(tokenizer.encode(text, add_special_tokens=False)) == text


def test_prepare_dev_prompts_holds_only_the_sql_prompts_of_dev(tmp_path: Path) -> None:
    need(DEV_PROMPTS)
    assert train_lora.main(["prepare-dev-prompts", "--out", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dataset-metadata.json",
                                                          "dev-prompts.jsonl"]  # fmt: skip
    rows = [json.loads(line) for line in (tmp_path / "dev-prompts.jsonl").read_text().splitlines()]
    assert rows == sql_prompts() and len(rows) == 161
    meta = json.loads((tmp_path / "dataset-metadata.json").read_text())
    assert meta["id"] == "aohkvd/eth-nl2sql-phase2-dev-prompts"
    test_named = tmp_path / "test.predictions.prompts.jsonl"
    test_named.write_text("")
    with pytest.raises(SystemExit):
        train_lora.main(["prepare-dev-prompts", "--prompts", str(test_named),
                         "--out", str(tmp_path / "x")])  # fmt: skip


def test_prepare_generate_bakes_the_inputs(tiny_adapter: Path, tmp_path: Path) -> None:
    prompts = tmp_path / "dev-prompts.jsonl"
    prompts.write_text('{"question_id": "q00001", "messages": []}\n')
    adapter_sha = train_lora.sha256(tiny_adapter / "adapter_model.safetensors")
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"model": {"key": "3b"}, "script_sha256": "abc",
                               "finished": "2026-11-08T10:09:32+0000",
                               "checkpoints": [{"name": "tiny-e2",
                                                "adapter_sha256": adapter_sha}]}))  # fmt: skip
    out = tmp_path / "kernel"
    argv = ["prepare-generate", "--model", "3b", "--adapter", str(tiny_adapter), "--run", str(run),
            "--prompts", str(prompts), "--out", str(out)]  # fmt: skip
    assert train_lora.main(argv) == 0
    meta = json.loads((out / "kernel-metadata.json").read_text())
    assert meta["id"] == "aohkvd/eth-nl2sql-phase2-fp16" and meta["is_private"] == "true"
    assert meta["dataset_sources"] == ["aohkvd/eth-nl2sql-phase2-dev-prompts"]
    assert meta["kernel_sources"] == ["aohkvd/eth-nl2sql-phase2-train"]
    spec = importlib.util.spec_from_file_location("kernel", out / "train_lora.py")
    kernel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kernel)
    assert kernel.KERNEL["argv"] == ["generate", "--model", "3b", "--adapter-name", "tiny-e2",
                                     "--install"]  # fmt: skip
    assert kernel.KERNEL["inputs"] == {"prompts_sha256": train_lora.sha256(prompts),
                                       "adapter_sha256": adapter_sha}  # fmt: skip
    with pytest.raises(SystemExit):  # an adapter that is not a checkpoint of the run
        train_lora.main([*argv[:2], "1.5b", *argv[3:]])
