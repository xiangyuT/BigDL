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
# https://github.com/huggingface/transformers/blob/v4.30.2/src/transformers/utils/bitsandbytes.py
# and https://github.com/huggingface/transformers/blob/main/src/transformers/modeling_utils.py
# which is licensed under Apache License 2.0:
#
# Copyright 2021 The HuggingFace Inc. team. All rights reserved.
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


import platform
import torch
import torch.nn as nn
from accelerate import init_empty_weights
import warnings
import transformers
import importlib.util
from bigdl.llm.ggml.quantize import ggml_tensor_qtype
from .utils import logger
from typing import Union
import numpy as np
import os
from bigdl.llm.utils.common import invalidInputError


def is_auto_gptq_available():
    return importlib.util.find_spec("auto_gptq") is not None


def is_auto_awq_available():
    return importlib.util.find_spec("awq") is not None


def is_deepspeed_available():
    spec = importlib.util.find_spec("deepspeed")
    if spec is not None:
        deepspeed_path = spec.submodule_search_locations[0]
        if deepspeed_path != os.path.join(os.getcwd(), "deepspeed"):
            return True
        else:
            # not deepspeed package, just local dir
            return False
    else:
        return False


if is_auto_gptq_available():
    from auto_gptq.utils.peft_utils import QuantLinearCuda, QuantLinearCudaOld

if is_auto_awq_available():
    from bigdl.llm.transformers.awq.linear import WQLinear_GEMM
    from transformers.utils.quantization_config import AwqBackendPackingMethod


def is_linear_module(module):

    in_features = None
    out_features = None
    mp_group = None

    is_awq = is_auto_awq_available() and isinstance(module, WQLinear_GEMM)

    if is_auto_gptq_available() and isinstance(module, QuantLinearCudaOld):
        in_features = module.infeatures
        out_features = module.outfeatures
        mp_group = None
        result = True
    elif isinstance(module, nn.Linear) or is_awq:
        in_features = module.in_features
        out_features = module.out_features
        mp_group = None
        result = True
    else:
        if is_deepspeed_available():
            from deepspeed.module_inject.layers import LinearLayer, LinearAllreduce
            if isinstance(module, LinearLayer):
                in_features = module.weight.shape[1]
                out_features = module.weight.shape[0]
                mp_group = None
                result = True
            elif isinstance(module, LinearAllreduce):
                in_features = module.weight.shape[1]
                out_features = module.weight.shape[0]
                mp_group = module.mp_group
                result = True
            else:
                result = False
        else:
            result = False

    return result, (in_features, out_features, mp_group)


