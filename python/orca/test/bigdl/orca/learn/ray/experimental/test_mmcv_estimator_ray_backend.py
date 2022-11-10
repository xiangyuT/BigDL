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

import unittest
import os
import random
import shutil
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
from torchvision.datasets import CIFAR10

from mmcv.runner import EpochBasedRunner
from mmcv.utils import get_logger
from bigdl.orca import init_orca_context, stop_orca_context
from bigdl.orca.learn.pytorch.experimential.mmcv.mmcv_ray_estimator import MMCVRayEstimator

resource_path = os.path.join(
    os.path.realpath(os.path.dirname(__file__)), "../../../resources")


class Model(nn.Module):

    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(50, 50)
        self.relu1 = nn.ReLU()
        self.dout = nn.Dropout(0.2)
        self.fc2 = nn.Linear(50, 100)
        self.prelu = nn.PReLU(1)
        self.out = nn.Linear(100, 1)
        self.out_act = nn.Sigmoid()
        self.loss_fn = nn.BCELoss()

    def forward(self, input_):
        a1 = self.fc1(input_)
        h1 = self.relu1(a1)
        dout = self.dout(h1)
        a2 = self.fc2(dout)
        h2 = self.prelu(a2)
        a3 = self.out(h2)
        y = self.out_act(a3)
        return y

    def train_step(self, data, optimizer):
        features, labels = data
        predicts = self(features)  # -> self.__call__() -> self.forward()
        loss = self.loss_fn(predicts, labels)
        return {'loss': loss}


def runner_creator(config):
    model = Model()

    optimizer = optim.SGD(model.parameters(), lr=0.001, momentum=0.9)
    logger = get_logger('mmcv')
    # runner is a scheduler to manage the training
    runner = EpochBasedRunner(
        model,
        optimizer=optimizer,
        work_dir='./work_dir',
        logger=logger,
        max_epochs=4)

    # learning rate scheduler config
    lr_config = dict(policy='step', step=[2, 3])
    # configuration of optimizer
    optimizer_config = dict(grad_clip=None)
    # save log periodically and multiple hooks can be used simultaneously
    log_config = dict(interval=4, hooks=[dict(type='TextLoggerHook')])
    # register hooks to runner and those hooks will be invoked automatically
    runner.register_training_hooks(
        lr_config=lr_config,
        optimizer_config=optimizer_config,
        log_config=log_config)

    return runner


class LinearDataset(torch.utils.data.Dataset):
    """y = a * x + b"""

    def __init__(self, size=1000):
        X1 = torch.randn(size // 2, 50)
        X2 = torch.randn(size // 2, 50) + 1.5
        self.x = torch.cat([X1, X2], dim=0)
        Y1 = torch.zeros(size // 2, 1)
        Y2 = torch.ones(size // 2, 1)
        self.y = torch.cat([Y1, Y2], dim=0)

    def __getitem__(self, index):
        return self.x[index, None], self.y[index, None]

    def __len__(self):
        return len(self.x)


def train_dataloader_creator(config):
    train_set = LinearDataset()
    train_loader = DataLoader(
        train_set, batch_size=64, shuffle=True, num_workers=2)
    return train_loader


class TestMMCVRayEstimator(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        init_orca_context(cores=8, memory="8g")

    @classmethod
    def tearDownClass(cls) -> None:
        stop_orca_context()

    def setUp(self) -> None:
        self.estimator = MMCVRayEstimator(
            mmcv_runner_creator=runner_creator,
            config={}
        )

    def test_fit(self):
        self.estimator.fit([train_dataloader_creator], [('train', 1)])

    def test_run(self):
        self.estimator.run([train_dataloader_creator], [('train', 1)])


if __name__ == "__main__":
    unittest.main()
