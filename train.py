import transformers
import json
import glob
from collections import namedtuple
import eval
import os
import data
import helpers
import torch
import wandb
from peft import get_peft_model, PeftModel
from peft.tuners.lora import LoraLayer, LoraConfig
from torch.utils.data import DataLoader
from transformers import get_scheduler
from tqdm import tqdm
from typing import Optional
import argparse
from dataclasses import dataclass, field, asdict
import bitsandbytes as bnb

grouped_question_metadata = None
grouped_model_inputs = None

OUTPUT_DIR = "/home/ubuntu/fuyu/output"


@dataclass
class Config:
    model_name_or_path: str = field(default="adept/fuyu-8b")
    per_device_batch_size: int = field(default=2)
    learning_rate: float = field(default=3e-4)
    scheduler_type: str = field(default="constant")
    warmup_steps: int = field(default=200)
    lora: bool = field(default=True)
    lora_r: int = field(default=32)
    lora_alpha: int = field(default=32)
    lora_vision: bool = field(default=False)
    use_8bit_optimizer: bool = field(default=False)
    gradient_accumulation_steps: int = field(default=1)
    max_eval_steps: Optional[int] = field(default=None)
    train_on_questions: bool = field(default=False)
    run_name: Optional[str] = field(default=None)
    save_every_steps: int = field(default=500)
    eval_every_steps: int = field(default=500)
    weight_decay: float = field(default=0.0)
    do_vocab_surgery: bool = field(default=True)
    seed: Optional[int] = field(default=None)


Data = namedtuple(
    "Data",
    ["train_dataloader", "eval_dataloader", "data_collator", "auto_eval_dataloader"],
)


def get_lora_model(model, checkpoint_dir: Optional[str], config: Config):
    if checkpoint_dir is not None:
        model = PeftModel.from_pretrained(
            model, os.path.join(checkpoint_dir, "adapter_model")
        )
    else:
        lora_module_names = set()
        for name, module in model.named_modules():
            if isinstance(module, torch.nn.Linear):
                names = name.split(".")
                lora_module_names.add(names[0] if len(names) == 1 else names[-1])

        lora_module_names.remove("lm_head")
        if config.lora_vision:
            lora_module_names.remove("vision_embed_tokens")
        lora_config = LoraConfig(
            r=config.lora_r,
            lora_alpha=config.lora_alpha,
            target_modules=list(lora_module_names),
            lora_dropout=0.0,
            bias="none",
            task_type="CAUSAL_LM",
        )

        model = get_peft_model(model, lora_config)

    for name, module in model.named_modules():
        if isinstance(module, LoraLayer):
            module = module.to(torch.bfloat16)
        if "norm" in name:
            module = module.to(torch.float32)
        if "lm_head" in name or "embed_tokens" in name:
            if hasattr(module, "weight"):
                if module.weight.dtype == torch.float32:
                    module = module.to(torch.bfloat16)
    return model


def get_data(config: Config, tokenizer):
    vocab = tokenizer.get_vocab()
    tokenizer.get_vocab = lambda: vocab

    processor = transformers.FuyuProcessor(
        image_processor=transformers.FuyuImageProcessor(debug=False),
        tokenizer=tokenizer,
    )
    processor.max_tokens_to_generate = 0
    instruction = "Answer the following question tersely:"
    full_ds = data.AI2DDataset("/home/ubuntu/ai2d", processor, instruction)
    train_dataset, eval_dataset, _, eval_question_ids = full_ds.split(0.99)
    dataset_for_auto_eval = data.AI2DDatasetForAutoEval(
        "/home/ubuntu/ai2d", processor, eval_question_ids, instruction
    )
    data_collator = data.DataCollatorForMultimodal(pad_token_id=0)
    train_dataloader = DataLoader(
        train_dataset,
        shuffle=True,
        collate_fn=data_collator,
        batch_size=config.per_device_batch_size,
        pin_memory=True,
        num_workers=min(config.per_device_batch_size, 8),
        worker_init_fn=helpers.seed_worker,
    )
    eval_batch_size = 4
    eval_dataloader = DataLoader(
        eval_dataset,
        shuffle=True,
        collate_fn=data_collator,
        batch_size=eval_batch_size,
        pin_memory=True,
        worker_init_fn=helpers.seed_worker,
    )
    auto_eval_dataloader = DataLoader(
        dataset_for_auto_eval,
        batch_size=eval_batch_size,
        shuffle=False,
        collate_fn=data_collator,
        pin_memory=True,
        worker_init_fn=helpers.seed_worker,
    )
    return Data(train_dataloader, eval_dataloader, data_collator, auto_eval_dataloader)


def save_model(step, model, is_lora):
    assert wandb.run is not None
    checkpoint_folder = f"/home/ubuntu/fuyu/output/{wandb.run.name}/step-{step}"
    if is_lora:
        model_path = os.path.join(checkpoint_folder, "adapter_model")
    else:
        model_path = os.path.join(checkpoint_folder, "pytorch_model.bin")
    model.save_pretrained(model_path)


