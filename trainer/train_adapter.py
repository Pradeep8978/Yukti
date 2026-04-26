"""Trainer CLI: fine-tune a small adapter (LoRA) on exported JSONL.

This file provides a pragmatic, runnable CLI for prototyping LoRA-style
adapters. It's intentionally conservative: PEFT/bitsandbytes are optional
and only used if available and requested.

Recommended workflow:
  1. Create dataset: python scripts/export_training_data.py --out data/training/journal_export.jsonl
  2. Install trainer deps: pip install -r trainer/requirements.txt
  3. Train: python trainer/train_adapter.py --data data/training/journal_export.jsonl --model facebook/opt-125m --out_dir models/lora-journal --use_peft auto

Adjust hyperparams for your GPU / cloud environment.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Dict, List

from datasets import load_dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    TrainingArguments,
    Trainer,
    set_seed,
)

try:
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
except Exception:  # pragma: no cover - optional dependency
    LoraConfig = get_peft_model = prepare_model_for_kbit_training = None

LOG = logging.getLogger("trainer.train_adapter")


def build_examples(batch: Dict[str, List], tokenizer, max_input_length: int, max_target_length: int):
    inputs: List[List[int]] = []
    labels: List[List[int]] = []
    prompts = batch.get("prompt", [])
    targets = batch.get("target", [])
    for p, t in zip(prompts, targets):
        tgt_text = json.dumps(t, ensure_ascii=False)
        input_text = "CONTEXT:\n" + (p or "") + "\n\nRESPONSE: "
        full = input_text + tgt_text
        tokenized_full = tokenizer(full, truncation=True, max_length=max_input_length + max_target_length)
        full_ids = tokenized_full["input_ids"]
        prompt_ids = tokenizer(input_text, truncation=True, max_length=max_input_length)["input_ids"]
        labels_ids = full_ids.copy()
        # mask prompt tokens in labels
        for i in range(min(len(prompt_ids), len(labels_ids))):
            labels_ids[i] = -100
        inputs.append(full_ids)
        labels.append(labels_ids)
    return {"input_ids": inputs, "labels": labels}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="Path to JSONL exported dataset")
    parser.add_argument("--val_data", default=None, help="Optional validation JSONL")
    parser.add_argument("--model", default="facebook/opt-125m")
    parser.add_argument("--out_dir", default="models/lora")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch_size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--dry_run", action="store_true", dest="dry_run", help="Tokenize and exit without training")
    parser.add_argument("--min_journals", type=int, default=50, help="Minimum number of quality journals required to start training")
    parser.add_argument("--min_quality", type=int, default=6, help="Minimum quality score to count a journal")
    parser.add_argument("--use_peft", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--max_input_length", type=int, default=1024)
    parser.add_argument("--max_target_length", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fp16", action="store_true")
    parser.add_argument("--gradient_checkpointing", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    set_seed(args.seed)
    os.makedirs(args.out_dir, exist_ok=True)

    LOG.info("Loading dataset from %s", args.data)
    ds = load_dataset("json", data_files={"train": args.data})
    # Optional safety: ensure enough quality journals before training
    if not args.dry_run and args.min_journals and args.min_journals > 0:
        try:
            # load as iterable to count quality scores if present
            records = list(ds["train"])
            kval_count = 0
            for r in records:
                q = None
                if isinstance(r, dict):
                    q = r.get("quality_score") or (r.get("target") or {}).get("quality_score")
                try:
                    if q is not None and float(q) >= float(args.min_quality):
                        kval_count += 1
                except Exception:
                    continue

            LOG.info("Dataset quality check: %d journals with quality >= %s", kval_count, args.min_quality)
            if kval_count < args.min_journals:
                LOG.error("Not enough high-quality journals for training: required=%d found=%d", args.min_journals, kval_count)
                raise SystemExit(1)
        except Exception:
            LOG.exception("Failed to validate dataset quality; aborting to avoid accidental training on poor data")
            raise
    if args.val_data:
        ds_val = load_dataset("json", data_files={"validation": args.val_data})
    else:
        ds_val = None

    tokenizer = AutoTokenizer.from_pretrained(args.model, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def preprocess(batch):
        return build_examples(batch, tokenizer, max_input_length=args.max_input_length, max_target_length=args.max_target_length)

    tokenized = ds["train"].map(
        preprocess,
        batched=True,
        remove_columns=ds["train"].column_names,
    )

    if ds_val is not None:
        tokenized_val = ds_val["validation"].map(
            preprocess,
            batched=True,
            remove_columns=ds_val["validation"].column_names,
        )
    else:
        tokenized_val = None

    if args.dry_run:
        try:
            train_count = len(tokenized)
        except Exception:
            train_count = None
        try:
            val_count = len(tokenized_val) if tokenized_val is not None else 0
        except Exception:
            val_count = None

        summary = {"train_count": train_count, "val_count": val_count}
        summary_path = os.path.join(args.out_dir, "ci_preprocess_summary.json")
        os.makedirs(args.out_dir, exist_ok=True)
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh)
        LOG.info("Dry-run complete: wrote %s", summary_path)
        print(f"Dry-run complete: train={train_count} val={val_count} summary={summary_path}")
        return

    LOG.info("Loading model %s", args.model)
    model = AutoModelForCausalLM.from_pretrained(args.model, trust_remote_code=True)
    if args.gradient_checkpointing:
        try:
            model.gradient_checkpointing_enable()
        except Exception:
            LOG.warning("Gradient checkpointing not supported for this model")

    will_use_peft = False
    if args.use_peft == "on":
        will_use_peft = True
    elif args.use_peft == "auto" and get_peft_model is not None:
        will_use_peft = True

    if will_use_peft:
        if prepare_model_for_kbit_training is not None:
            try:
                model = prepare_model_for_kbit_training(model)
            except Exception:
                LOG.warning("prepare_model_for_kbit_training failed; continuing without kbit optimization")

        if get_peft_model is not None:
            peft_config = LoraConfig(
                r=8,
                lora_alpha=32,
                target_modules=["q_proj", "v_proj"],
                lora_dropout=0.05,
                bias="none",
                task_type="CAUSAL_LM",
            )
            model = get_peft_model(model, peft_config)
        else:
            LOG.warning("PEFT requested but `peft` not installed; proceeding without adapters")

    data_collator = DataCollatorForLanguageModeling(tokenizer, mlm=False)

    training_args = TrainingArguments(
        output_dir=args.out_dir,
        per_device_train_batch_size=args.batch_size,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        fp16=args.fp16,
        logging_steps=50,
        save_total_limit=3,
        remove_unused_columns=False,
        evaluation_strategy="epoch" if tokenized_val is not None else "no",
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized,
        eval_dataset=tokenized_val,
        data_collator=data_collator,
        tokenizer=tokenizer,
    )

    LOG.info("Starting training")
    trainer.train()

    LOG.info("Saving model to %s", args.out_dir)
    os.makedirs(args.out_dir, exist_ok=True)
    meta = {
        "base_model": args.model,
        "use_peft": will_use_peft,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "lr": args.lr,
        "seed": args.seed,
    }
    try:
        with open(os.path.join(args.out_dir, "train_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
    except Exception:
        LOG.warning("Could not write train_meta.json")

    # Try saving in a few ways depending on whether PEFT is in use
    saved = False
    try:
        model.save_pretrained(args.out_dir)
        saved = True
        LOG.info("Model saved via model.save_pretrained() to %s", args.out_dir)
    except Exception:
        LOG.warning("model.save_pretrained() failed; attempting trainer.save_model()")
        try:
            trainer.save_model(args.out_dir)
            saved = True
            LOG.info("Model saved via trainer.save_model() to %s", args.out_dir)
        except Exception:
            LOG.exception("trainer.save_model() failed; attempting PEFT-specific save if available")
            try:
                # PEFT models usually implement save_pretrained as well
                if hasattr(model, "save_pretrained"):
                    model.save_pretrained(args.out_dir)
                    saved = True
                    LOG.info("Model saved via model.save_pretrained() (PEFT path) to %s", args.out_dir)
            except Exception:
                LOG.exception("PEFT save attempt also failed")

    if not saved:
        LOG.error("All save attempts failed; check trainer state and disk permissions")


if __name__ == "__main__":
    main()
