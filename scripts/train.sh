#!/usr/bin/env bash
 
# get args
# 设定$1第一个参数，GPUs的数量，如果没有设置，默认是8
GPUS=${1:-8}

# get root dir
FOLDER_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
ROOT_DIR=$FOLDER_DIR/..

# go to root dir
cd $ROOT_DIR

# define dataset shards
COLLATED_VIDEO_DIR=./dataset/MSRVTT-collated/train/videos
PROCESSED_DATASET=(
    ./dataset/MSRVTT-processed/train/part-00000
    ./dataset/MSRVTT-processed/train/part-00001
    ./dataset/MSRVTT-processed/train/part-00002
    ./dataset/MSRVTT-processed/train/part-00003
    ./dataset/MSRVTT-processed/train/part-00004
    ./dataset/MSRVTT-processed/train/part-00005
    ./dataset/MSRVTT-processed/train/part-00006
    ./dataset/MSRVTT-processed/train/part-00007
    ./dataset/MSRVTT-processed/train/part-00008
    ./dataset/MSRVTT-processed/train/part-00009
)

# create timestamp to differentiate between runs
timestamp=$(date +%Y-%m-%d-%H-%M)

# run single node training
torchrun --standalone \
    --nproc_per_node $GPUS \
    train.py \
    --epochs 1 \
    --batch_size 1 \
    --lr 1e-4 \
    --accumulation_steps 32 \
    --grad_checkpoint \
    --dataset "${PROCESSED_DATASET[@]}" \
    --video_dir $COLLATED_VIDEO_DIR \
    --save_interval 224 \
    --checkpoint_dir ./checkpoints/$timestamp \
    --tensorboard_dir ./runs/$timestamp
