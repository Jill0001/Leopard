# Copyright (c) 2023 Alibaba PAI and Nvidia Megatron-LM Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from contextlib import nullcontext
import math
import numpy as np
import torch
import torch.nn.functional as F
from typing import Optional
from copy import deepcopy

from megatron.core import mpu, tensor_parallel
from megatron.core.enums import ModelType
from megatron.core.models.common.embeddings.rotary_pos_embedding import apply_rotary_pos_emb
from megatron.core.tensor_parallel import gather_from_sequence_parallel_region_to_moe, reduce_scatter_to_sequence_parallel_region_from_moe
from megatron.core.parallel_state import get_tensor_model_parallel_group, get_tensor_and_data_parallel_group
try:
    from megatron import get_timers, get_args, get_retro_args, core, get_num_microbatches
    from megatron.model.module import MegatronModule    
    from megatron.model.enums import AttnMaskType, LayerType, AttnType
    from megatron.model.fused_softmax import FusedScaleMaskSoftmax
    from megatron.model.fused_bias_gelu import bias_gelu_impl
    from megatron.model.utils import attention_mask_func, openai_gelu, erf_gelu, get_norm
except:
    from megatron.training import get_timers, get_args, get_num_microbatches
    from megatron import core
    from megatron.legacy.model.module import MegatronModule
    from megatron.legacy.model.enums import AttnMaskType, LayerType, AttnType
    from megatron.legacy.model.fused_softmax import FusedScaleMaskSoftmax
    from megatron.legacy.model.fused_bias_gelu import bias_gelu_impl
    from megatron.legacy.model.utils import attention_mask_func, openai_gelu, erf_gelu, get_norm
    from megatron.core.fusions.fused_softmax import ScaledMaskedSoftmax

# from .rotary_pos_embedding import RotaryEmbedding
# from .rotary_pos_embedding import apply_rotary_pos_emb as apply_mistral_rotary_pos_emb
# from .idefics2_mlp import Idefics2MLP, ColumnParallelParameter

from transformers.modeling_attn_mask_utils import _prepare_4d_attention_mask

# Copied from transformers.models.llama.modeling_llama.repeat_kv
def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
    """
    This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
    num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
    """
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(batch, num_key_value_heads, n_rep, slen, head_dim)
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)

try:
    from einops import rearrange
except ImportError:
    rearrange = None

try:
    from flash_attn.flash_attn_interface import flash_attn_unpadded_func
except ImportError:
    try:
        from flash_attn.flash_attn_interface import flash_attn_varlen_func as flash_attn_unpadded_func
    except ImportError:
        flash_attn_unpadded_func = None

from flash_attn.bert_padding import unpad_input

""" We use the following notation throughout this file:
     h: hidden size
     n: number of attention heads
     p: number of model parallel partitions
     np: n/p
     hp: h/p
     hn: h/n
     b: batch size
     s: sequence length
     l: number of layers
    Transformer takes input of size [s, b, h] and returns a
    tensor of the same size. We use the following arguments:
        hyperparameters: transformer hyperparameters
"""

class DropPath(MegatronModule):
    """Drop paths (Stochastic Depth) per sample
    (when applied in main path of residual blocks).
    """

    def __init__(self, drop_prob=0.):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, hidden_state):
        if self.drop_prob == 0. or not self.training:
            return hidden_state
        keep_prob = 1 - self.drop_prob
        # work with diff dim tensors, not just 2D ConvNets
        # hidden_state: [s, b, h]
        shape = (1,) + (hidden_state.shape[1],) + (1,) * (hidden_state.ndim - 2)
        random_tensor = keep_prob + \
            torch.rand(shape, dtype=hidden_state.dtype, device=hidden_state.device)
        random_tensor.floor_()  # binarize
        output = hidden_state.div(keep_prob) * random_tensor
        return output

class Idefics2ParallelMLP(MegatronModule):
    """MLP.

    MLP will take the input with h hidden state, project it to 4*h
    hidden dimension, perform nonlinear transformation, and project the
    state back into h hidden dimension.
    """

    def __init__(self, config, is_expert=False):
        super(Idefics2ParallelMLP, self).__init__()
        args = get_args()

        self.add_bias = config.add_bias_linear

        ffn_hidden_size = config.ffn_hidden_size
        if config.gated_linear_unit:
            ffn_hidden_size *= 2

        # Project to 4h. If using swiglu double the output width, see https://arxiv.org/pdf/2002.05202.pdf
        self.dense_h_to_4h = tensor_parallel.ColumnParallelLinear(
            config.input_size,
            ffn_hidden_size,
            config=config,
            init_method=config.init_method,
            bias=self.add_bias,
            gather_output=False,
            skip_bias_add=True,
            is_expert=is_expert,
        )

        self.bias_gelu_fusion = False
        self.activation_func = None
        self.swiglu = args.swiglu

        if args.openai_gelu:
            self.activation_func = openai_gelu
        elif args.onnx_safe:
            self.activation_func = erf_gelu
        elif args.swiglu:
            def swiglu(x):
                x = torch.chunk(x, 2, dim=-1)
                return F.silu(x[0]) * x[1]
            self.activation_func = swiglu
        elif args.squared_relu:
            def squared_relu(x):
                return torch.pow(F.relu(x), 2)
            self.activation_func = squared_relu
        else:
            self.bias_gelu_fusion = args.bias_gelu_fusion
            self.activation_func = F.gelu

        # Project back to h.
        self.dense_4h_to_h = tensor_parallel.RowParallelLinear(
            config.ffn_hidden_size,
            config.output_size,
            config=config,
            init_method=config.output_layer_init_method,
            bias=self.add_bias,
            input_is_parallel=True,
            skip_bias_add=True,
            is_expert=is_expert,
        )

    def forward(self, hidden_states):

        # [s, b, 4hp]
        intermediate_parallel, bias_parallel = self.dense_h_to_4h(hidden_states)
        
        if self.bias_gelu_fusion:
            assert self.add_bias is True
            assert self.activation_func == F.gelu
            intermediate_parallel = bias_gelu_impl(intermediate_parallel, bias_parallel)
        else:
            if bias_parallel is not None:
                intermediate_parallel = intermediate_parallel + bias_parallel
            intermediate_parallel = self.activation_func(intermediate_parallel)

        # [s, b, h]
        output, output_bias = self.dense_4h_to_h(intermediate_parallel)
        return output, output_bias

