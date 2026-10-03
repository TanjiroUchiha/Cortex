import argparse
import hashlib
import importlib
import importlib.util
import inspect
import json
import re
from importlib.metadata import version
from pathlib import Path

from dataset import DEFAULT_DATA, load_records, tokenize_record
from m1 import SYSTEM_PROMPT, build_messages, parse_decision

BASE_MODEL = "Qwen/Qwen3-4B"


def preflight():
    torch = importlib.import_module("torch")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable. Prepare data here; run QLoRA later on a compatible GPU. No model was downloaded.")
    if importlib.util.find_spec("bitsandbytes") is None:
        raise RuntimeError("bitsandbytes is missing. Use an isolated, compatible GPU training environment.")
    try:
        transformers = importlib.import_module("transformers")
        peft = importlib.import_module("peft")
        importlib.import_module("bitsandbytes")
        arguments = inspect.signature(transformers.TrainingArguments.__init__).parameters
        trainer = inspect.signature(transformers.Trainer.__init__).parameters
        if "eval_strategy" not in arguments or "processing_class" not in trainer:
            raise RuntimeError("Training APIs do not match this pipeline; use a compatible Transformers version.")
        for name in ("prepare_model_for_kbit_training", "get_peft_model", "LoraConfig"):
            getattr(peft, name)
    except Exception as exc:
        raise RuntimeError(f"Training dependency preflight failed ({type(exc).__name__}). Repair an isolated environment, not global Python.") from exc
    return {"gpu": torch.cuda.get_device_name(0), "vram_gib": round(torch.cuda.get_device_properties(0).total_memory / 2**30, 2),
            "packages": {name: version(name) for name in ("torch", "transformers", "peft", "accelerate", "bitsandbytes")}}


def training_model(revision):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype)
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, revision=revision, trust_remote_code=False)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL, revision=revision, trust_remote_code=False,
                                               quantization_config=quantization, device_map={"": 0}, torch_dtype=dtype)
    return model, tokenizer, dtype


def validate_training_args(args):
    if args.revision is None or re.fullmatch(r"[0-9a-f]{40}", args.revision) is None:
        raise ValueError("Supply --revision with the verified 40-character Hugging Face commit hash for Qwen/Qwen3-4B")
    if not args.reviewed_data:
        raise ValueError("Review the examples, then explicitly acknowledge review using --reviewed-data")
    if not 256 <= args.max_length <= 4096 or not 1 <= args.epochs <= 5 or args.max_steps == 0 or args.max_steps < -1:
        raise ValueError("Use max-length 256..4096, epochs 1..5, and positive max-steps or -1")
    if args.output.exists() or not args.output.parent.is_dir():
        raise ValueError("Choose a new output directory under an existing parent; existing outputs are never overwritten")


