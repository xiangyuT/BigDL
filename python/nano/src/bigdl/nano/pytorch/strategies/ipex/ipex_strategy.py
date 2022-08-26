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

from contextlib import contextmanager
from functools import partial
from logging import warning
from typing import Any, Union, Callable

import torch
from torch.nn import Module
from torch.optim import Optimizer, LBFGS
from bigdl.nano.pytorch.utils import TORCH_VERSION_LESS_1_12

import pytorch_lightning as pl
from pytorch_lightning.strategies import SingleDeviceStrategy
from pytorch_lightning.accelerators.accelerator import Accelerator
from pytorch_lightning.plugins.precision import PrecisionPlugin
from pytorch_lightning.utilities import AMPType

from bigdl.nano.utils.log4Error import invalidInputError
import intel_extension_for_pytorch as ipex
from intel_extension_for_pytorch.optim._optimizer_utils import IPEX_FUSED_OPTIMIZER_LIST

from .ipex_accelerator import IPEXAccelerator


class IPEXStrategy(SingleDeviceStrategy):
    """IPEX strategy."""

    strategy_name = "ipex"

    def __init__(
        self,
        accelerator: Accelerator = IPEXAccelerator(),
        precision_plugin: PrecisionPlugin = PrecisionPlugin(),
        dtype=None,
    ) -> None:
        """
        Create a IPEXStrategy.

        :param accelerator: the accelerator to handle hardware
        :param precision_plugin: the plugin to handle precision-specific parts
        """
        self.dtype = dtype

        if self.dtype == torch.bfloat16 and isinstance(precision_plugin, PrecisionPlugin):
            precision_plugin = IPEXBF16Precision()
        super().__init__(accelerator=accelerator, precision_plugin=precision_plugin)

    def setup(self, trainer: pl.Trainer) -> None:
        """
        Setup plugins for the trainer fit and creates optimizers.

        We add IPEX's optimizations here.

        :param trainer: the trainer instance
        """
        super().setup(trainer)

        if len(self.optimizers) == 0:
            ipex.optimize(self.model, inplace=True, dtype=self.dtype)
        elif len(self.optimizers) == 1:
            ipex.optimize(self.model, optimizer=self.optimizers[0], inplace=True, dtype=self.dtype)
        else:
            invalidInputError(False, "Ipex does not support more than one optimizers.")


class IPEXBF16Precision(PrecisionPlugin):
    """Create Precision Plugin for IPEX BFloat16."""

    precision: Union[str, int] = 'bf16'

    @contextmanager
    def forward_context(self):
        """AMP for managing model forward/training_step/evaluation_step/predict_step."""
        with torch.cpu.amp.autocast():
            yield

    def optimizer_step(self,
                       model: Union["pl.LightningModule", Module],
                       optimizer: Optimizer,
                       optimizer_idx: int,
                       closure: Callable[[], Any],
                       **kwargs: Any) -> Any:
        """Hook to run the optimizer step."""
        if type(optimizer) in IPEX_FUSED_OPTIMIZER_LIST:
            return super().optimizer_step(model, optimizer, optimizer_idx, closure, **kwargs)

        if isinstance(model, pl.LightningModule):
            closure = partial(self._wrap_closure, model, optimizer, optimizer_idx, closure)

        # Only `torch.optim.LBFGS`  need to reevaluate closure multiple times
        # in optimizer.step(...) now.
        if isinstance(optimizer, LBFGS):
            invalidInputError(False,
                              "IPEX BFloat16 and the LBFGS optimizer are not compatible "
                              f"(optimizer {optimizer_idx}",
                              "Hint: Set 'use_ipex' to False or not set 'precision' to 'bf16'"
                              " if LBFGS optimizer is necessary")

        # Detect custom optimzer
        if type(optimizer).__name__ not in dir(torch.optim):
            warning("Seems like you are using a custom optimizer,"
                    "please make sure that 'optimizer.step(closure)'"
                    " does not need to be called in training stage")

        # For optimizer not in IPEX_FUSED_OPTIMIZER_LIST,
        # `closure()` needs to be called to backward the loss to avoid `.grad` being None
        closure_result = closure()
        optimizer.step(**kwargs)

        return closure_result