class CoreAttention(MegatronModule):

    def __init__(self, layer_number, config,
                 attn_mask_type=AttnMaskType.padding):
        super(CoreAttention, self).__init__()
        self.fp16 = config.fp16
        self.bf16 = config.bf16

        self.apply_query_key_layer_scaling = config.apply_query_key_layer_scaling
        self.attention_softmax_in_fp32 = config.attention_softmax_in_fp32
        if self.apply_query_key_layer_scaling:
            self.attention_softmax_in_fp32 = True
        self.layer_number = max(1, layer_number)
        self.attn_mask_type = attn_mask_type
        self.sequence_parallel = config.sequence_parallel
        assert not self.sequence_parallel, 'Sequence parallel not supported'

        projection_size = config.kv_channels * config.num_attention_heads

        # Per attention head and per partition values.
        world_size = mpu.get_tensor_model_parallel_world_size()
        self.hidden_size_per_partition = core.utils.divide(projection_size,
                                                           world_size)
        self.hidden_size_per_attention_head = core.utils.divide(
            projection_size, config.num_attention_heads)
        self.num_attention_heads_per_partition = core.utils.divide(
            config.num_attention_heads, world_size)

        coeff = None
        self.norm_factor = math.sqrt(self.hidden_size_per_attention_head)
        if self.apply_query_key_layer_scaling:
            coeff = self.layer_number
            self.norm_factor *= coeff

        self.scale_mask_softmax = FusedScaleMaskSoftmax(
            self.fp16, self.bf16,
            AttnMaskType.padding, # self.attn_mask_type,
            config.masked_softmax_fusion,
            attention_mask_func,
            self.attention_softmax_in_fp32,
            coeff)

        # Dropout. Note that for a single iteration, this layer will generate
        # different outputs on different number of parallel partitions but
        # on average it should not be partition dependent.
        self.attention_dropout = torch.nn.Dropout(config.attention_dropout)

    def forward(self, query_layer, key_layer,
                value_layer, attention_mask):

        # ===================================
        # Raw attention scores. [b, np, s, s]
        # ===================================

        # [b, np, sq, sk]
        output_size = (query_layer.size(1),
                       query_layer.size(2),
                       query_layer.size(0),
                       key_layer.size(0))

        # [sq, b, np, hn] -> [sq, b * np, hn]
        query_layer = query_layer.contiguous().reshape(output_size[2],
                                          output_size[0] * output_size[1], -1)
        # [sk, b, np, hn] -> [sk, b * np, hn]
        key_layer = key_layer.contiguous().view(output_size[3],
                                   output_size[0] * output_size[1], -1)

        # preallocting input tensor: [b * np, sq, sk]
        matmul_input_buffer = mpu.get_global_memory_buffer().get_tensor(
            (output_size[0]*output_size[1], output_size[2], output_size[3]),
            query_layer.dtype, "mpu")

        # Raw attention scores. [b * np, sq, sk]
        matmul_result = torch.baddbmm(
            matmul_input_buffer,
            query_layer.transpose(0, 1),   # [b * np, sq, hn]
            key_layer.transpose(0, 1).transpose(1, 2),  # [b * np, hn, sk]
            beta=0.0, alpha=(1.0/self.norm_factor))

        # change view to [b, np, sq, sk]
        attention_scores = matmul_result.view(*output_size)

        # ===========================
        # Attention probs and dropout
        # ===========================

        # attention scores and attention mask [b, np, sq, sk]
        # attention_mask = attention_mask.to(torch.bool)


        # attention_probs = self.scale_mask_softmax(attention_scores,
        #                                           attention_mask)
        
        # attention_probs = ScaledMaskedSoftmax.apply(attention_scores, attention_mask, 1.0)

        if attention_mask is not None:
            if attention_mask.size() != (output_size[0], 1, output_size[2], output_size[3]):
                raise ValueError(
                    f"Attention mask should be of size { (output_size[0], 1, output_size[2], output_size[3])}, but is {attention_mask.size()}"
                )

            attn_weights = attention_scores + attention_mask

        attention_probs = torch.nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32).to(query_layer.dtype)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        if not self.sequence_parallel:
            with tensor_parallel.get_cuda_rng_tracker().fork():
                attention_probs = self.attention_dropout(attention_probs)
        else:
            attention_probs = self.attention_dropout(attention_probs)

        # =========================
        # Context layer. [sq, b, hp]
        # =========================

        # value_layer -> context layer.
        # [sk, b, np, hn] --> [b, np, sq, hn]

        # context layer shape: [b, np, sq, hn]
        output_size = (value_layer.size(1),
                       value_layer.size(2),
                       query_layer.size(0),
                       value_layer.size(3))

        # change view [sk, b * np, hn]
        value_layer = value_layer.view(value_layer.size(0),
                                       output_size[0] * output_size[1], -1)

        # change view [b * np, sq, sk]
        attention_probs = attention_probs.view(output_size[0] * output_size[1],
                                               output_size[2], -1)

        # matmul: [b * np, sq, hn]
        context_layer = torch.bmm(attention_probs, value_layer.transpose(0, 1))

        # change view [b, np, sq, hn]
        context_layer = context_layer.view(*output_size)

        # [b, np, sq, hn] --> [sq, b, np, hn]
        context_layer = context_layer.permute(2, 0, 1, 3).contiguous()

        # [sq, b, np, hn] --> [sq, b, hp]
        new_context_layer_shape = context_layer.size()[:-2] + \
            (self.hidden_size_per_partition,)
        context_layer = context_layer.view(*new_context_layer_shape)

        return context_layer


class FlashSelfAttention(torch.nn.Module):
    """Implement the scaled dot product attention with softmax.
    Arguments
    ---------
        softmax_scale: The temperature to use for the softmax attention.
                      (default: 1/sqrt(d_keys) where d_keys is computed at
                      runtime)
        attention_dropout: The dropout rate to apply to the attention
                           (default: 0.0)
    """
    def __init__(self, causal=False, softmax_scale=None, attention_dropout=0.0,
                 device=None, dtype=None):
        super().__init__()
        assert flash_attn_unpadded_func is not None, ('Please install FlashAttention first, '
                                                      'e.g., with pip install flash-attn')
        assert rearrange is not None, 'Please install einops first, e.g., with pip install einops'
        self.causal = causal
        self.softmax_scale = softmax_scale
        self.dropout_p = attention_dropout

    def forward(self, q, k, v, attention_mask=None):
        """Implements the multihead softmax attention.
        Arguments
        ---------
            q, k, v: The tensor containing the query, key, and value. (B, S, H, D)
            attention_mask:  
        """

        assert all((i.dtype in [torch.float16, torch.bfloat16] for i in (q,k,v)))
        assert all((i.is_cuda for i in (q,k,v)))

        batch_size, seqlen_q = q.shape[0], q.shape[1]
        seqlen_k = k.shape[1]

        # q, k, v = [rearrange(x, 'b s ... -> (b s) ...') for x in [q, k, v]]

        q = rearrange(q, 'b s ... -> (b s) ...')

        # in Idefics2, all q have the query length of 64.
        cu_seqlens_q = torch.arange(0, (batch_size + 1) * seqlen_q, step=seqlen_q, dtype=torch.int32,
                                    device=q.device)
        # the length of the k v depends on the attention_mask
        if attention_mask is not None:
            k, _, cu_seqlens_k, _ = unpad_input(k, attention_mask)
            v, _, _, _ = unpad_input(v, attention_mask)
        else:
            k, v = [rearrange(x, 'b s ... -> (b s) ...') for x in [k, v]]

        if self.training:
            # during training q,k,v always have same seqlen
            # assert seqlen_k == seqlen_q

            is_causal = self.causal
            if attention_mask is None:
                cu_seqlens_k = cu_seqlens_q
            dropout_p = self.dropout_p
        else:
            # turn off FA causal mask after first inference autoregressive iteration
            # only on first autoregressive step q,k,v have same seqlen
            is_causal = seqlen_q == seqlen_k
            # cu_seqlens_k = torch.arange(0, (batch_size + 1) * seqlen_k, step=seqlen_k, dtype=torch.int32,
            #             device=q.device)
            if attention_mask is None:
                cu_seqlens_k = cu_seqlens_q
            dropout_p = 0

        output = flash_attn_unpadded_func(
            q, k, v, cu_seqlens_q, cu_seqlens_k, seqlen_q, seqlen_k,
            dropout_p,
            softmax_scale=self.softmax_scale, causal=False
        )

        output = rearrange(output, '(b s) ... -> b s ...', b=batch_size)
        return output