def train(args):
    validate_training_args(args)
    records = load_records(args.data)
    environment = preflight()
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import DataCollatorForSeq2Seq, Trainer, TrainingArguments, set_seed

    set_seed(42)
    model, tokenizer, dtype = training_model(args.revision)
    tokenized = [(row["split"], tokenize_record(row, tokenizer, args.max_length)) for row in records if row["split"] in ("train", "eval")]
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(r=8, lora_alpha=16, lora_dropout=0.05,
                                           target_modules=["q_proj", "k_proj", "v_proj", "o_proj"], bias="none", task_type="CAUSAL_LM"))
    settings = TrainingArguments(
        output_dir=str(args.output), num_train_epochs=args.epochs, max_steps=args.max_steps,
        per_device_train_batch_size=1, per_device_eval_batch_size=1, gradient_accumulation_steps=8,
        gradient_checkpointing=True, learning_rate=1e-4, warmup_ratio=0.1,
        fp16=dtype == torch.float16, bf16=dtype == torch.bfloat16,
        optim="adamw_torch", logging_steps=1, eval_strategy="epoch", save_strategy="epoch",
        save_total_limit=2, report_to="none", push_to_hub=False, seed=42, data_seed=42,
        dataloader_num_workers=0, eval_accumulation_steps=1,
    )
    args.output.mkdir(exist_ok=False)
    manifest = {"base_model": BASE_MODEL, "revision": args.revision, "seed": 42, "max_length": args.max_length,
                "epochs": args.epochs, "max_steps": args.max_steps, "dataset_sha256": hashlib.sha256(args.data.read_bytes()).hexdigest(),
                "review_acknowledged": args.reviewed_data, "unreviewed_record_flags": sum(not row["reviewed"] for row in records),
                "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                "environment": environment, "status": "started"}
    manifest_path = args.output / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    trainer = Trainer(
        model=model, args=settings, processing_class=tokenizer,
        train_dataset=[item for split, item in tokenized if split == "train"],
        eval_dataset=[item for split, item in tokenized if split == "eval"],
        data_collator=DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=-100, return_tensors="pt"),
    )
    trainer.train()
    metrics = trainer.evaluate()
    adapter_path = args.output / "adapter"
    trainer.save_model(str(adapter_path))
    tokenizer.save_pretrained(str(adapter_path))
    manifest.update(status="completed", metrics=metrics)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (adapter_path / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Saved adapter to {adapter_path}. Evaluate routing predictions before use; eval loss alone is not routing quality.")


class HFRouter:
    def __init__(self, revision=None, adapter_path=None):
        self.max_length = 2048
        if adapter_path:
            adapter_path = Path(adapter_path)
            manifest = json.loads((adapter_path / "run_manifest.json").read_text(encoding="utf-8"))
            if manifest.get("base_model") != BASE_MODEL or manifest.get("status") != "completed":
                raise ValueError("Adapter manifest is incompatible or incomplete")
            if manifest.get("prompt_sha256") != hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest():
                raise ValueError("Prompt/schema differs from training; use the matching source revision")
            revision = manifest["revision"]
            self.max_length = manifest["max_length"]
        if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError("Pinned Qwen3-4B revision required")
        preflight()
        self.revision = revision
        self.model, self.tokenizer, _ = training_model(revision)
        if adapter_path:
            from peft import PeftModel
            from transformers import AutoTokenizer
            self.tokenizer = AutoTokenizer.from_pretrained(str(adapter_path), local_files_only=True, trust_remote_code=False)
            self.model = PeftModel.from_pretrained(self.model, str(adapter_path), is_trainable=False, local_files_only=True)
        self.model.eval()
        self.model.config.use_cache = True

    def raw(self, request):
        import torch
        prompt = self.tokenizer.apply_chat_template(build_messages(request), tokenize=False, add_generation_prompt=True, enable_thinking=False)
        inputs = self.tokenizer(prompt, add_special_tokens=False, return_tensors="pt").to(self.model.device)
        if inputs["input_ids"].shape[-1] + 512 > self.max_length:
            raise ValueError("Request exceeds inference budget; shorten the request")
        with torch.inference_mode():
            output = self.model.generate(**inputs, do_sample=False, max_new_tokens=512, pad_token_id=self.tokenizer.eos_token_id)
        generated = output[0, inputs["input_ids"].shape[-1]:]
        if self.tokenizer.eos_token_id not in generated.tolist():
            raise ValueError("Output did not terminate within its token budget")
        return self.tokenizer.decode(generated, skip_special_tokens=True).strip()

    def __call__(self, request):
        return parse_decision(self.raw(request), request)


def adapter_route(request, adapter_path):
    return HFRouter(adapter_path=adapter_path)(request)


def main():
    parser = argparse.ArgumentParser(description="Deferred Cortex M1 QLoRA training; no automatic cloud use")
    parser.add_argument("--check", action="store_true", help="Check training dependencies and GPU without downloading weights")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=Path("m1-training-run"))
    parser.add_argument("--revision")
    parser.add_argument("--reviewed-data", action="store_true")
    parser.add_argument("--max-length", type=int, default=2048)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=-1)
    args = parser.parse_args()
    try:
        if args.check:
            print(json.dumps(preflight(), indent=2))
        else:
            train(args)
    except (ValueError, RuntimeError, ImportError, OSError) as exc:
        parser.exit(1, f"Training not completed: {exc}\n")


if __name__ == "__main__":
    main()
