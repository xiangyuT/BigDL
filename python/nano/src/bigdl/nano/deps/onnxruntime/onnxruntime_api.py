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


def PytorchONNXRuntimeModel(model, input_sample=None,
                            onnxruntime_session_options=None,
                            simplification=True, **export_kwargs):
    """
        Create a ONNX Runtime model from pytorch.

        :param model: 1. Pytorch model to be converted to ONNXRuntime for inference.
                      2. Path to ONNXRuntime saved model.
        :param input_sample: A set of inputs for trace, defaults to None if you have trace before or
                             model is a LightningModule with any dataloader attached,
                             defaults to None.
        :param onnxruntime_session_options: A session option for onnxruntime accelerator.
        :param simplification: whether we use onnxsim to simplify the ONNX model, only valid when
                               accelerator='onnxruntime', otherwise will be ignored. If this option
                               is set to True, new dependency 'onnxsim' need to be installed.
        :param **export_kwargs: will be passed to torch.onnx.export function.
        :return: A PytorchONNXRuntimeModel instance
        """
    from .pytorch.pytorch_onnxruntime_model import PytorchONNXRuntimeModel
    return PytorchONNXRuntimeModel(model, input_sample,
                                   onnxruntime_session_options=onnxruntime_session_options,
                                   simplification=simplification, **export_kwargs)


def load_onnxruntime_model(path):
    from .pytorch.pytorch_onnxruntime_model import PytorchONNXRuntimeModel
    return PytorchONNXRuntimeModel._load(path)


def KerasONNXRuntimeModel(model, input_sample,
                          onnxruntime_session_options=None,
                          **export_kwargs):
    """
    Create a ONNX Runtime model from tensorflow.

    :param model: 1. Keras model to be converted to ONNXRuntime for inference
                    2. Path to ONNXRuntime saved model
    :param input_sample: (a tuple or list of) tf.TensorSpec or numpy array defining
        the shape/dtype of the input
    :param onnxruntime_session_options: will be passed to tf2onnx.convert.from_keras function
    """
    from .tensorflow.tensorflow_onnxruntime_model import KerasONNXRuntimeModel
    return KerasONNXRuntimeModel(model, input_sample,
                                 onnxruntime_session_options=onnxruntime_session_options,
                                 **export_kwargs)