class ParallelAttention(MegatronModule):
    """Parallel self-attention layer abstract class.

    Self-attention layer takes input with size [s, b, h]
    and returns output of the same size.
    """

    def __init__(self, config, layer_number,
                 attention_type=AttnType.cross_attn,
                 attn_mask_type=AttnMaskType.padding):
        super(ParallelAttention, self).__init__()
        args = get_args()
        self.layer_number = max(1, layer_number)
        self.attention_type = attention_type
        self.attn_mask_type = attn_mask_type
        self.params_dtype = config.params_dtype
        self.sequence_parallel = config.sequence_parallel
        assert not self.sequence_parallel, 'Sequence parallel not supported'

        self.group_query_attention = args.group_query_attention
        self.num_query_groups = args.num_query_groups

        # query_projection_size = config.kv_channels * config.num_attention_heads
        # if self.group_query_attention:
        #     kv_projection_size = args.kv_channels * args.num_query_groups
        # else:
        #     kv_projection_size = args.kv_channels * args.num_attention_heads
        
        # new variables
        query_projection_size = config.kv_channels * config.num_attention_heads # config.query_projection_size
        kv_projection_size = query_projection_size # config.kv_projection_size
        num_heads = config.num_attention_heads
        num_kv_heads = config.num_attention_heads # config.num_key_value_heads. Duplicate 4 times
        num_key_value_groups = num_heads // num_kv_heads

        # self.use_flash_attn = args.use_flash_attn \
        #     and attention_type == AttnType.self_attn \
        #     and self.attn_mask_type == AttnMaskType.causal
        self.use_flash_attn = args.use_flash_attn
        print("Perceiver using flash_attn: ", self.use_flash_attn)

        if self.use_flash_attn:
            if flash_attn_unpadded_func is None:
                raise ImportError('FlashAttention is not installed, please install with '
                                  'pip install flash-attn')
            # assert attention_type == AttnType.self_attn, ('FlashAttention code path only supports '
            #                                               'self-attention for now')
            # assert self.attn_mask_type == AttnMaskType.causal, ('FlashAttention code path only '
            #                                                     'supports causal mask for now')
            if rearrange is None:
                raise ImportError('einops is not installed, please install with pip install einops')

        # Per attention head and per partition values.
        world_size = mpu.get_tensor_model_parallel_world_size()
        self.hidden_size_per_attention_head = core.utils.divide(
            query_projection_size, config.num_attention_heads)
        self.hidden_size_per_attention_head_query = core.utils.divide(
            query_projection_size, config.num_attention_heads)
        self.hidden_size_per_attention_head_kv = core.utils.divide(
            kv_projection_size, config.num_attention_heads)
        self.num_attention_heads_per_partition = core.utils.divide(
            config.num_attention_heads, world_size)
        
        # num_kv_heads, may be smaller than world_size
        if num_kv_heads >= world_size:
            self.num_attention_heads_per_partition_kv = core.utils.divide(
                config.num_attention_heads, world_size)
        else:
            # duplicate kv attention several times
            self.num_attention_heads_per_partition_kv = self.num_attention_heads_per_partition


        if self.group_query_attention:
            if args.num_query_groups % world_size != 0:
                raise NotImplementedError('Currently the num_query_groups should be '
                                          'a multiple of the tensor parallel size')
            self.num_query_groups_per_partition = core.utils.divide(
                        args.num_query_groups, world_size)
        else:
            self.num_query_groups_per_partition = self.num_attention_heads_per_partition


        # it's a cross_attention            
        assert attention_type == AttnType.cross_attn

        # if self.group_query_attention:
        #     raise NotImplementedError("Grouped query attention not implemented for cross-attention.")

        self.query = tensor_parallel.ColumnParallelLinear(
            config.text_hidden_size,
            query_projection_size,
            config=config,
            init_method=config.init_method,
            bias=config.add_bias_linear,
            gather_output=False)

        # check later
        self.key_value = tensor_parallel.ColumnParallelLinear(
            config.text_hidden_size,
            2 * kv_projection_size * num_key_value_groups, 
            config=config,
            init_method=config.init_method,
            bias=config.add_bias_linear,
            gather_output=False)

        self.core_attention = CoreAttention(self.layer_number, config,
                                            self.attn_mask_type)
        self.checkpoint_core_attention = config.recompute_granularity == 'selective'

        self.checkpoint_core_attention = False # TQ FIXME: setting true on this will yield error

        if self.use_flash_attn:
            is_causal = self.attn_mask_type == AttnMaskType.causal
            self.core_attention_flash = FlashSelfAttention(
                causal=is_causal, attention_dropout=config.attention_dropout
            )

        # Output.
        self.dense = tensor_parallel.RowParallelLinear(
            query_projection_size,
            config.text_hidden_size,
            config=config,
            init_method=config.output_layer_init_method,
            bias=args.add_bias_linear,
            input_is_parallel=True,
            skip_bias_add=True)

    def _checkpointed_attention_forward(self, query_layer, key_layer,
                                        value_layer, attention_mask,
                                        rotary_pos_emb=None):
        """Forward method with activation checkpointing."""
        def custom_forward(*inputs):
            query_layer = inputs[0]
            key_layer = inputs[1]
            value_layer = inputs[2]
            attention_mask = inputs[3]
            output_ = self.core_attention(query_layer, key_layer,
                                          value_layer, attention_mask)
            return output_

        q_pos_emb, k_pos_emb = (None, None) if rotary_pos_emb is None \
            else rotary_pos_emb

        hidden_states = tensor_parallel.checkpoint(
            custom_forward,
            False, query_layer, key_layer, value_layer, attention_mask,
            q_pos_emb, k_pos_emb)

        return hidden_states

    def _allocate_memory(self, inference_max_sequence_len, batch_size, num_attention_heads):
        return torch.empty(
            inference_max_sequence_len,
            batch_size,
            num_attention_heads,
            self.hidden_size_per_attention_head_query,
            dtype=self.params_dtype,
            device=torch.cuda.current_device())

    def forward(self, latents, context, attention_mask,
                inference_params=None,
                rotary_pos_emb=None, position_ids=None):
        # hidden_states: [sq, b, h]

        # =================================================
        # Pre-allocate memory for key-values for inference.
        # =================================================
        is_first_step = False
        if inference_params:
            if self.layer_number not in inference_params.key_value_memory_dict:
                inf_max_seq_len = inference_params.max_sequence_length
                inf_max_batch_size = inference_params.max_batch_size
                inference_key_memory = self._allocate_memory(
                    inf_max_seq_len, inf_max_batch_size,
                    self.num_query_groups_per_partition)
                inference_value_memory = self._allocate_memory(
                    inf_max_seq_len, inf_max_batch_size,
                    self.num_query_groups_per_partition)

                inference_params.key_value_memory_dict[self.layer_number] = (
                    inference_key_memory, inference_value_memory)
                is_first_step = True
            else:
                inference_key_memory, inference_value_memory = \
                    inference_params.key_value_memory_dict[self.layer_number]

        # =====================
        # Query, Key, and Value
        # =====================
        # cross attention
        # Attention heads [sk, b, h] --> [sk, b, (np * 2 * hn)]

        
        # latents (`torch.Tensor`): Tensor of shape [n_latents, bsz, embed_dim] representing fixed length latents to compress to.
        # context (`torch.Tensor`): Tensor of shape [seq, bsz, embed_dim] representing long-form context to resample.

        hidden_states = torch.concat([context, latents], dim=0)

        mixed_kv_layer, _ = self.key_value(hidden_states) # by default no bias
        
        # np = num_kv_head // tp
        # 2: k and v
        # hn: head_hidden_dim

        # [sk, b, (np * 2 * hn)] --> [sk, b, np, 2 * hn]
        new_tensor_shape = mixed_kv_layer.size()[:-1] + \
            (self.num_attention_heads_per_partition,
            2 * self.hidden_size_per_attention_head_kv)
        mixed_kv_layer = mixed_kv_layer.view(*new_tensor_shape)

        # [sk, b, np, 2 * hn] --> 2 [sk, b, np, hn]
        (key_layer,
        value_layer) = tensor_parallel.split_tensor_along_last_dim(mixed_kv_layer, 2)

        # Attention head [sq, b, h] --> [sq, b, hp]
        query_layer, _ = self.query(latents)
        # [sq, b, hp] --> [sq, b, np, hn]
        new_tensor_shape = query_layer.size()[:-1] + \
            (self.num_attention_heads_per_partition,
            self.hidden_size_per_attention_head)
        query_layer = query_layer.view(*new_tensor_shape)

        # ==================================
        # Adjust key and value for inference
        # ==================================

        if inference_params:

            batch_start = inference_params.batch_size_offset
            batch_end = batch_start + key_layer.size(1)
            assert batch_end <= inference_key_memory.size(1)
            sequence_start = inference_params.sequence_len_offset
            sequence_end = sequence_start + key_layer.size(0)
            assert sequence_end <= inference_key_memory.size(0)
            # Copy key and values.
            inference_key_memory[sequence_start:sequence_end,
                                 batch_start:batch_end, ...] = key_layer
            inference_value_memory[sequence_start:sequence_end,
                                   batch_start:batch_end, ...] = value_layer
            key_layer = inference_key_memory[
                :sequence_end, batch_start:batch_end, ...]
            value_layer = inference_value_memory[
                :sequence_end, batch_start:batch_end, ...]

        # ==================================
        # core attention computation
        # ==================================
        # expand the key_layer and value_layer [sk, b, ng, hn] -> [sk, b, np, hn]
        # key_layer = key_layer.repeat_interleave(
        #     self.num_attention_heads_per_partition // self.num_query_groups_per_partition,
        #     dim = 2
        # )
        # value_layer = value_layer.repeat_interleave(
        #     self.num_attention_heads_per_partition // self.num_query_groups_per_partition,
        #     dim = 2
        # )
        
        # already duplicate the kv attention heads.

        if not self.use_flash_attn:
            if self.checkpoint_core_attention:
                context_layer = self._checkpointed_attention_forward(
                    query_layer, key_layer, value_layer, attention_mask)
            else:
                context_layer = self.core_attention(
                    query_layer, key_layer, value_layer, attention_mask)
        else:
            q, k, v = [rearrange(x, 's b ... -> b s ...').contiguous()
                       for x in (query_layer, key_layer, value_layer)]
            if not self.sequence_parallel:
                with tensor_parallel.get_cuda_rng_tracker().fork():
                    context_layer = self.core_attention_flash(q, k, v, attention_mask)
            else:
                assert False
                context_layer = self.core_attention_flash(q, k, v)
            context_layer = rearrange(context_layer, 'b s h d -> s b (h d)').contiguous()

        # =================
        # Output. [sq, b, h]
        # =================

        output, bias = self.dense(context_layer)

        return output, bias


