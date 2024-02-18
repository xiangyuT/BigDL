#
# Copyright 2016 The BigDL Authors.
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
#
# Some parts of this file is adapted from
# https://github.com/huggingface/transformers/blob/main/src/transformers/models/mistral/modeling_mistral.py
#
# Copyright 2023 Mistral AI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
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
""" PyTorch Mistral model."""
import math
from typing import Optional, Tuple

import torch
from torch import nn
import torch.nn.functional as F
from bigdl.llm.utils.common import invalidInputError
from bigdl.llm.transformers.models.utils import init_kv_cache, extend_kv_cache, append_kv_cache
from bigdl.llm.transformers.models.utils import apply_rotary_pos_emb, \
    apply_rotary_pos_emb_no_cache_xpu
from bigdl.llm.transformers.models.utils import is_enough_kv_cache_room_4_31, \
    is_enough_kv_cache_room_4_36
from bigdl.llm.transformers.low_bit_linear import SYM_INT4, FP8E5
from bigdl.llm.transformers.models.utils import use_flash_attention

KV_CACHE_ALLOC_BLOCK_LENGTH = 256


def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
    """
    This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep).
    The hidden states go from (batch, num_key_value_heads, seqlen, head_dim)
    to (batch, num_attention_heads, seqlen, head_dim)
    """
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(batch, num_key_value_heads,
                                                           n_rep, slen, head_dim)
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


def should_use_fuse_rope(self, hidden_states, position_ids):
    use_fuse_rope = hidden_states.device.type == "xpu"
    use_fuse_rope = use_fuse_rope and not (self.training and hidden_states.requires_grad)
    use_fuse_rope = use_fuse_rope and position_ids is not None
    return use_fuse_rope


def use_decoding_fast_path(q_type, use_fuse_rope, enough_kv_room, bs):
    return q_type in [SYM_INT4, FP8E5] and \
        use_fuse_rope and enough_kv_room and bs == 1


def compute_attn_outputs_weights(query_states, key_states, value_states, bsz, q_len, kv_seq_len,
                                 num_heads, head_dim, hidden_size, attention_mask):
    attn_weights = torch.matmul(
        query_states.to(key_states.dtype),
        key_states.transpose(2, 3)) / math.sqrt(head_dim)

    if attn_weights.size() != (bsz, num_heads, q_len, kv_seq_len):
        invalidInputError(
            False,
            f"Attention weights should be of size {(bsz, num_heads, q_len, kv_seq_len)},"
            f" but is {attn_weights.size()}"
        )

    if attention_mask is not None:
        if attention_mask.size() != (bsz, 1, q_len, kv_seq_len):
            invalidInputError(
                False,
                f"Attention mask should be of size {(bsz, 1, q_len, kv_seq_len)},"
                f" but is {attention_mask.size()}"
            )

        attn_weights = attn_weights + attention_mask

    # upcast attention to fp32
    attn_weights = nn.functional.\
        softmax(attn_weights, dim=-1, dtype=torch.float32).to(query_states.dtype)
    attn_output = torch.matmul(attn_weights, value_states.to(query_states.dtype))

    if attn_output.size() != (bsz, num_heads, q_len, head_dim):
        invalidInputError(
            f"`attn_output` should be of size {(bsz, num_heads, q_len, head_dim)},"
            f" but is {attn_output.size()}"
        )

    attn_output = attn_output.transpose(1, 2).contiguous()
    attn_output = attn_output.reshape(bsz, q_len, hidden_size)

    return attn_output, attn_weights


