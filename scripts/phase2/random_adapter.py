"""A random PEFT LoRA adapter with the exact names and shapes of the real 1.5B one (config.json of
the pinned revision, the frozen LoRA config), without the base weights: the model is built on
the meta device and only the LoRA tensors are drawn. For a local runtime check before any GPU
training: convert_lora_to_gguf.py, llama-server --lora-scaled, generate-sql --adapter.

usage: python random_adapter.py BASE_CONFIG_DIR TRAIN_CONFIG_JSON OUT_DIR
"""

import json
import sys
from pathlib import Path

import torch
from peft import LoraConfig, get_peft_model
from safetensors.torch import save_file
from transformers import AutoConfig, AutoModelForCausalLM

base_dir, train_config, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
lora = json.loads(train_config.read_text())["lora"]
config = AutoConfig.from_pretrained(base_dir)
with torch.device("meta"):
    model = AutoModelForCausalLM.from_config(config)
peft_config = LoraConfig(
    r=lora["r"],
    lora_alpha=lora["alpha"],
    lora_dropout=lora["dropout"],
    target_modules=lora["target_modules"],
    bias="none",
    task_type="CAUSAL_LM",
)
peft_model = get_peft_model(model, peft_config)
generator = torch.Generator().manual_seed(0)
tensors = {}
for name, param in peft_model.named_parameters():
    if "lora_" not in name:
        continue
    key = name.replace(".default", "")
    tensors[key] = (torch.randn(param.shape, generator=generator) * 0.05).contiguous()
out.mkdir(parents=True, exist_ok=True)
save_file(tensors, out / "adapter_model.safetensors")
peft_config.base_model_name_or_path = None
peft_config.save_pretrained(out)
print(f"{len(tensors)} tensors, {sum(t.numel() for t in tensors.values()):,} parameters -> {out}")
print(sorted(tensors)[:2])
