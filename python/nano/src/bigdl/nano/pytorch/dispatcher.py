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

import os
from importlib.util import find_spec
from bigdl.nano.pytorch.gpu_cpu import patch_cuda


_mapping_torch = None


def _get_patch_map():
    global _mapping_torch

    # decide if generate
    patch_lightning = find_spec("pytorch_lightning") is not None
    patch_torchvision = find_spec("torchvision") is not None
    patch_torch = patch_lightning or patch_torchvision

    if patch_torch and _mapping_torch is None:
        _mapping_torch = []
        if patch_lightning:
            import pytorch_lightning
            from bigdl.nano.pytorch import Trainer
            _mapping_torch += [
                [pytorch_lightning, "Trainer", Trainer, None],
            ]
        if patch_torchvision:
            import torchvision
            from bigdl.nano.pytorch.vision import transforms
            from bigdl.nano.pytorch.vision import datasets
            _mapping_torch += [
                [torchvision, "transforms", transforms, None],
                [torchvision, "datasets", datasets, None],
            ]

    if not patch_torch:
        _mapping_torch = []

    return _mapping_torch


def patch_torch(cuda_to_cpu: bool = True):
    """
    patch_torch is used to patch optimized torch classes to replace original ones.

    Optimized classes include:

    | 1. pytorch_lightning.Trainer -> bigdl.nano.pytorch.Trainer
    | 2. torchvision.transforms -> bigdl.nano.pytorch.vision.transforms
    | 3. torchvision.datasets -> bigdl.nano.pytorch.vision.datasets

    :param cuda_to_cpu: bool, make codes write for CUDA available for CPU if set to True.
           This feature is still experimental and only valid in python layer codes.
           Default to True.
    """
    if cuda_to_cpu:
        patch_cuda()
    mapping_torch = _get_patch_map()

    for mapping_iter in mapping_torch:
        if mapping_iter[3] is None:
            mapping_iter[3] = getattr(mapping_iter[0], mapping_iter[1], None)
        setattr(mapping_iter[0], mapping_iter[1], mapping_iter[2])

    # we need these environment variables to know whether we should
    # call this patch in subprocess when multi-instance training
    os.environ['BIGDL_NANO_PATCH_TORCH'] = '1'
    os.environ['BIGDL_NANO_PATCH_CUDA'] = '1' if cuda_to_cpu else '0'


def unpatch_torch():
    """unpatch_torch is used to unpatch optimized torch classes to original ones."""
    # TODO: unpatch_torch to support gpu-to-cpu unpatching

    mapping_torch = _get_patch_map()

    for mapping_iter in mapping_torch:
        setattr(mapping_iter[0], mapping_iter[1], mapping_iter[3])

    os.environ['BIGDL_NANO_PATCH_TORCH'] = '0'
    os.environ['BIGDL_NANO_PATCH_CUDA'] = '0'
