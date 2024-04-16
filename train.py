# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# note: peter wu by 2024-03-08

"""
A minimal training script for DiT using PyTorch DDP.
"""
import argparse
import os
from functools import partial

import torch
import torch.distributed as dist
from colossalai import launch_from_torch
from colossalai.accelerator import get_accelerator
from colossalai.booster import Booster
from colossalai.cluster import DistCoordinator
from colossalai.logging import get_dist_logger
from colossalai.nn.lr_scheduler import CosineAnnealingLR
from colossalai.nn.optimizer import HybridAdam
from colossalai.utils import get_current_device
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from open_sora.diffusion import create_diffusion
from open_sora.modeling import DiT_models
from open_sora.modeling.dit import SUPPORTED_MODEL_ARCH, SUPPORTED_SEQ_PARALLEL_MODES
from open_sora.utils.data import (
    create_video_compressor,
    load_datasets,
    make_batch,
    preprocess_batch,
)
from open_sora.utils.plugin import ZeroSeqParallelPlugin

#################################################################################
#                             Training Helper Functions                         #
#################################################################################

# 配置torch的后端，主要是涉及到数学计算
def configure_backends():
    # the first flag below was False when we tested this script but True makes A100 training a lot faster:
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True


@torch.no_grad()
def update_ema(ema_model, model, decay=0.9999):
    """
    Step the EMA model towards the current model.
    """

    for ema_p, p in zip(ema_model.parameters(), model.parameters()):
        ema_p.mul_(decay).add_(p.data, alpha=1 - decay)


def requires_grad(model, flag=True):
    """
    Set requires_grad flag for all parameters in a model.
    """
    for p in model.parameters():
        p.requires_grad = flag


def all_reduce_mean(tensor: torch.Tensor) -> torch.Tensor:
    dist.all_reduce(tensor=tensor, op=dist.ReduceOp.SUM)
    tensor.div_(dist.get_world_size())
    return tensor


def save_checkpoints(booster, model, optimizer, ema, save_path, coordinator):
    os.makedirs(save_path, exist_ok=True)
    booster.save_model(model, os.path.join(save_path, "model"), shard=False)
    booster.save_optimizer(optimizer, os.path.join(save_path, "optimizer"), shard=True)
    if coordinator.is_master():
        ema_state_dict = ema.state_dict()
        for k, v in ema_state_dict.items():
            ema_state_dict[k] = v.cpu()
        torch.save(ema_state_dict, os.path.join(save_path, "ema.pt"))
    dist.barrier()


#################################################################################
#                                  Training Loop                                #
#################################################################################