def convert_gptq(module, awq=False, llm_awq=False):
    from bigdl.llm.transformers.low_bit_linear import get_block_size
    Q4_1 = get_block_size("asym_int4")

    scales = module.scales

    zeros = torch.bitwise_right_shift(
        torch.unsqueeze(module.qzeros, 2).expand(-1, -1, 32 // module.bits),
        module.wf.unsqueeze(0)).to(torch.int16 if module.bits == 8 else torch.int8)
    zeros = torch.bitwise_and(zeros, (2 ** module.bits) - 1)

    if not awq:
        zeros = zeros + 1
    zeros = zeros.reshape(scales.shape)

    if awq:
        weight = torch.bitwise_right_shift(
            torch.unsqueeze(module.qweight, 2).expand(-1, -1, 32 // module.bits),
            module.wf.unsqueeze(0)).to(torch.int16 if module.bits == 8 else torch.int8)
        weight = torch.bitwise_and(weight, (2 ** module.bits) - 1)
        weight = weight.reshape(weight.shape[0], weight.shape[1] * weight.shape[2])
        if llm_awq:
            weight = weight.t()
    else:
        weight = torch.bitwise_right_shift(
            torch.unsqueeze(module.qweight, 1).expand(-1, 32 // module.bits, -1),
            module.wf.unsqueeze(-1)).to(torch.int8)
        weight = torch.bitwise_and(weight, (2 ** module.bits) - 1)
        weight = weight.reshape(weight.shape[0] * weight.shape[1], weight.shape[2])

    # convert weight to ggml format
    weight = weight.reshape(weight.shape[0]//module.group_size, module.group_size, weight.shape[1])
    weight = weight.permute(2, 0, 1).reshape(weight.shape[2], -1, 2, Q4_1//2)
    weight = weight.transpose(2, 3)
    weight = torch.bitwise_left_shift(weight,
                                      torch.tensor([0, 4], dtype=torch.int8).reshape(1, 1, 1, 2))
    weight = torch.bitwise_or(weight[:, :, :, 0], weight[:, :, :, 1]).contiguous()

    # convert zeros to ggml format
    if llm_awq:
        real_scale_num = module.in_features // module.group_size
        zeros = zeros[:, : real_scale_num]
        scales = scales[:, : real_scale_num]
        zeros = zeros.t()
        scales = scales.t()
    zeros = zeros.reshape(-1, 1, zeros.shape[1]).permute(2, 0, 1)\
        .unsqueeze(2)\
        .expand(-1, -1, module.group_size//Q4_1, -1)\
        .reshape(zeros.shape[1], -1, 1)\
        .contiguous().to(torch.float16)

    # convert scales to ggml format
    scales = scales.reshape(-1, 1, scales.shape[1]).permute(2, 0, 1)\
        .unsqueeze(2)\
        .expand(-1, -1, module.group_size//Q4_1, -1)\
        .reshape(scales.shape[-1], -1, 1)\
        .contiguous().to(torch.float16)

    m = -(zeros * scales)
    d = scales

    ggml_weight = torch.cat([d.view(torch.uint8),
                             m.view(torch.uint8),
                             weight.view(torch.uint8)], dim=-1)
    ggml_weight = ggml_weight.reshape([-1])

    return ggml_weight


def _replace_with_low_bit_linear(model, qtype, modules_to_not_convert=None,
                                 current_key_name=None, convert_shape_only=False,
                                 cpu_embedding=False):
    from bigdl.llm.transformers.low_bit_linear import LowBitLinear, FP4Params, \
        FP16Linear, BF16Linear
    from bigdl.llm.transformers.embedding import LLMEmbedding
    has_been_replaced = False

    for name, module in model.named_children():
        if current_key_name is None:
            current_key_name = []

        is_linear, linear_args = is_linear_module(module)
        if is_linear and name not in modules_to_not_convert:
            # Check if the current key is not in the `modules_to_not_convert`
            if not any(key in ".".join(current_key_name) for key in modules_to_not_convert):
                in_features, out_features, mp_group = linear_args
                with init_empty_weights():
                    new_linear = None
                    is_gptq = is_auto_gptq_available() and isinstance(module, QuantLinearCudaOld)
                    is_awq = is_auto_awq_available() and isinstance(module, WQLinear_GEMM)
                    is_llm_awq = is_awq and module.backend == AwqBackendPackingMethod.LLMAWQ
                    if is_gptq or is_awq:
                        has_bias = module.bias is not None and module.bias.abs().sum() != 0
                        new_linear = LowBitLinear(
                            in_features,
                            out_features,
                            qtype=qtype,
                            bias=has_bias,
                            mp_group=mp_group,
                        )
                        device = module.qweight.data.device
                        invalidInputError(device.type != "meta",
                                          "converting from meta device is not supported")
                        # Copy the weights
                        paramsLowBit = FP4Params(data=convert_gptq(module, awq=is_awq,
                                                                   llm_awq=is_llm_awq),
                                                 requires_grad=False,
                                                 quantized=True,
                                                 _shape=(out_features, in_features),
                                                 convert_shape_only=convert_shape_only,
                                                 qtype=qtype).to(device)
                        new_linear._parameters['weight'] = paramsLowBit
                        if has_bias:
                            new_linear._parameters['bias'] = nn.Parameter(module.bias.data)\
                                .to(device)
                    elif qtype not in [ggml_tensor_qtype["fp16"], ggml_tensor_qtype["bf16"]]:
                        new_linear = LowBitLinear(
                            in_features,
                            out_features,
                            qtype,
                            module.bias is not None,
                            mp_group=mp_group,
                        )

                        device = module.weight.data.device
                        # Copy the weights
                        paramsLowBit = FP4Params(data=module.weight.data,
                                                 requires_grad=False,
                                                 quantized=False,
                                                 _shape=None,
                                                 convert_shape_only=convert_shape_only,
                                                 qtype=qtype).to(device)
                        new_linear._parameters['weight'] = paramsLowBit
                        if module.bias is not None:
                            new_linear._parameters['bias'] = nn.Parameter(module.bias.data)\
                                .to(device)
                    elif qtype == ggml_tensor_qtype["fp16"]:
                        module.to(torch.float16)
                        new_linear = FP16Linear(
                            in_features,
                            out_features,
                            module.bias is not None,
                            mp_group=mp_group,
                        )
                        device = module.weight.data.device
                        from bigdl.llm.transformers.utils import get_ipex_version
                        if get_ipex_version() < "2.1.10+xpu":
                            new_linear._parameters['weight'] = nn.Parameter(module.weight)
                        else:
                            # only from 2.1, ipex provides matmul_bias_out
                            # so we need to transpose weight
                            new_weight = module.weight.transpose(0, 1).contiguous()
                            new_linear._parameters['weight'] = nn.Parameter(new_weight)
                            new_linear.weight_type = 2
                        if module.bias is not None:
                            new_linear._parameters['bias'] = nn.Parameter(module.bias.data)\
                                .to(device)
                    elif qtype == ggml_tensor_qtype["bf16"]:
                        module.to(torch.bfloat16)
                        new_linear = BF16Linear(
                            in_features,
                            out_features,
                            module.bias is not None,
                            mp_group=mp_group,
                        )
                        device = module.weight.data.device
                        # convert here
                        new_linear._parameters['weight'] = nn.Parameter(module.weight)
                        if module.bias is not None:
                            new_linear._parameters['bias'] = nn.Parameter(module.bias.data)\
                                .to(device)

                    if new_linear is not None:
                        if not module.training:
                            new_linear.eval()
                        model._modules[name] = new_linear
                        has_been_replaced = True
                        # Force requires grad to False to avoid unexpected errors
                        model._modules[name].requires_grad_(False)

                        module.weight = None
        elif cpu_embedding and type(module) == nn.Embedding:
            # skip user-defined Embedding layer
            if platform.system().lower() == 'windows':
                model._modules[name] = LLMEmbedding(
                    num_embeddings=module.num_embeddings,
                    embedding_dim=module.embedding_dim,
                    padding_idx=module.padding_idx,
                    max_norm=module.max_norm,
                    norm_type=module.norm_type,
                    scale_grad_by_freq=module.scale_grad_by_freq,
                    sparse=module.sparse,
                    _weight=module.weight.data,
                )

        # Remove the last key for recursion
        if len(list(module.children())) > 0:
            _, _flag = _replace_with_low_bit_linear(
                module,
                qtype,
                modules_to_not_convert,
                current_key_name,
                convert_shape_only,
                cpu_embedding,
            )
            has_been_replaced = _flag or has_been_replaced
    return model, has_been_replaced


def replace_with_low_bit_linear_for_module(model, qtype, module_name=None,
                                           modules_to_not_convert=None, current_key_name=None,
                                           convert_shape_only=False, torch_dtype="auto"):
    from bigdl.llm.transformers.low_bit_linear import LowBitLinear, FP4Params, \
        FP16Linear, BF16Linear
    has_been_replaced = False

    if "." in module_name:
        splits = module_name.split(".")
    parent_module = getattr(model, splits[0])

    if "lm_head" not in module_name:
        for split in splits[1:-2]:
            new_module = getattr(parent_module, split)
            parent_module = new_module
        module = getattr(parent_module, splits[-2])
        module_name = splits[-2]
    else:
        module = parent_module
        parent_module = model
        module_name = splits[0]

    if current_key_name is None:
        current_key_name = []

    if modules_to_not_convert is None:
        modules_to_not_convert = []

    is_linear, linear_args = is_linear_module(module)
    if is_linear and module_name not in modules_to_not_convert:
        # Check if the current key is not in the `modules_to_not_convert`
        if (not any(key in ".".join(current_key_name) for key in modules_to_not_convert) and
                module.weight.data.device.type != 'meta' and not isinstance(module, LowBitLinear)):
            in_features, out_features, mp_group = linear_args
            with init_empty_weights():
                new_linear = None
                is_gptq = is_auto_gptq_available() and isinstance(module, QuantLinearCudaOld)
                is_awq = is_auto_awq_available() and isinstance(module, WQLinear_GEMM)
                is_llm_awq = is_awq and module.backend == AwqBackendPackingMethod.LLMAWQ
                if is_gptq or is_awq:
                    has_bias = module.bias is not None and module.bias.abs().sum() != 0
                    new_linear = LowBitLinear(
                        in_features,
                        out_features,
                        qtype=qtype,
                        bias=has_bias,
                        mp_group=mp_group,
                    )
                    device = module.qweight.data.device
                    invalidInputError(device.type != "meta",
                                      "converting from meta device is not supported")
                    # Copy the weights
                    paramsLowBit = FP4Params(data=convert_gptq(module, awq=is_awq,
                                                               llm_awq=is_llm_awq),
                                             requires_grad=False,
                                             quantized=True,
                                             _shape=(out_features, in_features),
                                             convert_shape_only=convert_shape_only,
                                             qtype=qtype).to(device)
                    new_linear._parameters['weight'] = paramsLowBit
                    if has_bias:
                        new_linear._parameters['bias'] = nn.Parameter(module.bias.data)\
                            .to(device)
                elif qtype not in [ggml_tensor_qtype["fp16"], ggml_tensor_qtype["bf16"]]:
                    new_linear = LowBitLinear(
                        in_features,
                        out_features,
                        qtype,
                        module.bias is not None,
                        mp_group=mp_group,
                    )

                    device = module.weight.data.device
                    # Copy the weights
                    paramsLowBit = FP4Params(data=module.weight.data,
                                             requires_grad=False,
                                             quantized=False,
                                             _shape=None,
                                             convert_shape_only=convert_shape_only,
                                             qtype=qtype).to(device)
                    new_linear._parameters['weight'] = paramsLowBit
                    if module.bias is not None:
                        new_linear._parameters['bias'] = nn.Parameter(module.bias.data)\
                            .to(device)
                elif qtype == ggml_tensor_qtype["fp16"]:
                    module.to(torch.float16)
                    new_linear = FP16Linear(
                        in_features,
                        out_features,
                        module.bias is not None,
                        mp_group=mp_group,
                    )
                    device = module.weight.data.device
                    from bigdl.llm.transformers.utils import get_ipex_version
                    if get_ipex_version() < "2.1.10+xpu":
                        new_linear._parameters['weight'] = nn.Parameter(module.weight)
                    else:
                        # only from 2.1, ipex provides matmul_bias_out
                        # so we need to transpose weight
                        new_weight = module.weight.transpose(0, 1).contiguous()
                        new_linear._parameters['weight'] = nn.Parameter(new_weight)
                        new_linear.weight_type = 2
                    if module.bias is not None:
                        new_linear._parameters['bias'] = nn.Parameter(module.bias.data)\
                            .to(device)
                elif qtype == ggml_tensor_qtype["bf16"]:
                    module.to(torch.bfloat16)
                    new_linear = BF16Linear(
                        in_features,
                        out_features,
                        module.bias is not None,
                        mp_group=mp_group,
                    )
                    device = module.weight.data.device
                    # convert here
                    new_linear._parameters['weight'] = nn.Parameter(module.weight)
                    if module.bias is not None:
                        new_linear._parameters['bias'] = nn.Parameter(module.bias.data)\
                            .to(device)

                if new_linear is not None:
                    if not module.training:
                        new_linear.eval()
                    parent_module._modules[module_name] = new_linear
                    has_been_replaced = True
                    # Force requires grad to False to avoid unexpected errors
                    parent_module._modules[module_name].requires_grad_(False)

                    module.weight = None

    if has_been_replaced:
        if not (getattr(model, "quantization_method", None) == "gptq"):
            if torch_dtype == "auto":
                convert_bigdl_other_module(model, torch.float32)
            else:
                convert_bigdl_other_module(model, torch_dtype)
    return model


def _optimize_pre(model):
    from transformers.modeling_utils import PreTrainedModel
    # All huggingface format models are inherited from `PreTrainedModel`
    if not isinstance(model, PreTrainedModel):
        logger.info("Only HuggingFace Transformers models are currently "
                    "supported for further optimizations")
        return model
    # for rwkv models (verified RWKV/rwkv-4-world-7b)
    if model.config.model_type == "rwkv":
        model.rwkv._rescale_layers()
    # process NormHead module in Baichuan2 7B and 13B
    if model.config.model_type == "baichuan" and model.config.vocab_size == 125696:
        # NormHead do normalization on the weights just once at inference time.
        # so we do it in advance and convert it to Linear so that it can be replaced.
        # modeling_module_name = model.__class__.__module__
        # module = importlib.import_module(modeling_module_name)
        if hasattr(model, 'lm_head') and model.lm_head is not None:
            # do we need to check the class instance?
            vocab_size, hidden_size = model.lm_head.weight.shape
            lm_head_weight_data = model.lm_head.weight.data
            model.lm_head = nn.Linear(hidden_size, vocab_size, bias=False,
                                      device=lm_head_weight_data.device)
            # In which case we are NOT loading the normalized weights
            if model.lm_head.weight.data.device != "meta":
                norm_weight = nn.functional.normalize(lm_head_weight_data)
                model.lm_head.weight.data = norm_weight
    return model


def ggml_convert_low_bit(model, qtype, optimize_model=True,
                         convert_shape_only=False, device="cpu",
                         modules_to_not_convert=None, cpu_embedding=False,
                         lightweight_bmm=False, torch_dtype="auto"):
    logger.info(f"Converting the current model to "
                f"{list(ggml_tensor_qtype.keys())[list(ggml_tensor_qtype.values()).index(qtype)]} "
                f"format......")
    modules_to_not_convert = [] if modules_to_not_convert is None else modules_to_not_convert

    if optimize_model:
        model = _optimize_pre(model)

    model, has_been_replaced = _replace_with_low_bit_linear(
        model, qtype, modules_to_not_convert,
        None, convert_shape_only, cpu_embedding,
    )
    if not has_been_replaced:
        warnings.warn(
            "No linear modules were found in "
            "your model. This can happen for some architectures such as gpt2 that uses Conv1D "
            "instead of Linear layers. Please double check your model architecture, or submit "
            "an issue on github if you think this is a bug."
        )
    elif device == "cpu":
        if not (getattr(model, "quantization_method", None) == "gptq"):
            if torch_dtype == "auto":
                convert_bigdl_other_module(model, torch.float32)
            else:
                convert_bigdl_other_module(model, torch_dtype)
    elif device == "meta":
        # Do nothing here for weights are empty.
        pass

    if optimize_model:
        model = _optimize_post(model, lightweight_bmm, qtype)
    return model


def convert_bigdl_other_module(model, dtype):
    # Convert modules outside of bigdl linear to corresponding dtype
    from bigdl.llm.transformers.low_bit_linear import LowBitLinear, \
        FP16Linear, BF16Linear
    for module in model.modules():
        if list(module.children()) == []:
            # leaf module
            if not isinstance(module, (LowBitLinear, FP16Linear, BF16Linear)):
                module.to(dtype)


def convert_forward(m, target_m, new_forward):
    for _, sub_m in m.named_children():
        if isinstance(sub_m, target_m):
            bound_method = new_forward.__get__(sub_m, sub_m.__class__)
            setattr(sub_m, "forward", bound_method)
        convert_forward(sub_m, target_m, new_forward)


# ipex
def replace_func(m, target_m, func_name, new_func):
    for _, sub_m in m.named_children():
        if isinstance(sub_m, target_m):
            bound_method = new_func.__get__(sub_m, sub_m.__class__)
            setattr(sub_m, func_name, bound_method)
        replace_func(sub_m, target_m, func_name, new_func)


def lowering_class_cpu(m, target_m, new_class, config, tpp=False, woq=False):
    for name, sub_m in m.named_children():
        if isinstance(sub_m, target_m):
            new_m = new_class(sub_m, config, tpp, woq)
            setattr(m, name, new_m)
        lowering_class_cpu(sub_m, target_m, new_class, config, tpp, woq)

def convert_class(m, target_m, new_class, config, distributed=False):
    for name, sub_m in m.named_children():
        if isinstance(sub_m, target_m):
            new_m = new_class(sub_m, config, distributed)
            setattr(m, name, new_m)
        convert_class(sub_m, target_m, new_class, config, distributed)

def _set_optimized_model_for_generation(
    model,
    optimized_model,
    first_token_optimized_model=None,
):
    from intel_extension_for_pytorch.transformers.models.reference.models import IPEX_LLM_Model_Return

    if first_token_optimized_model is not None:
        model.trace_graph_first = IPEX_LLM_Model_Return(
            model, first_token_optimized_model
        ).forward

    model.trace_graph = IPEX_LLM_Model_Return(model, optimized_model).forward
    print(
        "ipex.llm.optimize has set the optimized or quantization model for model.generate()"
    )
    return model

def _ipex_optimize_decoder(model, decoder_layer):
    from intel_extension_for_pytorch.transformers.models.reference.modules.decoder import _IPEXDecoderLayerRef
    from intel_extension_for_pytorch.transformers.models.cpu.modules.decoder import _IPEXDecoderLayerCPU

    for supported_mlp_class in [_IPEXDecoderLayerRef]:
        lowering_class_cpu(
            model,
            supported_mlp_class,
            _IPEXDecoderLayerCPU,
            model.config,
            tpp=False,
            woq=False,
        )
    convert_class(
        model,
        decoder_layer,
        _IPEXDecoderLayerRef,
        model.config,
        distributed=True,
    )

def _ipex_optimize_attention(model, attention_layer):
    from intel_extension_for_pytorch.transformers.models.reference.modules.attentions import _IPEXAttentionRef
    from intel_extension_for_pytorch.transformers.models.cpu.modules.attentions import _IPEXAttentionCPU
    for supported_mha_class in [_IPEXAttentionRef]:
        lowering_class_cpu(
            model,
            supported_mha_class,
            _IPEXAttentionCPU,
            model.config,
            tpp=False,
            woq=False,
        )
    convert_class(
        model,
        attention_layer,
        _IPEXAttentionRef,
        model.config,
        distributed=True,
    )

def _ipex_jit(model):
    from intel_extension_for_pytorch.transformers.optimize import get_dummy_input
    sample_inputs = (
        get_dummy_input(model, return_dict=True)
    )
    with torch.no_grad(), torch.cpu.amp.autocast(
        enabled=True
    ):
        trace_model = torch.jit.trace(
            model,
            example_kwarg_inputs=sample_inputs,
            strict=False,
            check_trace=False,
        )
        trace_model = torch.jit.freeze(trace_model)
        model = _set_optimized_model_for_generation(
            model, optimized_model=trace_model
        )

    return model.eval()

def _optimize_post(model, lightweight_bmm=False, qtype="auto"):
    from packaging import version
    from bigdl.llm.transformers.models.llama import llama_attention_forward_4_31
    from bigdl.llm.transformers.models.llama import llama_attention_selective_batching_forward_4_31
    from bigdl.llm.transformers.models.llama import llama_model_selective_batching_forward_4_31
    from bigdl.llm.transformers.models.llama import llama_rms_norm_forward
    from bigdl.llm.transformers.models.llama import llama_mlp_forward
    from transformers.modeling_utils import PreTrainedModel

    # All huggingface format models are inherited from `PreTrainedModel`
    if not isinstance(model, PreTrainedModel):
        logger.info("Only HuggingFace Transformers models are currently "
                    "supported for further optimizations")
        return model

    vllm_selective_batching = os.getenv("VLLM_ENABLE_SELECTIVE_BATCHING")
    enable_vllm_se_batching = vllm_selective_batching is not None
    enable_vllm_se_batching = enable_vllm_se_batching and vllm_selective_batching.lower() == "true"

    
    _enable_ipex = os.getenv("ENABLE_IPEX")
    _enable_ipex = (_enable_ipex is not None) and (_enable_ipex.lower() == "true") 
    _enable_ipex = _enable_ipex and (qtype == ggml_tensor_qtype["bf16"])
    print('ENABLE_IPEX: ', _enable_ipex)

    trans_version = transformers.__version__
    if version.parse(trans_version) >= version.parse("4.31.0"):
        if _enable_ipex:
            import intel_extension_for_pytorch as ipex
            from intel_extension_for_pytorch.transformers.models.cpu.fusions.mha_fusion import _IPEXRMSNorm
            from intel_extension_for_pytorch.transformers.models.cpu.modules.decoder import _IPEXDecoderLayerCPU
            from intel_extension_for_pytorch.transformers.models.cpu.modules.attentions import _IPEXAttentionCPU
            from intel_extension_for_pytorch.transformers.models.reference.modules.decoder import _IPEXDecoderLayerRef
            from intel_extension_for_pytorch.transformers.models.reference.modules.attentions import _IPEXAttentionRef
            from intel_extension_for_pytorch.cpu._auto_kernel_selection import _enable_tpp
            from intel_extension_for_pytorch.transformers.optimize import model_convert_reference, get_dummy_input

            # convert_forward(
            #     model,
            #     transformers.models.llama.modeling_llama.LlamaRMSNorm,
            #     llama_rms_norm_forward,)

            # amp_dtype = getattr(torch, 'bfloat16')
            # model = ipex.llm.optimize(
            #             model.eval(),
            #             dtype=amp_dtype,
            #             inplace=True,
            #             deployment_mode=True,
            # )

            # _enable_tpp()
            # model = ipex.optimize(model.eval(), dtype=torch.bfloat16, inplace=True)
            model = model_convert_reference(model)

            # lowering_class_cpu(
            #         model,
            #         transformers.models.llama.modeling_llama.LlamaRMSNorm,
            #         _IPEXRMSNorm,
            #         model.config,
            #         tpp=False,
            #         woq=False,
            #     )
            # convert_class(
            #     model,
            #     transformers.models.llama.modeling_llama.LlamaRMSNorm,
            #     _IPEXRMSNorm,
            #     model.config,
            #     distributed=True,
            # )
            
            _ipex_optimize_attention(model, transformers.models.llama.modeling_llama.LlamaAttention)
            _ipex_optimize_decoder(model, transformers.models.llama.modeling_llama.LlamaDecoderLayer)
            from intel_extension_for_pytorch.transformers.models.reference.models import output_hook
            model.register_forward_hook(output_hook, with_kwargs=True)
            # for supported_mlp_class in [_IPEXDecoderLayerRef]:
            #     lowering_class_cpu(
            #         model,
            #         supported_mlp_class,
            #         _IPEXDecoderLayerCPU,
            #         model.config,
            #         tpp=False,
            #         woq=False,
            #     )
            # convert_class(
            #     model,
            #     transformers.models.llama.modeling_llama.LlamaDecoderLayer,
            #     _IPEXDecoderLayerRef,
            #     model.config,
            #     distributed=True,
            # )

            # for supported_mha_class in [_IPEXAttentionRef]:
            #     lowering_class_cpu(
            #         model,
            #         supported_mha_class,
            #         _IPEXAttentionCPU,
            #         model.config,
            #         tpp=False,
            #         woq=False,
            #     )
            # convert_class(
            #     model,
            #     transformers.models.llama.modeling_llama.LlamaAttention,
            #     _IPEXAttentionRef,
            #     model.config,
            #     distributed=True,
            # )
            

            # sample_inputs = (
            #     get_dummy_input(model, return_dict=True)
            # )
            # with torch.no_grad(), torch.cpu.amp.autocast(
            #     enabled=True
            # ):
            #     trace_model = torch.jit.trace(
            #         model,
            #         example_kwarg_inputs=sample_inputs,
            #         strict=False,
            #         check_trace=False,
            #     )
            #     trace_model = torch.jit.freeze(trace_model)
            #     model = _set_optimized_model_for_generation(
            #         model, optimized_model=trace_model
            #     )

            return _ipex_jit(model)
        else:
            convert_forward(
                model,
                transformers.models.llama.modeling_llama.LlamaRMSNorm,
                llama_rms_norm_forward,)

            convert_forward(model,
                            transformers.models.llama.modeling_llama.LlamaMLP,
                            llama_mlp_forward)
                    
            if version.parse(trans_version) >= version.parse("4.36.0"):
                # transformers version >= 4.36.0
                from bigdl.llm.transformers.models.llama import llama_attention_forward_4_36
                convert_forward(
                    model,
                    transformers.models.llama.modeling_llama.LlamaAttention,
                    llama_attention_forward_4_36, )
            else:
                # transformers version between 4.31.0 - 4.35.2
                convert_forward(
                    model,
                    transformers.models.llama.modeling_llama.LlamaAttention,
                    llama_attention_forward_4_31, )
                if enable_vllm_se_batching:
                    convert_forward(
                        model,
                        transformers.models.llama.modeling_llama.LlamaModel,
                        llama_model_selective_batching_forward_4_31,
                    )
                    convert_forward(
                        model,
                        transformers.models.llama.modeling_llama.LlamaAttention,
                        llama_attention_selective_batching_forward_4_31,
                    )
            # def _set_optimized_model_for_generation(
            #     model,
            #     optimized_model,
            #     first_token_optimized_model=None,
            # ):
            #     from intel_extension_for_pytorch.transformers.models.reference.models import IPEX_LLM_Model_Return

            #     if first_token_optimized_model is not None:
            #         model.trace_graph_first = IPEX_LLM_Model_Return(
            #             model, first_token_optimized_model
            #         ).forward

            #     model.trace_graph = IPEX_LLM_Model_Return(model, optimized_model).forward
            #     print(
            #         "ipex.llm.optimize has set the optimized or quantization model for model.generate()"
            #     )
            #     return model
            
            # def get_dummy_input(_model, return_dict=False):
            #     sample_inputs = None
            #     if hasattr(_model.config, "n_layer"):
            #         model_num_layers = _model.config.n_layer
            #     elif hasattr(_model.config, "num_hidden_layers"):
            #         model_num_layers = _model.config.num_hidden_layers
            #     elif hasattr(_model.config, "num_layers"):
            #         model_num_layers = _model.config.num_layers
            #     elif hasattr(_model.config, "n_layers"):
            #         model_num_layers = _model.config.n_layers

            #     input_ids = torch.ones(32).to(torch.long)
            #     model_inputs = _model.prepare_inputs_for_generation(input_ids.unsqueeze(0))
            #     has_position_ids = "position_ids" in model_inputs
            #     attention_mask = torch.ones(len(input_ids))
            #     position_ids = torch.arange(len(input_ids))
            #     past_key_values = [(torch.zeros(1, 40, 0, 128).contiguous(), torch.zeros(1, 40, 0, 128).contiguous()) for _ in range(model_num_layers)]
            #     # past_key_values = DynamicCache.from_legacy_cache(past_key_values)

            #     # past_key_values = tuple(past_key_values)
            #     # past_key_values = torch.zeros(model_num_layers, 2, 1, 40, 0, 128)
            #     # past_key_values = None
            #     if has_position_ids:
            #         sample_inputs = (
            #             {
            #                 "input_ids": input_ids.unsqueeze(0),
            #                 "attention_mask": attention_mask.unsqueeze(0),
            #                 "position_ids": position_ids.unsqueeze(0),
            #                 "past_key_values": past_key_values,
            #             }
            #             if return_dict
            #             else (
            #                 input_ids.unsqueeze(0),
            #                 attention_mask.unsqueeze(0),
            #                 position_ids.unsqueeze(0),
            #                 past_key_values,
            #             )
            #         )
            #     if "return_last_logit" in model_inputs:
            #         sample_inputs["return_last_logit"] = torch.tensor(True)
            #     return sample_inputs

            # sample_inputs = (
            #     get_dummy_input(model, return_dict=True)
            # )

            # with torch.no_grad(), torch.cpu.amp.autocast(
            #     enabled=True
            # ):
            #     trace_model = torch.jit.trace(
            #         model,
            #         example_kwarg_inputs=sample_inputs,
            #         strict=False,
            #         check_trace=False,
            #     )
            #     trace_model = torch.jit.freeze(trace_model)
            #     model = _set_optimized_model_for_generation(
            #         model, optimized_model=trace_model
            #     )

            return model
    else:
        # todo implement 4.28.0 ~ 4.30.2
        pass

    # convert all nn.LayerNorm
    from bigdl.llm.transformers.models.bloom import bloom_layer_norm_forward
    convert_forward(model,
                    nn.LayerNorm,
                    bloom_layer_norm_forward)

    if model.config.architectures is not None \
       and model.config.architectures[0] in ["ChatGLMModel", "ChatGLMForConditionalGeneration"]:
        if model.config.num_layers == 28 and hasattr(model.config, 'rope_ratio'):
            # chatglm2-6b-32k
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            from bigdl.llm.transformers.models.chatglm2_32k import chatglm2_32k_attention_forward
            convert_forward(model,
                            module.SelfAttention,
                            chatglm2_32k_attention_forward)
        elif hasattr(model.config, 'padded_vocab_size') and \
                model.config.padded_vocab_size == 65024:
            # chatglm2-6b
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            from bigdl.llm.transformers.models.chatglm2 import chatglm2_attention_forward_8eb45c
            from bigdl.llm.transformers.models.chatglm2 import core_attn_forward_8eb45c
            from bigdl.llm.transformers.models.chatglm2 import chatglm_rms_norm_forward
            from bigdl.llm.transformers.models.chatglm2 import chatglm2_model_forward
            convert_forward(model,
                            module.SelfAttention,
                            chatglm2_attention_forward_8eb45c
                            )
            convert_forward(model,
                            module.CoreAttention,
                            core_attn_forward_8eb45c)
            convert_forward(model,
                            module.ChatGLMModel,
                            chatglm2_model_forward)
            convert_forward(model,
                            module.RMSNorm,
                            chatglm_rms_norm_forward)
        elif hasattr(model.config, 'vocab_size') and model.config.vocab_size == 130528:
            # chatglm-6b
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            from bigdl.llm.transformers.models.chatglm import chatglm_attention_forward
            convert_forward(model,
                            module.SelfAttention,
                            chatglm_attention_forward
                            )
    elif "mpt" in model.config.model_type:
        if model.config.architectures is not None:
            modeling_module_name = model.__class__.__module__
            attention_module_name = '.'.join(modeling_module_name.split('.')[:-1]) + ".attention"
            module = importlib.import_module(attention_module_name)
            from bigdl.llm.transformers.models.mpt import mpt_multihead_attention_forward
            convert_forward(model,
                            module.MultiheadAttention,
                            mpt_multihead_attention_forward
                            )
    elif "gptj" in model.config.model_type:
        # dolly-v1-6b
        modeling_module_name = model.__class__.__module__
        module = importlib.import_module(modeling_module_name)
        from bigdl.llm.transformers.models.gptj import gptj_attention_forward
        convert_forward(model,
                        module.GPTJAttention,
                        gptj_attention_forward)
    elif "bloom" in model.config.model_type:
        modeling_module_name = model.__class__.__module__
        module = importlib.import_module(modeling_module_name)
        from bigdl.llm.transformers.models.bloom import bloom_attention_forward
        convert_forward(model,
                        module.BloomAttention,
                        bloom_attention_forward
                        )
    elif "falcon" in model.config.model_type or "RefinedWeb" in model.config.model_type:
        if model.config.architectures is not None:
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            if "RWForCausalLM" in model.config.architectures:
                if model.config.hidden_size == 4544:
                    # falcon-7b need to check performance drop after kv cache support.
                    # from bigdl.llm.transformers.models.falcon import rw_attention_forward_7b
                    # convert_forward(model,
                    #                 module.Attention,
                    #                 rw_attention_forward_7b
                    #                 )
                    pass
                else:
                    # falcon-40b
                    from bigdl.llm.transformers.models.falcon import rw_attention_forward_40b
                    convert_forward(model,
                                    module.Attention,
                                    rw_attention_forward_40b
                                    )
            elif "FalconForCausalLM" in model.config.architectures:
                if model.config.hidden_size != 4544:
                    # falcon-180b and new falcon-40b
                    from bigdl.llm.transformers.models.falcon import falcon_attention_forward
                    convert_forward(model,
                                    module.FalconAttention,
                                    falcon_attention_forward
                                    )
    elif model.config.model_type == "baichuan" and model.config.vocab_size == 125696:
        # baichuan2
        if model.config.hidden_size == 4096:
            # baichuan2-7B
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            from bigdl.llm.transformers.models.baichuan2 import baichuan_attention_forward_7b
            from bigdl.llm.transformers.models.baichuan2 import baichuan_mlp_forward
            convert_forward(model,
                            module.Attention,
                            baichuan_attention_forward_7b
                            )
            convert_forward(model,
                            module.RMSNorm,
                            llama_rms_norm_forward)
            convert_forward(model,
                            module.MLP,
                            baichuan_mlp_forward)
        elif model.config.hidden_size == 5120:
            # baichuan2-13B
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            from bigdl.llm.transformers.models.baichuan2 import baichuan_attention_forward_13b
            from bigdl.llm.transformers.models.baichuan2 import baichuan_13b_rms_norm_forward
            from bigdl.llm.transformers.models.baichuan2 import baichuan_mlp_forward
            from bigdl.llm.transformers.models.baichuan2 import baichuan_13b_get_alibi_mask

            if _enable_ipex:
                model = model_convert_reference(model)
                from intel_extension_for_pytorch.transformers.models.reference.models import output_hook
                model.register_forward_hook(output_hook, with_kwargs=True)
                return _ipex_jit(model)
            else:
                convert_forward(model,
                                module.BaichuanAttention,
                                baichuan_attention_forward_13b
                                )
                # baichuan2-13B's RMSNorm is a little different
                convert_forward(model,
                                module.RMSNorm,
                                baichuan_13b_rms_norm_forward)
                convert_forward(model,
                                module.MLP,
                                baichuan_mlp_forward)
                replace_func(model,
                             module.BaichuanModel,
                             "get_alibi_mask",
                             baichuan_13b_get_alibi_mask)
            

    elif model.config.model_type == "baichuan":
        # baichuan1
        if model.config.hidden_size == 4096:
            # baichuan-7B
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            from bigdl.llm.transformers.models.baichuan import baichuan_attention_forward_7b
            convert_forward(model,
                            module.Attention,
                            baichuan_attention_forward_7b
                            )
            convert_forward(model,
                            module.RMSNorm,
                            llama_rms_norm_forward)
        elif model.config.hidden_size == 5120:
            # baichuan-13B
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            from bigdl.llm.transformers.models.baichuan import baichuan_attention_forward_13b
            from bigdl.llm.transformers.models.baichuan2 import baichuan_13b_rms_norm_forward
            convert_forward(model,
                            module.BaichuanAttention,
                            baichuan_attention_forward_13b
                            )
            # baichuan-13B's RMSNorm is a little different
            convert_forward(model,
                            module.RMSNorm,
                            baichuan_13b_rms_norm_forward)
    elif model.config.model_type == "gpt_neox":
        from bigdl.llm.transformers.models.gptneox import gptneox_attention_forward
        convert_forward(model,
                        transformers.models.gpt_neox.modeling_gpt_neox.GPTNeoXAttention,
                        gptneox_attention_forward
                        )
    elif model.config.model_type == "internlm":
        modeling_module_name = model.__class__.__module__
        module = importlib.import_module(modeling_module_name)
        from bigdl.llm.transformers.models.internlm import internlm_attention_forward
        convert_forward(model,
                        module.InternLMAttention,
                        internlm_attention_forward
                        )
        convert_forward(model,
                        module.InternLMRMSNorm,
                        llama_rms_norm_forward
                        )
    elif model.config.model_type == "qwen":
        if hasattr(model.config, "visual"):
            # for Qwen-VL-Chat
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            from bigdl.llm.transformers.models.qwen_vl import qwen_attention_forward_vl
            convert_forward(model,
                            module.QWenAttention,
                            qwen_attention_forward_vl
                            )
        else:
            # for Qwen-7B and Qwen-14B
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            from bigdl.llm.transformers.models.qwen import qwen_attention_forward
            from bigdl.llm.transformers.models.qwen import qwen_mlp_forward
            from bigdl.llm.transformers.models.chatglm2 import chatglm_rms_norm_forward
            convert_forward(model,
                            module.QWenAttention,
                            qwen_attention_forward
                            )
            convert_forward(model,
                            module.RMSNorm,
                            chatglm_rms_norm_forward)
            convert_forward(model,
                            module.QWenMLP,
                            qwen_mlp_forward)
    elif model.config.model_type == "aquila":
        modeling_module_name = model.__class__.__module__
        module = importlib.import_module(modeling_module_name)
        from bigdl.llm.transformers.models.aquila import aquila_attention_forward
        convert_forward(model,
                        module.AquilaAttention,
                        aquila_attention_forward
                        )
        convert_forward(model,
                        module.AquilaRMSNorm,
                        llama_rms_norm_forward)
    elif model.config.model_type == "mixtral":
        # For mistralai/Mixtral-8x7B-v0.1
        invalidInputError(version.parse(trans_version) >= version.parse("4.36.0"),
                          "Please upgrade transformers to 4.36.0 or higher version "
                          "to run Mixtral models.")
        modeling_module_name = model.__class__.__module__
        module = importlib.import_module(modeling_module_name)
        from bigdl.llm.transformers.models.mixtral import mixtral_moeblock_forward, \
            mixtral_attention_forward, mixtral_mlp_forward
        convert_forward(model,
                        module.MixtralAttention,
                        mixtral_attention_forward)
        convert_forward(model,
                        module.MixtralRMSNorm,
                        llama_rms_norm_forward)
        convert_forward(model,
                        module.MixtralSparseMoeBlock,
                        mixtral_moeblock_forward)
        convert_forward(model,
                        module.MixtralBLockSparseTop2MLP,
                        mixtral_mlp_forward)
    elif model.config.model_type == "mistral":
        if model.config.architectures is not None and \
                model.config.architectures[0] == "MixtralForCausalLM":
            # For DiscoResearch/mixtral-7b-8expert
            invalidInputError(version.parse(trans_version) >= version.parse("4.36.0"),
                              "Please upgrade transformers to 4.36.0 or higher version "
                              "to run Mixtral models.")
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            convert_forward(model,
                            module.MistralRMSNorm,
                            llama_rms_norm_forward)
        else:
            if version.parse(trans_version) >= version.parse("4.36.0"):
                modeling_module_name = model.__class__.__module__
                module = importlib.import_module(modeling_module_name)
                from bigdl.llm.transformers.models.mistral import mistral_attention_forward_4_36
                convert_forward(model,
                                module.MistralAttention,
                                mistral_attention_forward_4_36
                                )
                convert_forward(model,
                                module.MistralRMSNorm,
                                llama_rms_norm_forward)
                convert_forward(model,
                                module.MistralMLP,
                                llama_mlp_forward)
            else:
                modeling_module_name = model.__class__.__module__
                module = importlib.import_module(modeling_module_name)
                from bigdl.llm.transformers.models.mistral import mistral_attention_forward
                convert_forward(model,
                                module.MistralAttention,
                                mistral_attention_forward
                                )
                convert_forward(model,
                                module.MistralRMSNorm,
                                llama_rms_norm_forward)
                convert_forward(model,
                                module.MistralMLP,
                                llama_mlp_forward)
    elif model.config.model_type == "Yi":
        modeling_module_name = model.__class__.__module__
        module = importlib.import_module(modeling_module_name)
        convert_forward(model,
                        module.YiRMSNorm,
                        llama_rms_norm_forward)
    elif model.config.model_type == "whisper" and lightweight_bmm:
        if platform.system().lower() == 'windows':
            from bigdl.llm.transformers.bmm import SafeBMM
            modeling_module_name = model.__class__.__module__
            module = importlib.import_module(modeling_module_name)
            old_fwd = module.WhisperAttention.forward

            def safe_bmm_fwd(*args, **kwargs):
                with SafeBMM():
                    return old_fwd(*args, **kwargs)

            convert_forward(model,
                            module.WhisperAttention,
                            safe_bmm_fwd)
    elif model.config.model_type == "rwkv":
        # rwkv v4
        modeling_module_name = model.__class__.__module__
        module = importlib.import_module(modeling_module_name)
        from bigdl.llm.transformers.models.rwkv4 import rwkv_attention_forward
        convert_forward(model,
                        module.RwkvSelfAttention,
                        rwkv_attention_forward)
    return model