def mistral_attention_forward(
    self,
    hidden_states: torch.Tensor,
    attention_mask: Optional[torch.Tensor]=None,
    position_ids: Optional[torch.LongTensor]=None,
    past_key_value: Optional[Tuple[torch.Tensor]]=None,
    output_attentions: bool=False,
    use_cache: bool=False,
    padding_mask: Optional[torch.Tensor]=None,
) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
    bsz, q_len, hidden_size = hidden_states.size()
    device = hidden_states.device
    # for flash attention
    original_dtype = hidden_states.dtype

    use_fuse_rope = should_use_fuse_rope(self, hidden_states, position_ids)
    enough_kv_room = is_enough_kv_cache_room_4_31(past_key_value)
    decoding_fast_path = use_decoding_fast_path(self.q_proj.qtype,
                                                use_fuse_rope,
                                                enough_kv_room,
                                                bsz * q_len)
    decoding_fast_path = decoding_fast_path and not self.q_proj.transpose_qweight

    if decoding_fast_path:
        hidden_states = hidden_states.view(1, -1)
        kv_seq_len = past_key_value[0].shape[-2]
        cache_k = past_key_value[0]
        cache_v = past_key_value[1]
        import linear_q4_0
        query_states, key_states, value_states = linear_q4_0.forward_qkv(hidden_states,
                                                                         self.q_proj.weight,
                                                                         self.k_proj.weight,
                                                                         self.v_proj.weight,
                                                                         position_ids,
                                                                         cache_k, cache_v,
                                                                         self.q_proj.weight.qtype,
                                                                         kv_seq_len,
                                                                         self.head_dim)
        kv_seq_len += 1
    else:
        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len,
                                     self.num_key_value_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len,
                                         self.num_key_value_heads, self.head_dim).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            kv_seq_len += past_key_value[0].shape[-2]

        if use_fuse_rope:
            query_states, key_states = apply_rotary_pos_emb_no_cache_xpu(query_states,
                                                                         key_states,
                                                                         position_ids,
                                                                         "mistral")
        else:
            cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
            query_states, key_states = apply_rotary_pos_emb(query_states, key_states,
                                                            cos, sin, position_ids, "mistral")

        if past_key_value is not None:
            # reuse k, v, self_attention
            cache_k = past_key_value[0]
            cache_v = past_key_value[1]
            if not enough_kv_room:
                # allocate new
                new_cache_k, new_cache_v = extend_kv_cache(bsz,
                                                           self.num_key_value_heads,  # Support GQA
                                                           self.head_dim,
                                                           cache_k.size(2),
                                                           kv_seq_len + KV_CACHE_ALLOC_BLOCK_LENGTH,
                                                           dtype=cache_k.dtype,
                                                           device=device)

                new_cache_k[:] = cache_k
                new_cache_v[:] = cache_v
                cache_k = new_cache_k
                cache_v = new_cache_v

            key_states, value_states = append_kv_cache(cache_k, cache_v, key_states, value_states)

        elif use_cache:
            max_cache_length = kv_seq_len + KV_CACHE_ALLOC_BLOCK_LENGTH
            new_key_states, new_value_states = init_kv_cache(bsz,
                                                             self.num_key_value_heads,
                                                             self.head_dim,
                                                             kv_seq_len,
                                                             max_cache_length,
                                                             dtype=key_states.dtype,
                                                             device=device)
            new_key_states[:] = key_states
            new_value_states[:] = value_states
            key_states = new_key_states
            value_states = new_value_states

    past_key_value = (key_states, value_states) if use_cache else None

    if not self.training and not hidden_states.requires_grad:
        fsdp_flag = use_flash_attention(query_states, key_states)
    else:
        fsdp_flag = False
    if fsdp_flag:
        attention_dtype = torch.float16  # use fp16 for flash attention
    else:
        attention_dtype = original_dtype

    # repeat k/v heads if n_kv_heads < n_heads
    key_states = repeat_kv(key_states, self.num_key_value_groups).to(device,
                                                                     dtype=attention_dtype)
    value_states = repeat_kv(value_states, self.num_key_value_groups).to(device,
                                                                         dtype=attention_dtype)

    if fsdp_flag:
        attn_output = F.scaled_dot_product_attention(query_states.to(dtype=attention_dtype),
                                                     key_states,
                                                     value_states,
                                                     is_causal=True)
        attn_weights = None
        attn_output = attn_output.transpose(1, 2).contiguous()
        attn_output = attn_output.reshape(bsz, q_len, hidden_size)
    else:
        attn_output, attn_weights = compute_attn_outputs_weights(query_states,
                                                                 key_states,
                                                                 value_states,
                                                                 bsz, q_len, kv_seq_len,
                                                                 self.num_heads, self.head_dim,
                                                                 self.hidden_size, attention_mask)

    attn_output = self.o_proj(attn_output)

    if not output_attentions:
        attn_weights = None

    return attn_output.to(original_dtype), attn_weights, past_key_value


