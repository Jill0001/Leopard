#!/bin/bash
#sh run_pretrain_mcore_qwen.sh dsw ../.. 0.5B 1 8 1e-5 1e-6 2048 32768 293 bf16 1 1 sel true false true true false 100000 /mnt/qwen-datasets/wudao_qwenbpe_content_document /mnt/qwen-ckpts/qwen1.5_0.5b_mcore_tp1_pp1_v2 10000000000 100000000 debug
#sh run_pretrain_mcore_qwen.sh dsw ../.. 0.5B 1 8 1e-5 1e-6 2048 32768 293 bf16 1 1 sel true false true true true 10 /mnt/qwen-datasets/wudao_qwenbpe_content_document /mnt/qwen-ckpts/Qwen1.5-0.5B 18349219840 183492198 debug
set -e
export CUDA_VISIBLE_DEVICES=2
MASTER_ADDR=localhost
MASTER_PORT=$(shuf -n 1 -i 10000-65535)
NNODES=1
NODE_RANK=0
GPUS_PER_NODE=1

EXP_DIR=$1

# 512 * 1k * 400k = 200b tokens.
# 512 * 1k * 200k = 100b tokens.
# 512 * 1k * 100k = 50b tokens (default).
# 512 * 1k * 20k = 10b tokens.
TRAINING_STEPS=20000
if [ -n "${2}" ]; then
    TRAINING_STEPS=$2;
fi

NUM_EXPERTS=4
if [ -n "${3}" ]; then
    NUM_EXPERTS=$3;
fi

TOP_K=1
if [ -n "${4}" ]; then
    TOP_K=$4;
fi

LOSS_WEIGHT=0.1
if [ -n "${5}" ]; then
    LOSS_WEIGHT=$5;
fi

BATCH_SIZE=1
if [ -n "${6}" ]; then
    BATCH_SIZE=$6;
fi

##
### Pre-training for dMoE 46M parameter.
##

# MoE hyperparameters.
MOE_ARGUMENTS="\
--moe-num-experts=${NUM_EXPERTS} \
--moe-loss-weight=${LOSS_WEIGHT} \
--moe-top-k=${TOP_K}"

DISTRIBUTED_ARGS="--nproc_per_node $GPUS_PER_NODE --nnodes $NNODES --node_rank $NODE_RANK --master_addr $MASTER_ADDR --master_port $MASTER_PORT"

# Model hyperparameters.
MODEL_ARGUMENTS="\
--num-layers 2 \
--hidden-size 1024 \
--num-attention-heads 32 \
--ffn-hidden-size 14336 \
--seq-length 128 \
--max-position-embeddings 128"

# Training hyperparameters.
TRAINING_ARGUMENTS="\
--micro-batch-size ${BATCH_SIZE} \
--global-batch-size 8 \
--train-iters ${TRAINING_STEPS} \
--lr-decay-iters ${TRAINING_STEPS} \
--lr 0.0006 \
--min-lr 0.00006 \
--lr-decay-style cosine \
--lr-warmup-fraction 0.01 \
--clip-grad 1.0 \
--init-method-std 0.01 \
--optimizer adam"

# NOTE: We don't train for enough tokens for the
# split to matter.
DATA_ARGUMENTS="\
--data-path /mnt/pile/gpt_small_text_document \
--vocab-file /mnt/pile/gpt2-vocab.json \
--merge-file /mnt/pile/gpt2-merges.txt \
--make-vocab-size-divisible-by 1024 \
--split 969,30,1"

COMPUTE_ARGUMENTS="\
--fp16 \
--DDP-impl local \
--moe-expert-model-parallelism \
--no-async-tensor-model-parallel-allreduce \
"

CHECKPOINT_ARGUMENTS="\
--save-interval 10 \
--save ./${EXP_DIR}"

EVALUATION_ARGUMENTS="\
--eval-iters 100 \
--log-interval 1 \
--eval-interval 1000"

torchrun ${DISTRIBUTED_ARGS} \
       third_party/Megatron-LM/pretrain_gpt.py \
       ${MOE_ARGUMENTS} \
       ${MODEL_ARGUMENTS} \
       ${TRAINING_ARGUMENTS} \
       ${DATA_ARGUMENTS} \
       ${COMPUTE_ARGUMENTS} \
       ${CHECKPOINT_ARGUMENTS} \
       ${EVALUATION_ARGUMENTS}