def main(args):
    """
    Trains a new DiT model.
    """
    # Step 1: init distributed environment
    # colossal ai启动torch分布式训练环境的函数
    launch_from_torch({})
    # colossal ai用于分布式环境的一些信息的存储
    coordinator = DistCoordinator()
    logger = get_dist_logger()
    # 配置torch后端的cuda等计算精度
    configure_backends()

    # Step 2: set up acceleration plugins
    plugin = ZeroSeqParallelPlugin(sp_size=args.sp_size, stage=2, precision="fp16")
    booster = Booster(plugin=plugin)

    if coordinator.is_master():
        os.makedirs(args.checkpoint_dir, exist_ok=True)
        os.makedirs(args.tensorboard_dir, exist_ok=True)
        writer = SummaryWriter(args.tensorboard_dir)

    # Step 3: Create video compressor
    video_compressor = create_video_compressor(args.compressor)
    model_kwargs = {
        "in_channels": video_compressor.out_channels,
        "seq_parallel_group": plugin.sp_group,
        "seq_parallel_mode": args.sp_mode,
        "seq_parallel_overlap": args.sp_overlap,
        "model_arch": args.model_arch,
    }

    # Step 4: Create DiT and EMA
    model = DiT_models[args.model](**model_kwargs).to(get_current_device())
    patch_size = model.patch_size
    ema = DiT_models[args.model](**model_kwargs).to(get_current_device())
    update_ema(ema, model, decay=0)
    requires_grad(ema, False)

    model.train()  # important! This enables embedding dropout for classifier-free guidance
    ema.eval()  # EMA model should always be in eval mode

    # configure gradient checkpointing
    if args.grad_checkpoint:
        model.enable_gradient_checkpointing()

    # Step 5: create diffusion pipeline
    diffusion = create_diffusion(
        timestep_respacing=""
    )  # default: 1000 steps, linear noise schedule

    # Step 6: setup optimizer (we used default Adam betas=(0.9, 0.999) and a constant learning rate of 1e-4 in our paper):
    opt = HybridAdam(model.parameters(), lr=args.lr, weight_decay=0)

    # Step 7: Setup dataloader
    dataset = load_datasets(args.dataset)
    dataloader = plugin.prepare_dataloader(
        dataset,
        batch_size=args.batch_size,
        collate_fn=partial(
            make_batch,
            video_dir=args.video_dir,
            pad_to_multiple=args.sp_size,
            use_pooled_text=args.model_arch == "adaln",
        ),
        shuffle=True,
        drop_last=True,
    )
    lr_scheduler = CosineAnnealingLR(
        opt, args.epochs * len(dataloader) // args.accumulation_steps
    )
    logger.info(f"Dataset contains {len(dataset)} samples", ranks=[0])

    # Step 8: setup booster
    model, opt, _, dataloader, lr_scheduler = booster.boost(
        model, opt, dataloader=dataloader, lr_scheduler=lr_scheduler
    )
    if args.load_model is not None:
        booster.load_model(model, args.load_model)
    if args.load_optimizer is not None:
        booster.load_optimizer(opt, args.load_optimizer)
    logger.info(
        f"Booster init max device memory: {get_accelerator().max_memory_allocated() / 1024 ** 2:.2f} MB",
        ranks=[0],
    )

    # Step 9: Train
    num_steps_per_epoch = len(dataloader) // args.accumulation_steps

    for epoch in range(args.epochs):
        dataloader.sampler.set_epoch(epoch)
        with tqdm(
            desc=f"Epoch {epoch}",
            disable=not coordinator.is_master(),
            total=num_steps_per_epoch,
        ) as pbar:
            total_loss = torch.tensor(0.0, device=get_current_device())
            for step, batch in enumerate(dataloader):
                batch = preprocess_batch(
                    batch,
                    patch_size,
                    video_compressor,
                    pad_to_multiple=args.sp_size,
                    model_arch=args.model_arch,
                )
                video_inputs = batch.pop("video_latent_states")
                mask = batch.pop("video_padding_mask")
                t = torch.randint(
                    0,
                    diffusion.num_timesteps,
                    (video_inputs.shape[0],),
                    device=video_inputs.device,
                )
                loss_dict = diffusion.training_losses(
                    model, video_inputs, t, batch, mask=mask
                )
                loss = loss_dict["loss"].mean() / args.accumulation_steps
                total_loss.add_(loss.data)
                booster.backward(loss, opt)

                if (step + 1) % args.accumulation_steps == 0:
                    opt.step()
                    opt.zero_grad()
                    lr_scheduler.step()
                    update_ema(ema, model)

                    all_reduce_mean(total_loss)
                    pbar.set_postfix({"Loss": f"{total_loss.item():.4f}"})
                    if coordinator.is_master():
                        global_step = (epoch * num_steps_per_epoch) + (
                            step + 1
                        ) // args.accumulation_steps
                        writer.add_scalar(
                            tag="Loss",
                            scalar_value=total_loss.item(),
                            global_step=global_step,
                        )
                    pbar.update()
                    total_loss.zero_()

                # Save DiT checkpoint:
                if args.save_interval > 0 and (
                    (step + 1) % (args.save_interval * args.accumulation_steps) == 0
                    or (step + 1) == len(dataloader)
                ):
                    save_path = os.path.join(
                        args.checkpoint_dir, f"epoch-{epoch}-step-{step}"
                    )
                    save_checkpoints(booster, model, opt, ema, save_path, coordinator)
                    logger.info(f"Saved checkpoint to {save_path}", ranks=[0])

                get_accelerator().empty_cache()
    final_save_path = os.path.join(args.checkpoint_dir, "final")
    save_checkpoints(booster, model, opt, ema, final_save_path, coordinator)
    logger.info(f"Saved checkpoint to {final_save_path}", ranks=[0])
    logger.info(
        f"Training complete, max device memory: {get_accelerator().max_memory_allocated() / 1024 ** 2:.2f} MB",
        ranks=[0],
    )


# 训练的入口，加载命令行参数，调用训练的主函数
if __name__ == "__main__":
    # Default args here will train DiT-XL/2 with the hyperparameters we used in our paper (except training iters).
    # 加载相关的命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-m", "--model", type=str, choices=list(DiT_models.keys()), default="DiT-S/8"
    )
    parser.add_argument(
        "-x", "--model_arch", choices=SUPPORTED_MODEL_ARCH, default="cross-attn"
    )
    parser.add_argument("-d", "--dataset", nargs="+", default=[])
    parser.add_argument("-v", "--video_dir", type=str, required=True)
    parser.add_argument("-e", "--epochs", type=int, default=10)
    parser.add_argument("-b", "--batch_size", type=int, default=4)
    parser.add_argument("-g", "--grad_checkpoint", action="store_true", default=False)
    parser.add_argument("-a", "--accumulation_steps", default=1, type=int)
    parser.add_argument("--sp_size", type=int, default=1)
    parser.add_argument(
        "--sp_mode", type=str, default="ulysses", choices=SUPPORTED_SEQ_PARALLEL_MODES
    )
    parser.add_argument("--sp_overlap", action="store_true", default=False)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--save_interval", type=int, default=20)
    parser.add_argument("--checkpoint_dir", type=str, default="checkpoints")
    parser.add_argument("--tensorboard_dir", type=str, default="runs")
    parser.add_argument(
        "-c", "--compressor", choices=["raw", "vqvae", "vae"], default="raw"
    )
    parser.add_argument("--load_model", default=None)
    parser.add_argument("--load_optimizer", default=None)
    args = parser.parse_args()
    main(args)