def bias_dropout_add(x, bias, residual, prob, training):
    # type: (Tensor, Optional[Tensor], Tensor, float, bool) -> Tensor
    if bias is not None:
        x = x + bias
    out = torch.nn.functional.dropout(x, p=prob, training=training)
    out = residual + out
    return out


def get_bias_dropout_add(training):
    def _bias_dropout_add(x, bias, residual, prob):
        return bias_dropout_add(x, bias, residual, prob, training)
    return _bias_dropout_add


@torch.jit.script
def bias_dropout_add_fused_train(x: torch.Tensor,
                                 bias: Optional[torch.Tensor],
                                 residual: torch.Tensor,
                                 prob: float) -> torch.Tensor:
    return bias_dropout_add(x, bias, residual, prob, True)


@torch.jit.script
def bias_dropout_add_fused_inference(x: torch.Tensor,
                                     bias: Optional[torch.Tensor],
                                     residual: torch.Tensor,
                                     prob: float) -> torch.Tensor:
    return bias_dropout_add(x, bias, residual, prob, False)


class ParallelTransformerLayer(MegatronModule):
    """A single transformer layer.

    Transformer layer takes input with size [s, b, h] and returns an
    output of the same size.
    """

    def __init__(self, config,
                 layer_number, layer_type=LayerType.encoder,
                 self_attn_mask_type=AttnMaskType.padding,
                 drop_path_rate=0.):
                 # retriever=None):
        args = get_args()

        super(ParallelTransformerLayer, self).__init__()
        self.layer_number = layer_number
        self.layer_type = layer_type

        self.apply_residual_connection_post_norm \
            = config.apply_residual_connection_post_layernorm

        self.bf16 = config.bf16
        self.fp32_residual_connection = config.fp32_residual_connection

        # Normalize the input data.
        # self.input_norm = get_norm(config)

        norm_config = deepcopy(config)
        norm_config.hidden_size = norm_config.text_hidden_size
        self.input_latents_norm = get_norm(norm_config)
        self.input_context_norm = get_norm(norm_config)

        # Self attention.
        self.self_attention = ParallelAttention(
            config,
            layer_number,
            attention_type=AttnType.cross_attn,
            attn_mask_type=self_attn_mask_type)
        self.hidden_dropout = config.hidden_dropout
        self.bias_dropout_fusion = config.bias_dropout_fusion
        self.drop_path = DropPath(drop_path_rate) if drop_path_rate > 0.0 else None

        # Normalize the attention output
        norm_config = deepcopy(config)
        norm_config.hidden_size = norm_config.text_hidden_size
        self.post_attention_norm = get_norm(norm_config)

        # Cross attention.
        if self.layer_type in (LayerType.decoder,
                               LayerType.retro_decoder,
                               LayerType.retro_decoder_with_retriever,
                               LayerType.retro_encoder):
            self.inter_attention = ParallelAttention(
                config,
                layer_number,
                attention_type=AttnType.cross_attn)
            # Normalize the attention output.
            self.post_inter_attention_norm = get_norm(config)

        attention_mlp_config = deepcopy(config)
        attention_mlp_config.input_size = attention_mlp_config.text_hidden_size
        attention_mlp_config.ffn_hidden_size = attention_mlp_config.text_hidden_size * 4
        attention_mlp_config.output_size = attention_mlp_config.text_hidden_size
        attention_mlp_config.gated_linear_unit = True # gated MLP

        # MLP params:
        # input size: config.hidden_size
        # intermediate size: config.ffn_hidden_size
        # output size: 

        self.mlp = Idefics2ParallelMLP(attention_mlp_config)

        # Set bias+dropout+add fusion grad_enable execution handler.
        TORCH_MAJOR = int(torch.__version__.split('.')[0])
        TORCH_MINOR = int(torch.__version__.split('.')[1])
        use_nvfuser = TORCH_MAJOR > 1 or (TORCH_MAJOR == 1 and TORCH_MINOR >= 10)
        self.bias_dropout_add_exec_handler = \
                nullcontext if use_nvfuser else torch.enable_grad

        if args.retro_add_retriever:
            retro_args = get_retro_args()
            self.retro_num_neighbors = args.retro_num_neighbors
            self.retro_chunk_length = retro_args.retro_gpt_chunk_length
            self.retro_retrieved_length = retro_args.retro_gpt_retrieved_length

        # Retriever (bi-directional transformer with cross attention)
        if layer_type == LayerType.retro_decoder_with_retriever:
            self.retriever = ParallelTransformer(
                config=config,
                model_type=ModelType.retro_encoder,
                self_attn_mask_type=AttnMaskType.padding,
                pre_process=True,
                post_process=False,
            )
            self._retriever_key = 'retriever'
        else:
            self.retriever = None

    def default_decoder_cross_attention(self,
                                        encoder_output,
                                        enc_dec_attn_mask,
                                        norm_input,
                                        norm_output,
                                        bias_dropout_add_func):
        '''Cross attention for a standard encoder-decoder model.'''

        # Attention.
        attention_output, attention_bias = \
            self.inter_attention(norm_output,
                                 enc_dec_attn_mask,
                                 encoder_output=encoder_output)

        # Residual connection.
        if self.apply_residual_connection_post_norm:
            residual = norm_output
        else:
            residual = norm_input

        if attention_bias is not None:
            attention_bias = attention_bias.expand_as(residual)

        # Bias-dropout-add.
        with self.bias_dropout_add_exec_handler():
            norm_input = bias_dropout_add_func(
                attention_output,
                attention_bias,
                residual,
                self.hidden_dropout)

        # Normalize.
        norm_output = self.post_inter_attention_norm(norm_input)

        return norm_input, norm_output

    def retro_encoder_cross_attention(self,
                                      retriever_output,
                                      norm_input,
                                      norm_output,
                                      bias_dropout_add_func):
        """Cross attention for Retro encoder.

        Notation:
            ns : Sequence length.
            bs : Batch size.
            d  : Hidden size.
            l  : Number of chunks per sample (i.e., seq_length/chunk_length).
            k  : Number of neighbors.
            r  : Number of retrieved tokens (neighbors + continuation).
        """

        ns, bs, d = norm_output.shape # [r, bs * l * k, d]

        # Divide sequence dimension into chunks.
        chunked_outputs = norm_output.reshape(self.retro_retrieved_length,
                                              -1,
                                              self.retro_num_neighbors,
                                              d)
        chunked_outputs_before_norm = \
            norm_input.reshape(self.retro_retrieved_length, -1,
                               self.retro_num_neighbors, d) # [r, bs*l, k, d]

        # Per-chunk attention.
        norm_inputs = []
        norm_outputs = []
        for k in range(self.retro_num_neighbors):

            # Attention.
            chunked_output = chunked_outputs[:,:,k].contiguous()
            attention_output, attention_bias = \
                self.inter_attention(
                    chunked_output, # Q (neighbor embedding)
                    None,
                    encoder_output=retriever_output) # K, V (hidden act)

            # Residual connection.
            if self.apply_residual_connection_post_norm:
                residual = chunked_output
            else:
                residual = chunked_outputs_before_norm[:,:,k]

            # Re-enable torch grad to enable fused optimization.
            with torch.enable_grad():
                norm_input = bias_dropout_add_func(
                    attention_output,
                    None if attention_bias is None else attention_bias.expand_as(residual),
                    residual,
                    self.hidden_dropout)
                norm_inputs.append(norm_input)

            # Layer norm.
            norm_output = self.post_inter_attention_norm(norm_input)
            norm_outputs.append(norm_output)

        # Concatenate layer norms.
        # norm_input : [r, k * bs * l, d]
        # norm_output : [r, k * bs * l, d]
        norm_input = torch.stack(norm_inputs, dim=1).reshape(ns, bs, d)
        norm_output = torch.stack(norm_outputs, dim=1).reshape(ns, bs, d)

        return norm_input, norm_output

    def retro_decoder_cross_attention(self,
                                      retriever_input,
                                      retriever_output,
                                      retriever_attn_mask,
                                      norm_input,
                                      norm_output,
                                      inference_params,
                                      bias_dropout_add_func):
        """Cross attention for Retro decoder.

        Notation:
            ns : Sequence length.
            bs : Batch size.
            d  : Hidden size.
            l  : Number of chunks per sample (i.e., seq_length/chunk_length).
            m  : Number of tokens per chunk.
            k  : Number of neighbors.
            r  : Number of retrieved tokens (neighbors + continuation).
        """

        ns, bs, d = norm_output.shape
        l = int(np.ceil(ns / self.retro_chunk_length))

        # Retrieve neighbors.
        if self.layer_type == LayerType.retro_decoder_with_retriever:
            first_ns = ns % self.retro_chunk_length
            if first_ns > 0:
                raise Exception("test this case.")
                first_chunk, rest_chunk = \
                    norm_output[:first_ns], norm_output[first_ns:]
                first_chunk = torch.nn.functional.pad(
                    first_chunk,
                    (0, 0, 0, 0, 0, self.retro_chunk_length - first_ns),
                    'constant',
                    0)
                chunked_output = \
                    torch.cat((first_chunk, rest_chunk), dim=0) # [l * m, bs, d]
            else:
                chunked_output = norm_output # [l * m, bs, d]
            chunked_output = chunked_output \
                .reshape(l, self.retro_chunk_length, bs, d) \
                .permute(1, 2, 0, 3) \
                .reshape(self.retro_chunk_length, bs * l, d) \
                .contiguous()

            # Get Encoder Output
            retriever_output = self.retriever(
                hidden_states=retriever_input,
                attention_mask=retriever_attn_mask,
                retriever_output=chunked_output,
                retriever_attn_mask=retriever_attn_mask,
                inference_params=inference_params) # [r, k * bs * l , d]
            retriever_output = retriever_output.reshape(
                self.retro_retrieved_length * self.retro_num_neighbors, bs * l, d) # [r * k, bs * l, d]

        # Chunks.
        pad = (ns - 1) % self.retro_chunk_length
        attending_chunks = norm_output[pad:]
        padded_chunks = torch.nn.functional.pad(
            attending_chunks,
            (0, 0, 0, 0, 0, self.retro_chunk_length - 1),
            'constant', 0)
        padded_chunked_output = padded_chunks \
            .reshape(l, self.retro_chunk_length, bs, d) \
            .permute(1, 2, 0, 3)
        padded_chunked_output = padded_chunked_output.reshape(
            self.retro_chunk_length, bs * l, d).contiguous()

        # Encoder output.
        attention_output, attention_bias = \
            self.inter_attention(padded_chunked_output,
                                 None,
                                 encoder_output=retriever_output)

        # Residual connection.
        if self.apply_residual_connection_post_norm:
            residual = norm_output
        else:
            residual = norm_input

        # Re-enable torch grad to enable fused optimization.
        with torch.enable_grad():
            norm_input = bias_dropout_add_func(
                attention_output,
                None if attention_bias is None else attention_bias.expand_as(attention_output),
                torch.zeros_like(attention_output),
                self.hidden_dropout)
            norm_input = norm_input \
                .reshape(self.retro_chunk_length, bs, l, d) \
                .permute(2, 0, 1, 3) # [l, m, bs, d]
            norm_input = norm_input.reshape(self.retro_chunk_length * l, bs, d)
            norm_input = torch.nn.functional.pad(
                norm_input,
                (0, 0, 0, 0, pad, 0),
                'constant', 0)[:ns] # [ns, b, d]
            norm_input = norm_input + residual

        # Layer norm post the decoder attention
        norm_output = self.post_inter_attention_norm(norm_input)

        return retriever_output, norm_input, norm_output

    def forward(self, latents, context, attention_mask,
                encoder_output=None, enc_dec_attn_mask=None,
                retriever_input=None,
                retriever_output=None,
                retriever_attn_mask=None,
                inference_params=None,
                rotary_pos_emb=None,
                position_ids=None):
        # hidden_states: [s, b, h]

        # Layer norm at the beginning of the transformer layer.
        # norm_output = self.input_norm(hidden_states)

        residual = latents
        latents = self.input_latents_norm(latents)
        context = self.input_context_norm(context)

        # Self attention.
        attention_output, attention_bias = \
            self.self_attention(
                latents,
                context,
                attention_mask,
                inference_params=inference_params,
                rotary_pos_emb=rotary_pos_emb,
                position_ids=position_ids
            )
        
        if self.drop_path is None:
            # jit scripting for a nn.module (with dropout) is not
            # trigerring the fusion kernel. For now, we use two
            # different nn.functional routines to account for varying
            # dropout semantics during training and inference phases.
            if self.bias_dropout_fusion:
                if self.training:
                    bias_dropout_add_func = bias_dropout_add_fused_train
                else:
                    bias_dropout_add_func = bias_dropout_add_fused_inference
            else:
                bias_dropout_add_func = get_bias_dropout_add(self.training)

            if attention_bias is not None:
                attention_bias = attention_bias.expand_as(residual)
            with self.bias_dropout_add_exec_handler():
                norm_input = bias_dropout_add_func(
                    attention_output,
                    attention_bias,
                    residual,
                    self.hidden_dropout)
        else:
            out = torch.nn.functional.dropout(attention_output + attention_bias,
                                              p=self.hidden_dropout,
                                              training=self.training)
            norm_input = residual + self.drop_path(out)

        
        # Layer norm post the self attention.
        norm_output = self.post_attention_norm(norm_input)

        # MLP.

        mlp_output, mlp_bias = self.mlp(norm_output)

        # Second residual connection.
        if self.apply_residual_connection_post_norm:
            residual = norm_output
        else:
            residual = norm_input

        if self.drop_path is None:
            if mlp_bias is not None:
                mlp_bias = mlp_bias.expand_as(residual)
            with self.bias_dropout_add_exec_handler():
                output = bias_dropout_add_func(
                    mlp_output,
                    mlp_bias,
                    residual,
                    self.hidden_dropout)

            # Jit compiled function creates 'view' tensor. This tensor
            # potentially gets saved in the MPU checkpoint function context,
            # which rejects view tensors. While making a viewless tensor here
            # won't result in memory savings (like the data loader, or
            # p2p_communication), it serves to document the origin of this
            # 'view' tensor.
            output = core.utils.make_viewless_tensor(inp = output,
                                                     requires_grad = output.requires_grad,
                                                     keep_graph = True)

        else:
            if mlp_bias is not None:
                mlp_output = mlp_output + mlp_bias
            out = torch.nn.functional.dropout(mlp_output,
                                              p=self.hidden_dropout,
                                              training=self.training)
            output = residual + self.drop_path(out)

        if self.layer_type == LayerType.retro_decoder_with_retriever:
            return output, retriever_output
        else:
            return output