def mistral_attention_forward_4_36(
    self,
    hidden_states: torch.Tensor,
    attention_mask: Optional[torch.Tensor]=None,
    position_ids: Optional[torch.LongTensor]=None,
    past_key_value: Optional[Tuple[torch.Tensor]]=None,
    output_attentions: bool=False,
    use_cache: bool=False,
    padding_mask: Optional[torch.Tensor]=None,
) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
    bsz, q_len, hidden_size = hidden_states.size()
    device = hidden_states.device
    # for flash attention
    original_dtype = hidden_states.dtype

    use_fuse_rope = should_use_fuse_rope(self, hidden_states, position_ids)
    enough_kv_room = is_enough_kv_cache_room_4_36(past_key_value, self.layer_idx)
    decoding_fast_path = use_decoding_fast_path(self.q_proj.qtype,
                                                use_fuse_rope,
                                                enough_kv_room,
                                                bsz * q_len)
    decoding_fast_path = decoding_fast_path and not self.q_proj.transpose_qweight

    if decoding_fast_path:
        hidden_states = hidden_states.view(1, -1)

        cache_k = past_key_value.key_cache[self.layer_idx]
        cache_v = past_key_value.value_cache[self.layer_idx]

        kv_seq_len = cache_k.shape[-2]

        import linear_q4_0
        query_states, key_states, value_states = linear_q4_0.forward_qkv(hidden_states,
                                                                         self.q_proj.weight,
                                                                         self.k_proj.weight,
                                                                         self.v_proj.weight,
                                                                         position_ids,
                                                                         cache_k, cache_v,
                                                                         self.q_proj.weight.qtype,
                                                                         kv_seq_len,
                                                                         self.head_dim)
        kv_seq_len += 1

        # update past_key_value's seem_tokens and kv caches.
        if self.layer_idx == 0:
            past_key_value.seen_tokens = kv_seq_len
        past_key_value.key_cache[self.layer_idx] = key_states
        past_key_value.value_cache[self.layer_idx] = value_states

    else:
        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len,
                                     self.num_key_value_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len,
                                         self.num_key_value_heads, self.head_dim).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]

        if past_key_value is not None:
            if self.layer_idx is None:
                invalidInputError(False,
                                  "The cache structure has changed since version v4.36. "
                                  f"If you are using {self.__class__.__name__} for "
                                  "auto-regressive decodingwith k/v caching, please make sure "
                                  "to initialize the attention class with a layer index.")
            kv_seq_len += past_key_value.get_usable_length(kv_seq_len, self.layer_idx)

        if use_fuse_rope:
            query_states, key_states = apply_rotary_pos_emb_no_cache_xpu(query_states,
                                                                         key_states,
                                                                         position_ids,
                                                                         "mistral")
        else:
            cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
            query_states, key_states = apply_rotary_pos_emb(query_states, key_states,
                                                            cos, sin, position_ids, "mistral")

        if past_key_value is not None:
            # update the number of seen tokens
            if self.layer_idx == 0:
                past_key_value.seen_tokens += key_states.shape[-2]

            # reuse k, v, self_attention
            # update `past_key_value` with `key_states` and `value_states` for layer `layer_idx`
            if len(past_key_value.key_cache) <= self.layer_idx:
                past_key_value.key_cache.append(key_states)
                past_key_value.value_cache.append(value_states)
            else:
                cache_k = past_key_value.key_cache[self.layer_idx]
                cache_v = past_key_value.value_cache[self.layer_idx]

                if not enough_kv_room:
                    # allocate new
                    new_c_k, new_c_v = extend_kv_cache(bsz,
                                                       self.num_key_value_heads,  # Support GQA
                                                       self.head_dim,
                                                       cache_k.size(2),
                                                       kv_seq_len + KV_CACHE_ALLOC_BLOCK_LENGTH,
                                                       dtype=cache_k.dtype,
                                                       device=device)

                    new_c_k[:] = cache_k
                    new_c_v[:] = cache_v
                    cache_k = new_c_k
                    cache_v = new_c_v

                key_states, value_states = append_kv_cache(cache_k, cache_v,
                                                           key_states, value_states)

                # update past_key_value
                past_key_value.key_cache[self.layer_idx] = key_states
                past_key_value.value_cache[self.layer_idx] = value_states

    if not self.training and not hidden_states.requires_grad:
        fsdp_flag = use_flash_attention(query_states, key_states)
    else:
        fsdp_flag = False
    if fsdp_flag:
        attention_dtype = torch.float16  # use fp16 for flash attention
    else:
        attention_dtype = original_dtype

    # repeat k/v heads if n_kv_heads < n_heads
    key_states = repeat_kv(key_states, self.num_key_value_groups).to(device,
                                                                     dtype=attention_dtype)
    value_states = repeat_kv(value_states, self.num_key_value_groups).to(device,
                                                                         dtype=attention_dtype)

    if fsdp_flag:
        attn_output = F.scaled_dot_product_attention(query_states.to(dtype=attention_dtype),
                                                     key_states,
                                                     value_states,
                                                     is_causal=True)
        attn_weights = None
        attn_output = attn_output.transpose(1, 2).contiguous()
        attn_output = attn_output.reshape(bsz, q_len, hidden_size)
    else:
        attn_output, attn_weights = compute_attn_outputs_weights(query_states,
                                                                 key_states,
                                                                 value_states,
                                                                 bsz, q_len, kv_seq_len,
                                                                 self.num_heads,
                                                                 self.head_dim,
                                                                 self.hidden_size,
                                                                 attention_mask)

    attn_output = self.o_proj(attn_output)

    if not output_attentions:
        attn_weights = None

    return attn_output.to(original_dtype), attn_weights, past_key_value