def get_checkpoint_dir(run_name: str) -> str:
    run_dir = f"/{run_name}/"
    paths = glob.glob(os.path.join(run_dir, "step-*"))
    steps = [p.split("-")[-1] for p in paths]
    if "final" in steps:
        checkpoint_dir = os.path.join(run_dir, "step-final")
    else:
        step = max([int(s) for s in steps])
        checkpoint_dir = os.path.join(run_dir, f"step-{step}")
    return checkpoint_dir


def load_model(config: Config, device="cuda:0"):
    model = transformers.FuyuForCausalLM.from_pretrained(
        config.model_name_or_path, device_map=device, torch_dtype=torch.bfloat16
    )
    tokenizer = transformers.AutoTokenizer.from_pretrained("adept/fuyu-8b")
    if config.do_vocab_surgery:
        model, tokenizer = helpers.vocab_surgery(model, tokenizer)
    checkpoint_dir = None
    if config.run_name is not None:
        checkpoint_dir = get_checkpoint_dir(config.run_name)
    model.gradient_checkpointing_enable()
    model.language_model.model.gradient_checkpointing_enable()
    if config.lora:
        model = get_lora_model(model, checkpoint_dir, config)
    elif config.run_name is not None:
        raise NotImplementedError("Resuming non-finetune runs not yet implemented.")
    return model, tokenizer


def load_config(run_name: str):
    with open(f"/{OUTPUT_DIR}/{run_name}/config.json", "r") as f:
        config = json.loads(f.read())
    return config


def save_config(config: Config, run_name: str):
    run_dir = f"/{OUTPUT_DIR}/{run_name}"
    if not os.path.exists(run_dir):
        os.makedirs(run_dir)
        with open(os.path.join(run_dir, "config.json"), "w") as f:
            f.write(json.dumps(asdict(config)))


def train(
    model,
    tokenizer,
    config: Config,
):
    data = get_data(config, tokenizer)
    max_train_steps = len(data.train_dataloader)
    opt_params = [p for n, p in model.named_parameters() if "lora" in n]
    if config.lora:
        opt_group_params = [
            {
                "params": opt_params,
                "weight_decay": 0.0,
            },
        ]
    else:
        # todo consider (variable) weight decay
        opt_group_params = [
            {
                "params": model.parameters(),
                "weight_decay": 0.0,
            }
        ]
    if config.use_8bit_optimizer:
        optimizer = bnb.optim.AdamW8bit(
            opt_group_params, betas=(0.9, 0.95), lr=config.learning_rate
        )
    else:
        optimizer = torch.optim.AdamW(
            opt_group_params, betas=(0.9, 0.95), lr=config.learning_rate
        )

    lr_scheduler = get_scheduler(
        name=config.scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=config.warmup_steps * config.gradient_accumulation_steps,
        num_training_steps=max_train_steps * config.gradient_accumulation_steps,
    )

    wandb.init(project="fuyu", config=config.__dict__)
    if wandb.run is None:
        raise Exception
    save_config(config, wandb.run.name)
    model = model.train()
    for step, batch in enumerate(tqdm(data.train_dataloader)):
        cleaned_batch = helpers.clean(batch, fdtype=torch.bfloat16)
        with torch.autocast("cuda"):
            loss = model(**cleaned_batch).loss
        wandb.log({"step": step, "loss/train": loss})
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        lr_scheduler.step()
        optimizer.zero_grad()
        if (step + 1) % config.save_every_steps == 0:
            save_model(step + 1, model, config.lora)
        if (step + 1) % config.eval_every_steps == 0:
            accuracy, eval_loss = eval.do_auto_eval(
                model, None, data.auto_eval_dataloader
            )
            wandb.log(
                {"step": step + 1, "accuracy/val": accuracy, "loss/val": eval_loss}
            )
    save_model("final", model, config.lora)


def main():
    parser = argparse.ArgumentParser(description="Training Configuration")
    for field_name, field_value in asdict(Config()).items():
        if isinstance(field_value, bool) and field_value is False:
            parser.add_argument(f"--{field_name}", action="store_true")
        else:
            parser.add_argument(
                f"--{field_name}", type=type(field_value), default=field_value
            )

    args = parser.parse_args()
    config = Config(
        model_name_or_path=args.model_name_or_path,
        per_device_batch_size=args.per_device_batch_size,
        learning_rate=args.learning_rate,
        scheduler_type=args.scheduler_type,
        warmup_steps=args.warmup_steps,
        lora=args.lora,
        lora_r=args.lora_r,
        lora_alpha=args.lora_alpha,
        use_8bit_optimizer=args.use_8bit_optimizer,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        max_eval_steps=args.max_eval_steps,
        train_on_questions=args.train_on_questions,
        run_name=args.run_name,
        save_every_steps=args.save_every_steps,
        eval_every_steps=args.eval_every_steps,
        weight_decay=args.weight_decay,
        do_vocab_surgery=args.do_vocab_surgery,
        lora_vision=args.lora_vision,
        seed=args.seed,
    )
    if config.run_name is not None:
        config = load_config(config.run_name)
    seed = helpers.enforce_reproducibility(config.seed)
    config.seed = seed
    model, tokenizer = load_model(config)
    print("Loaded model.")
    train(model, tokenizer, config)


if __name__ == "__main__":
    main()