class NoopTransformerLayer(MegatronModule):
    """A single 'no-op' transformer layer.

    The sole purpose of this layer is for when a standalone embedding layer
    is used (i.e., args.standalone_embedding_stage == True). In this case,
    zero transformer layers are assigned when pipeline rank == 0. Additionally,
    when virtual pipeline rank >= 1, zero total model parameters are created
    (virtual rank 0 contains the input embedding). This results in the model's
    input and output tensors being the same, which causes an error when
    performing certain memory optimiations on the output tensor (e.g.,
    deallocating it). Thus, this layer disconnects the input from the output
    via a clone. Since ranks containing a no-op layer are generally under-
    utilized (both compute and memory), there's no worry of any performance
    degredation.
    """

    def __init__(self, layer_number):
        super().__init__()
        self.layer_number = layer_number

    def forward(self, hidden_states, attention_mask,
                encoder_output=None, enc_dec_attn_mask=None,
                inference_params=None):
        return hidden_states.clone()


def _get_num_layers(args, model_type, is_decoder=False):
    """Compute the number of transformer layers resident on the current rank."""
    is_encoder_and_decoder_model = (model_type == ModelType.encoder_and_decoder)
    if model_type == ModelType.retro_encoder:
        num_layers = args.retro_encoder_layers
    elif mpu.get_pipeline_model_parallel_world_size() > 1:
        if is_encoder_and_decoder_model:
            assert args.pipeline_model_parallel_split_rank is not None

            # When a standalone embedding stage is used, a rank is taken from
            # the encoder's ranks, to be used for the encoder's embedding
            # layer. This way, the rank referenced by the 'split rank' remains
            # the same whether or not a standalone embedding stage is used.
            num_ranks_in_encoder = (
                args.pipeline_model_parallel_split_rank - 1
                if args.standalone_embedding_stage else
                args.pipeline_model_parallel_split_rank
            )
            num_ranks_in_decoder = args.transformer_pipeline_model_parallel_size - num_ranks_in_encoder
            assert args.encoder_num_layers % num_ranks_in_encoder == 0, \
                    'encoder_num_layers (%d) must be divisible by number of ranks given to encoder (%d)' % (args.encoder_num_layers, num_ranks_in_encoder)
            assert args.decoder_num_layers % num_ranks_in_decoder == 0, \
                    'decoder_num_layers (%d) must be divisible by number of ranks given to decoder (%d)' % (args.decoder_num_layers, num_ranks_in_decoder)
            if mpu.is_pipeline_stage_before_split():
                num_layers = (
                    0
                    if args.standalone_embedding_stage
                    and mpu.get_pipeline_model_parallel_rank() == 0 else
                    args.encoder_num_layers // num_ranks_in_encoder
                )
            else:
                num_layers = args.decoder_num_layers // num_ranks_in_decoder
        else:
            assert args.num_layers == args.encoder_num_layers
            assert args.num_layers % args.transformer_pipeline_model_parallel_size == 0, \
                'num_layers must be divisible by transformer_pipeline_model_parallel_size'

            # When a standalone embedding stage is used, all transformer layers
            # are divided among pipeline rank >= 1, while on pipeline rank 0,
            # ranks either contain the input embedding layer (virtual pp rank 0),
            # or no layers at all (virtual pp rank >= 1).
            num_layers = (
                0
                if args.standalone_embedding_stage
                and mpu.get_pipeline_model_parallel_rank() == 0 else
                args.num_layers // args.transformer_pipeline_model_parallel_size
            )
    else:
        if not is_decoder:
            num_layers = args.encoder_num_layers
        else:
            num_layers = args.decoder_num_layers
    return num_layers


