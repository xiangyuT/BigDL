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

from collections import namedtuple
import time
import numpy as np
from typing import Dict


_whole_acceleration_options = ["inc", "ipex", "onnxruntime", "openvino", "pot",
                               "bf16", "jit", "channels_last"]

CompareMetric = namedtuple("CompareMetric", ["method_name", "latency", "accuracy"])


class AccelerationOption(object):
    __slot__ = _whole_acceleration_options

    def __init__(self, **kwargs):
        '''
        initialize optimization option
        '''
        for option in _whole_acceleration_options:
            setattr(self, option, kwargs.get(option, False))
        self.method = kwargs.get("method", None)

    def get_precision(self):
        if self.inc or self.pot:
            return "int8"
        if self.bf16:
            return "bf16"
        return "fp32"

    def get_accelerator(self):
        if self.onnxruntime:
            return "onnxruntime"
        if self.openvino:
            return "openvino"
        if self.jit:
            return "jit"
        return None


def throughput_calculate_helper(iterrun, baseline_time, func, *args):
    '''
    A simple helper to calculate average latency
    '''
    start_time = time.perf_counter()
    time_list = []
    for i in range(iterrun):
        st = time.perf_counter()
        func(*args)
        end = time.perf_counter()
        time_list.append(end - st)
        # if three samples cost more than 4x time than baseline model, prune it
        if i == 2 and end - start_time > 12 * baseline_time:
            return np.mean(time_list) * 1000, False
        # at least need 10 iters and try to control calculation
        # time less than 10s
        if i + 1 >= min(iterrun, 10) and (end - start_time) > 10:
            iterrun = i + 1
            break
    time_list.sort()
    # remove top and least 10% data
    time_list = time_list[int(0.1 * iterrun): int(0.9 * iterrun)]
    return np.mean(time_list) * 1000, True


def format_acceleration_option(method_name: str,
                               full_methods: Dict[str, AccelerationOption]) -> str:
    '''
    Get a string represation for current method's acceleration option
    '''
    option = full_methods[method_name]
    repr_str = ""
    for key, value in option.__dict__.items():
        if value is True:
            if key == "pot":
                repr_str = repr_str + "int8" + " + "
            else:
                repr_str = repr_str + key + " + "
        elif isinstance(value, str):
            repr_str = repr_str + value + " + "
    if len(repr_str) > 0:
        repr_str = repr_str[:-2]
    return repr_str


def format_optimize_result(optimize_result_dict: dict,
                           calculate_accuracy: bool) -> str:
    '''
    Get a format string represation for optimization result
    '''
    if calculate_accuracy is True:
        horizontal_line = " {0} {1} {2} {3}\n" \
            .format("-" * 32, "-" * 22, "-" * 14, "-" * 22)
        repr_str = horizontal_line
        repr_str += "| {0:^30} | {1:^20} | {2:^12} | {3:^20} |\n" \
            .format("method", "status", "latency(ms)", "accuracy")
        repr_str += horizontal_line
        for method, result in optimize_result_dict.items():
            status = result["status"]
            latency = result.get("latency", "None")
            if latency != "None":
                latency = round(latency, 3)
            accuracy = result.get("accuracy", "None")
            if accuracy != "None" and isinstance(accuracy, float):
                accuracy = round(accuracy, 3)
            method_str = f"| {method:^30} | {status:^20} | " \
                         f"{latency:^12} | {accuracy:^20} |\n"
            repr_str += method_str
        repr_str += horizontal_line
    else:
        horizontal_line = " {0} {1} {2}\n" \
            .format("-" * 32, "-" * 22, "-" * 14)
        repr_str = horizontal_line
        repr_str += "| {0:^30} | {1:^20} | {2:^12} |\n" \
            .format("method", "status", "latency(ms)")
        repr_str += horizontal_line
        for method, result in optimize_result_dict.items():
            status = result["status"]
            latency = result.get("latency", "None")
            if latency != "None":
                latency = round(latency, 3)
            method_str = f"| {method:^30} | {status:^20} | {latency:^12} |\n"
            repr_str += method_str
        repr_str += horizontal_line
    return repr_str
