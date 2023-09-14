# InternLM
In this directory, you will find examples on how you could apply BigDL-LLM INT4 optimizations on InternLM models on [Intel GPUs](../README.md). For illustration purposes, we utilize the [internlm/internlm-chat-7b-8k](https://huggingface.co/internlm/internlm-chat-7b-8k) as a reference InternLM model.

## 0. Requirements
To run these examples with BigDL-LLM on Intel GPUs, we have some recommended requirements for your machine, please refer to [here](../README.md#recommended-requirements) for more information.

## Example: Predict Tokens using `generate()` API
In the example [generate.py](./generate.py), we show a basic use case for a InternLM model to predict the next N tokens using `generate()` API, with BigDL-LLM INT4 optimizations on Intel GPUs.
### 1. Install
We suggest using conda to manage environment:
```bash
conda create -n llm python=3.9
conda activate llm
# below command will install intel_extension_for_pytorch==2.0.110+xpu as default
# you can install specific ipex/torch version for your need
pip install --pre --upgrade bigdl-llm[xpu] -f https://developer.intel.com/ipex-whl-stable-xpu
```

### 2. Configures OneAPI environment variables
```bash
source /opt/intel/oneapi/setvars.sh
```

### 3. Run

For optimal performance on Arc, it is recommended to set several environment variables.

```bash
export USE_XETLA=OFF
export SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1
```

```
python ./generate.py --repo-id-or-model-path REPO_ID_OR_MODEL_PATH --prompt PROMPT --n-predict N_PREDICT
```

Arguments info:
- `--repo-id-or-model-path REPO_ID_OR_MODEL_PATH`: argument defining the huggingface repo id for the InternLM model (e.g. `internlm/internlm-chat-7b-8k`) to be downloaded, or the path to the huggingface checkpoint folder. It is default to be `'internlm/internlm-chat-7b-8k'`.
- `--prompt PROMPT`: argument defining the prompt to be infered (with integrated prompt format for chat). It is default to be `'AI是什么？'`.
- `--n-predict N_PREDICT`: argument defining the max number of tokens to predict. It is default to be `32`.

#### Sample Output
#### [internlm/internlm-chat-7b-8k](https://huggingface.co/internlm/internlm-chat-7b-8k)
```log
Inference time: xxxx s
-------------------- Prompt --------------------
<|User|>:AI是什么？
<|Bot|>:
-------------------- Output --------------------
<|User|>:AI是什么？
<|Bot|>:AI是人工智能的缩写，是计算机科学的一个分支，旨在使计算机能够像人类一样思考、学习和执行任务。AI技术包括机器学习、自然
```

```log
Inference time: xxxx s
-------------------- Prompt --------------------
<|User|>:What is AI?
<|Bot|>:
-------------------- Output --------------------
<|User|>:What is AI?
<|Bot|>:AI is the ability of machines to perform tasks that would normally require human intelligence, such as perception, reasoning, learning, and decision-making. AI is made possible
```