def _get_layer_type(model_type, default_layer_type, retro_layer_numbers,
                    layer_number):
    args = get_args()
    if args.retro_add_retriever and layer_number in retro_layer_numbers:
        if model_type == ModelType.retro_decoder:
            return LayerType.retro_decoder_with_retriever \
                if layer_number == retro_layer_numbers[0] \
                   else LayerType.retro_decoder
        elif model_type == ModelType.retro_encoder:
            return LayerType.retro_encoder
        else:
            raise Exception("Unsupported model type, '%s'." % model_type)
    else:
        return default_layer_type


class Idefics2PerceiverParallelTransformer(MegatronModule):
    """Transformer class."""

    def __init__(self, config,
                 model_type, layer_type=LayerType.encoder,
                 self_attn_mask_type=AttnMaskType.padding,
                 post_norm=True,
                 pre_process=True,
                 post_process=True,
                 drop_path_rate=0.0):
        super(Idefics2PerceiverParallelTransformer, self).__init__()
        args = get_args()
        self.args = args
        
        self.layer_type = layer_type
        self.model_type = model_type
        self.bf16 = config.bf16
        self.fp32_residual_connection = config.fp32_residual_connection
        self.post_norm = post_norm
        self.pre_process = pre_process
        self.post_process = post_process
        self.input_tensor = None
        self.drop_path_rate = drop_path_rate
        self.transformer_impl = args.transformer_impl
        self.retro_add_retriever = args.retro_add_retriever

        # Store activation checkpoiting flag.
        self.recompute_granularity = config.recompute_granularity
        self.recompute_method = config.recompute_method
        self.recompute_num_layers = config.recompute_num_layers

        self.sequence_parallel = config.sequence_parallel
        assert not self.sequence_parallel, "Sequence parallel not supported for Perceiver."
        self.distribute_saved_activations = \
            config.distribute_saved_activations and not self.sequence_parallel

        # Transformer Engine Init.
        self.transformer_engine_v_0_10 = False
        self.transformer_engine_v_0_11 = False
        self.transformer_engine_v_0_8 = False
        if self.transformer_impl == 'transformer_engine':
            global transformer_engine
            import transformer_engine
            from importlib.metadata import version
            from pkg_resources import packaging

            te_version = packaging.version.Version(version("transformer-engine"))
            if te_version >= packaging.version.Version("0.8.0"):
                self.transformer_engine_v_0_8 = True
            if te_version >= packaging.version.Version("0.10.0"):
                self.transformer_engine_v_0_10 = True
            if te_version >= packaging.version.Version("0.11.0"):
                self.transformer_engine_v_0_11 = True

            del version, packaging

            assert not args.squared_relu, "TransformerEngine does not support squared relu activation."

        self.use_fp8 = args.fp8 is not None
        self.fp8_recipe = None
        self.fp8_group = None
        if self.use_fp8:
            assert args.transformer_impl == 'transformer_engine', \
                'transformer-engine required for fp8 training and inference'
            self.fp8_group = mpu.get_amax_reduction_group()
            if args.fp8 == "e4m3":
                fp8_format = transformer_engine.common.recipe.Format.E4M3
            elif args.fp8 == "hybrid":
                fp8_format = transformer_engine.common.recipe.Format.HYBRID
            else:
                raise ValueError("The DelayedScaling recipe only supports E4M3 and HYBRID formats.")
            self.fp8_recipe = transformer_engine.common.recipe.DelayedScaling(
                margin=args.fp8_margin,
                interval=args.fp8_interval,
                fp8_format=fp8_format,
                amax_history_len=args.fp8_amax_history_len,
                amax_compute_algo=args.fp8_amax_compute_algo,
                override_linear_precision=(False, False, not args.fp8_wgrad),
            )

        self.num_microbatches_in_previous_step = -1
        self.microbatch_count = 0
        self.checkpoint_core_attention = config.recompute_granularity == 'selective'

        # Number of layers.
        # self.num_layers = _get_num_layers(args, model_type,
        #                                   layer_type==LayerType.decoder)

        # TQ: this is a hard code
        self.num_layers = config.num_layers

        self.drop_path_rates = [
            rate.item() for rate in
            torch.linspace(0, self.drop_path_rate, config.num_layers)]

        self.retro_layer_numbers = None
        if model_type == ModelType.retro_decoder:
            retro_layer_start = 6 if config.num_layers <= 15 else 9
            self.retro_layer_numbers = \
                np.arange(retro_layer_start, args.num_layers + 1, 3).tolist()
        if model_type == ModelType.retro_encoder:
            self.retro_layer_numbers = [1]

        # Transformer layers.
        if args.retro_add_retriever:
            assert self.recompute_granularity != 'full', \
                "Full recompute not supported for Retro."
            assert args.transformer_impl == 'local', \
                "Transformer engine does not support Retro layers."
        def build_layer(layer_number):
            if args.transformer_impl == 'local':
                current_layer_type = _get_layer_type(
                    model_type, layer_type, self.retro_layer_numbers,
                    layer_number)
                return ParallelTransformerLayer(
                    config,
                    layer_number,
                    layer_type=current_layer_type,
                    self_attn_mask_type=self_attn_mask_type,
                    drop_path_rate=self.drop_path_rates[layer_number - 1])
            else:
                assert False
                # This argument is only available from TE v0.10 onwards.
                extra_transformer_engine_kwargs = {}
                if self.transformer_engine_v_0_8:
                    extra_transformer_engine_kwargs["bias"] = args.add_bias_linear
                if self.transformer_engine_v_0_10:
                    extra_transformer_engine_kwargs["activation"] = "swiglu" if args.swiglu else "gelu"
                if self.transformer_engine_v_0_11:
                    extra_transformer_engine_kwargs["normalization"] = args.normalization
                return transformer_engine.pytorch.TransformerLayer(
                    config.hidden_size,
                    config.ffn_hidden_size,
                    config.num_attention_heads,
                    layernorm_epsilon=config.layernorm_epsilon,
                    hidden_dropout=config.hidden_dropout,
                    attention_dropout=config.attention_dropout,
                    init_method=config.init_method,
                    output_layer_init_method=config.output_layer_init_method,
                    layer_number=layer_number,
                    kv_channels=config.kv_channels,
                    self_attn_mask_type=self_attn_mask_type.name,
                    tp_group=mpu.get_tensor_model_parallel_group(),
                    get_rng_state_tracker=tensor_parallel.get_cuda_rng_tracker,
                    fuse_wgrad_accumulation=config.gradient_accumulation_fusion,
                    apply_query_key_layer_scaling=config.apply_query_key_layer_scaling,
                    attention_softmax_in_fp32=config.attention_softmax_in_fp32,
                    seq_length=args.seq_length,
                    micro_batch_size=args.micro_batch_size,
                    sequence_parallel=self.sequence_parallel,
                    params_dtype=config.params_dtype,
                    apply_residual_connection_post_layernorm=config.apply_residual_connection_post_layernorm,
                    output_layernorm=False,
                    layer_type="encoder",
                    drop_path_rate=self.drop_path_rates[layer_number - 1],
                    set_parallel_mode=True,
                    fuse_qkv_params=True,
                    **extra_transformer_engine_kwargs)

        if config.virtual_pipeline_model_parallel_size is not None:
            assert config.num_layers % config.virtual_pipeline_model_parallel_size == 0, \
                'num_layers_per_stage must be divisible by ' \
                'virtual_pipeline_model_parallel_size'
            assert args.model_type != ModelType.encoder_and_decoder
            # Number of layers in each model chunk is the number of layers in the stage,
            # divided by the number of model chunks in a stage.
            self.num_layers = self.num_layers // config.virtual_pipeline_model_parallel_size
            # With 8 layers, 2 stages, and 4 model chunks, we want an assignment of
            # layers to stages like (each list is a model chunk):
            # Stage 0: [0]  [2]  [4]  [6]
            # Stage 1: [1]  [3]  [5]  [7]
            # With 8 layers, 2 stages, and 2 virtual stages, we want an assignment of
            # layers to stages like (each list is a model chunk):
            # Stage 0: [0, 1]  [4, 5]
            # Stage 1: [2, 3]  [6, 7]
            offset = mpu.get_virtual_pipeline_model_parallel_rank() * (
                config.num_layers // config.virtual_pipeline_model_parallel_size) + \
                (mpu.get_pipeline_model_parallel_rank() * self.num_layers)
        else:
            # Each stage gets a contiguous set of layers.
            if args.model_type == ModelType.encoder_and_decoder and \
                    mpu.get_pipeline_model_parallel_world_size() > 1:
                pipeline_rank = mpu.get_pipeline_model_parallel_rank()
                if layer_type == LayerType.encoder:
                    offset = pipeline_rank * self.num_layers
                else:
                    num_ranks_in_enc = args.pipeline_model_parallel_split_rank
                    offset = (pipeline_rank - num_ranks_in_enc) * self.num_layers
            else:
                offset = mpu.get_pipeline_model_parallel_rank() * self.num_layers

        # FIXME:
        # FTQ check later.
        modality_projection_config = deepcopy(config)
        modality_projection_config.input_size = modality_projection_config.vision_hidden_size
        modality_projection_config.ffn_hidden_size = modality_projection_config.intermediate_size 
        modality_projection_config.output_size = modality_projection_config.text_hidden_size
        modality_projection_config.gated_linear_unit = True # gated MLP


        # MLP params:
        # input size: config.hidden_size
        # intermediate size: config.ffn_hidden_size
        # output size: 

        self.modality_projection = Idefics2ParallelMLP(modality_projection_config)

        # self.latents = ColumnParallelParameter(config.n_latents, 
        #                                       modality_projection_config.text_hidden_size,
        #                                       config=modality_projection_config)
        self.n_latents = config.n_latents
        self.latents = torch.nn.Parameter(torch.ones(config.n_latents, modality_projection_config.text_hidden_size))

        if self.num_layers == 0:
            # When a standalone embedding stage is used (e.g.,
            # args.standalone_embedding_stage == True), virtual pipeline ranks
            # on pipeline rank 0 will have zero transformer layers assigned to
            # them. This results in the model's input and output tensors to be
            # the same, which will cause failure for certain output tensor
            # optimizations (e.g., pipeline output deallocation). To remedy
            # this, we assign a 'no-op' layer on these ranks, which will
            # disconnect the input tensor from the output tensor.
            self.num_layers = 1
            self.layers = torch.nn.ModuleList([ NoopTransformerLayer(1) ])
        else:
            self.layers = torch.nn.ModuleList(
                [build_layer(i + 1 + offset) for i in range(self.num_layers)])

            # Update dropout rate for Retro encoder.
            if model_type == ModelType.retro_encoder:
                for layer in self.layers:
                    if layer.self_attention.use_flash_attn:
                        layer.self_attention.core_attention_flash.dropout_p = \
                            torch.nn.Dropout(args.retro_encoder_attention_dropout)
                    else:
                        layer.self_attention.core_attention.attention_dropout.p =\
                            args.retro_encoder_attention_dropout
                    layer.hidden_dropout = args.retro_encoder_hidden_dropout

        if self.post_process and self.post_norm:
            # Final layer norm before output.
            norm_config = deepcopy(config)
            norm_config.hidden_size = norm_config.text_hidden_size
            self.final_norm = get_norm(norm_config)

    def _get_layer(self, layer_number):
        return self.layers[layer_number]

    def _checkpointed_forward(self, compressed_context, context, attention_mask,
                              encoder_output, enc_dec_attn_mask,
                              rotary_pos_emb, position_ids, is_first_microbatch):
        """Forward method with activation checkpointing."""
        def custom(start, end):
            def custom_forward(*args, **kwargs):
                x_, *args = args
                for index in range(start, end):
                    layer = self._get_layer(index)
                    x_ = layer(x_, *args, **kwargs)
                return x_
            return custom_forward

        te_forward_kwargs = {}
        if self.transformer_impl == 'transformer_engine':
            te_forward_kwargs['is_first_microbatch'] = is_first_microbatch
            if self.transformer_engine_v_0_10:
                te_forward_kwargs['rotary_pos_emb'] = rotary_pos_emb

        if self.recompute_method == 'uniform':
            # Uniformly divide the total number of Transformer layers and
            # checkpoint the input activation of each divided chunk.
            # A method to further reduce memory usage reducing checkpoints.
            l = 0
            while l < self.num_layers:
                if self.transformer_impl == 'transformer_engine':
                    compressed_context = transformer_engine.pytorch.checkpoint(
                        custom(l, l + self.recompute_num_layers),
                        self.distribute_saved_activations,
                        tensor_parallel.get_cuda_rng_tracker,
                        mpu.get_tensor_model_parallel_group(),
                        compressed_context, context, attention_mask, encoder_output,
                        enc_dec_attn_mask, **te_forward_kwargs)
                else:
                    compressed_context = tensor_parallel.checkpoint(
                        custom(l, l + self.recompute_num_layers),
                        self.distribute_saved_activations,
                        compressed_context, context, attention_mask,
                        encoder_output, enc_dec_attn_mask,
                        None, None, None, None, rotary_pos_emb, position_ids)
                    if mpu.get_tensor_model_parallel_rank() == 0 and compressed_context.isnan().any():
                        print ('compressed_context has nan', 'at layer', l,  compressed_context[:, :10, :10])
                l += self.recompute_num_layers

        elif self.recompute_method == 'block':
            # Checkpoint the input activation of only a set number of individual
            # Transformer layers and skip the rest.
            # A method fully use the device memory removing redundant re-computation.
            for l in range(self.num_layers):
                if l < self.recompute_num_layers:
                    if self.transformer_impl == 'transformer_engine':
                        compressed_context = transformer_engine.pytorch.checkpoint(
                            custom(l, l + 1),
                            self.distribute_saved_activations,
                            tensor_parallel.get_cuda_rng_tracker,
                            mpu.get_tensor_model_parallel_group(),
                            compressed_context, context, attention_mask, encoder_output,
                            enc_dec_attn_mask, **te_forward_kwargs)
                    else:
                        compressed_context = tensor_parallel.checkpoint(
                            custom(l, l + 1),
                            self.distribute_saved_activations,
                            compressed_context, context, attention_mask,
                            encoder_output, enc_dec_attn_mask,
                            None, None, None, None, rotary_pos_emb, position_ids)
                else:
                    if self.transformer_impl == 'transformer_engine':
                        compressed_context = custom(l, l + 1)(
                            compressed_context, context, attention_mask, encoder_output,
                            enc_dec_attn_mask, **te_forward_kwargs)
                    else:
                        compressed_context = custom(l, l + 1)(
                            compressed_context, context, attention_mask,
                            encoder_output, enc_dec_attn_mask,
                            None, None, None, None, rotary_pos_emb, position_ids)
        else:
            raise ValueError("Invalid activation recompute method.")

        return compressed_context

    def set_input_tensor(self, input_tensor):
        """Set input tensor to be used instead of forward()'s input.

        When doing pipeline parallelism the input from the previous
        stage comes from communication, not from the input, so the
        model's forward_step_func won't have it. This function is thus
        used by internal code to bypass the input provided by the
        forward_step_func"""
        self.input_tensor = input_tensor

    def forward(self, hidden_states, attention_mask,
                encoder_output=None, enc_dec_attn_mask=None,
                retriever_input=None,
                retriever_output=None,
                retriever_attn_mask=None,
                inference_params=None,
                rotary_pos_emb=None,
                position_ids=None):
        # hidden_states: [s, b, h]

        # Checks.
        if inference_params:
            assert self.recompute_granularity is None, \
                'inference does not work with activation checkpointing'

        if not self.pre_process:
            # See set_input_tensor()
            hidden_states = self.input_tensor

        # Viewless tensor.
        # - We only need to create a viewless tensor in the case of micro batch
        #   size (mbs) == 1, since in this case, 'hidden_states.transpose()'
        #   above creates a view tensor, and '.contiguous()' is a pass-through.
        #   For mbs >= 2, '.contiguous()' creates a new tensor, eliminating
        #   the need to make it viewless.
        #
        #   However, we don't explicitly check mbs == 1 here because
        #   make_viewless_tensor() has negligible overhead when its input
        #   is already viewless.
        #
        # - For the 'else' case above, calling make_viewless_tensor() here is
        #   likely redundant, since p2p_communication.py (likely originator)
        #   already creates viewless tensors. That said, make_viewless_tensor()
        #   is called here to be future-proof and corner-case-proof.
        

        hidden_states = core.utils.make_viewless_tensor(
            hidden_states,
            requires_grad=True,
            keep_graph=True,
        )

        context, _ = self.modality_projection(hidden_states)

        if mpu.get_tensor_model_parallel_rank() == 0 and context.isnan().any():
            print ('context has nan', context.shape, context[:, :10, :10])

        # RNG context.
        if self.sequence_parallel:
            rng_context = tensor_parallel.get_cuda_rng_tracker().fork()
        else:
            rng_context = nullcontext()

        # Forward layers.
        with rng_context:
            # The fp8_autocast context manager is a no-op when enabled=True
            # The if...else serves to short circuit name resolution for fp8_autocast
            with transformer_engine.pytorch.fp8_autocast(
                enabled=self.use_fp8,
                fp8_recipe=self.fp8_recipe,
                fp8_group=self.fp8_group
            ) if self.use_fp8 else nullcontext():
                # Determine if the current iteration is first microbatch
                if self.num_microbatches_in_previous_step != get_num_microbatches():
                    self.microbatch_count = 0 # Reset count on new batch size rampup interval
                self.num_microbatches_in_previous_step = get_num_microbatches()
                is_first_microbatch = self.microbatch_count % get_num_microbatches() == 0

                latents = self.latents.unsqueeze(0).expand((hidden_states.shape[0], *self.latents.size()))
                latent_attention_mask = torch.ones(
                    (attention_mask.size(0), latents.size(1)), dtype=attention_mask.dtype, device=attention_mask.device
                )
                attention_mask = torch.cat([attention_mask, latent_attention_mask], dim=-1)

                attention_mask = (
                    _prepare_4d_attention_mask(attention_mask, latents.dtype, tgt_len=self.n_latents)
                    if not self.args.use_flash_attn
                    else attention_mask
                )

                compressed_context = latents # bs, sq, hs

                context = context.transpose(1, 0) #  sq, bs, hs
                compressed_context = compressed_context.transpose(1, 0) # s, bs, hs

                # Forward pass.
                if self.recompute_granularity == 'full':
                    compressed_context = self._checkpointed_forward(compressed_context, 
                                                               context,
                                                               attention_mask,
                                                               encoder_output,
                                                               enc_dec_attn_mask,
                                                               rotary_pos_emb,
                                                               position_ids,
                                                               is_first_microbatch)
                else:
                    forward_kwargs = {
                        'encoder_output': encoder_output,
                        'enc_dec_attn_mask': enc_dec_attn_mask,
                        'inference_params': inference_params,
                    }

                    if self.transformer_impl == 'transformer_engine':
                        forward_kwargs['is_first_microbatch'] = is_first_microbatch
                        forward_kwargs['checkpoint_core_attention'] = self.checkpoint_core_attention
                        if self.transformer_engine_v_0_10:
                            forward_kwargs['rotary_pos_emb'] = rotary_pos_emb
                    else:
                        forward_kwargs['rotary_pos_emb'] = rotary_pos_emb
                        forward_kwargs['position_ids'] = position_ids
                        forward_kwargs['retriever_input'] = retriever_input
                        forward_kwargs['retriever_output'] = retriever_output
                        forward_kwargs['retriever_attn_mask'] = retriever_attn_mask
                    

                    for index in range(self.num_layers):
                        layer = self._get_layer(index)

                        compressed_context = layer(
                            compressed_context,
                            context,
                            attention_mask,
                            **forward_kwargs)
                        
                        # compressed_context = layer_outputs

                        # First Retro decoder layer returns both hidden_states
                        # and retriever_output. Make retriever_output available
                        # to subsequence Retro layers.
                        # if isinstance(hidden_states, tuple):
                        #     assert len(hidden_states) == 2
                        #     hidden_states, retriever_output = hidden_states
                        #     forward_kwargs["retriever_output"] = retriever_output

                # Skip counter update for eval and activation checkpointing
                if torch.is_grad_enabled() and self.training:
                    self.microbatch_count += 1

        # Final layer norm.
        if self.post_process and self.post_norm:
            hidden_states = self.final_norm(compressed_context)

        return hidden_states

    def load_state_dict(self, state_dict, strict=True):
        """Customize load."""

        # Handle renaming layernorm -> norm in component names
        args = get_args()
        state_dict_ = {}
        for key in state_dict.keys():
            if args.transformer_impl != "transformer_engine":
                newkey = key.replace("layernorm", "norm")
                state_dict_[newkey] = state_dict[key]
            else:
                state_dict_[key] = state_dict[key]

        if args.use_mistral_rotary_position_embeddings:
            super().load_state_dict(state_dict_, strict)
        else:
            super().load_state_dict(state_dict_, False)