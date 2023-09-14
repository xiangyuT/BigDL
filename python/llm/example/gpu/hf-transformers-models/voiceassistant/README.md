# Voice Assistant
In this directory, you will find examples on how you could apply BigDL-LLM INT4 optimizations on Whisper and Llama2 models on [Intel GPUs](../README.md). For illustration purposes, we utilize the following models: 
- [openai/whisper-small](https://huggingface.co/openai/whisper-small) and [openai/whisper-medium](https://huggingface.co/openai/whisper-medium) as reference whisper models.
- [meta-llama/Llama-2-7b-chat-hf](https://huggingface.co/meta-llama/Llama-2-7b-chat-hf) and [meta-llama/Llama-2-13b-chat-hf](https://huggingface.co/meta-llama/Llama-2-13b-chat-hf) as reference Llama2 models.

## 0. Requirements
To run these examples with BigDL-LLM on Intel GPUs, we have some recommended requirements for your machine, please refer to [here](../README.md#recommended-requirements) for more information.

## Example: Predict Tokens using `generate()` API
In the example [generate.py](./generate.py), we show a basic use case for a Whisper model to conduct transcription using `generate()` API, then use the recoginzed text as the input for Llama2 model to predict the next N tokens using `generate()` API, with BigDL-LLM INT4 optimizations on Intel GPUs.
### 1. Install
We suggest using conda to manage environment:
```bash
conda create -n llm python=3.9
conda activate llm
# below command will install intel_extension_for_pytorch==2.0.110+xpu as default
# you can install specific ipex/torch version for your need
pip install --pre --upgrade bigdl-llm[xpu] -f https://developer.intel.com/ipex-whl-stable-xpu
pip install librosa soundfile datasets
pip install accelerate
pip install SpeechRecognition sentencepiece colorama
# If you failed to install PyAudio, try to run sudo apt install portaudio19-dev on ubuntu
pip install PyAudio inquirer sounddevice
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
python ./generate.py --llama2-repo-id-or-model-path REPO_ID_OR_MODEL_PATH --whisper-repo-id-or-model-path REPO_ID_OR_MODEL_PATH --n-predict N_PREDICT
```

Arguments info:
- `--llama2-repo-id-or-model-path REPO_ID_OR_MODEL_PATH`: argument defining the huggingface repo id for the Llama2 model (e.g. `meta-llama/Llama-2-7b-chat-hf` and `meta-llama/Llama-2-13b-chat-hf`) to be downloaded, or the path to the huggingface checkpoint folder. It is default to be `'meta-llama/Llama-2-7b-chat-hf'`.
- `--whisper-repo-id-or-model-path REPO_ID_OR_MODEL_PATH`: argument defining the huggingface repo id for the Whisper model (e.g. `openai/whisper-small` and `openai/whisper-medium`) to be downloaded, or the path to the huggingface checkpoint folder. It is default to be `'openai/whisper-small'`.
- `--n-predict N_PREDICT`: argument defining the max number of tokens to predict. It is default to be `32`.

#### Known Issues
The speech_recognition library may occasionally skip recording due to low volume. An alternative option is to save the recording in WAV format using `PyAudio` and read the file as an input. Here is an example using PyAudio:
```python
import pyaudio
import speech_recognition as sr

CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1                # The desired number of input channels
RATE = 16000                # The desired rate (in Hz)
RECORD_SECONDS = 10         # Recording time (in second)
WAVE_OUTPUT_FILENAME = "/path/to/pyaudio_out.wav"
p = pyaudio.PyAudio()
                
stream = p.open(format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK)

print("*"*10, "Listening\n")
frames = []
data =0
for i in range(0, int(RATE / CHUNK * RECORD_SECONDS)):
  data = stream.read(CHUNK)  ## <class 'bytes'> ,exception_on_overflow = False
  frames.append(data)   ## <class 'list'>
print("*"*10, "Stop recording\n")

stream.stop_stream()
stream.close()
p.terminate()

wf = wave.open(WAVE_OUTPUT_FILENAME, 'wb')
wf.setnchannels(CHANNELS)
wf.setsampwidth(p.get_sample_size(FORMAT))
wf.setframerate(RATE)
wf.writeframes(b''.join(frames))
wf.close()

r = sr.Recognizer()
with sr.AudioFile(WAVE_OUTPUT_FILENAME) as source1:
    audio = r.record(source1)  # read the entire audio file   
frame_data = np.frombuffer(audio.frame_data, np.int16).flatten().astype(np.float32) / 32768.0
```

#### Sample Output
```bash
(llm) bigdl@bigdl-llm:~/Documents/voiceassistant$ python generate.py --llama2-repo-id-or-model-path /mnt/windows/demo/models/Llama-2-7b-chat-hf --whisper-repo-id-or-model-path /mnt/windows/demo/models/whisper-medium
/home/bigdl/anaconda3/envs/llm/lib/python3.9/site-packages/torchvision/io/image.py:13: UserWarning: Failed to load image Python extension: ''If you don't plan on using image functionality from `torchvision.io`, you can ignore this warning. Otherwise, there might be something wrong with your environment. Did you have `libjpeg` or `libpng` installed before building `torchvision` from source?
  warn(

[?] Which microphone do you choose?: Default
 > Default
   HDA Intel PCH: ALC274 Analog (hw:0,0)
   HDA Intel PCH: HDMI 0 (hw:0,3)
   HDA Intel PCH: HDMI 1 (hw:0,7)
   HDA Intel PCH: HDMI 2 (hw:0,8)
   HDA Intel PCH: HDMI 3 (hw:0,9)
   HDA Intel PCH: HDMI 4 (hw:0,10)
   HDA Intel PCH: HDMI 5 (hw:0,11)
   HDA Intel PCH: HDMI 6 (hw:0,12)
   HDA Intel PCH: HDMI 7 (hw:0,13)
   HDA Intel PCH: HDMI 8 (hw:0,14)
   HDA Intel PCH: HDMI 9 (hw:0,15)
   HDA Intel PCH: HDMI 10 (hw:0,16)

The device name Default is selected.
Downloading builder script: 100%|██████████████████████████████████████████████████████| 5.17k/5.17k [00:00<00:00, 14.3MB/s]
Downloading data: 100%|████████████████████████████████████████████████████████████████████████████████████████| 9.08M/9.08M [00:01<00:00, 4.75MB/s]
Downloading data files: 100%|████████████████████████████████████████████████████████████████████████████████████████| 1/1 [00:04<00:00,  4.57s/it]]
Extracting data files: 100%|██████████████████████████████████████████████████████████████████████████████████████████| 1/1 [00:00<00:00, 39.98it/s]
Generating validation split: 73 examples [00:00, 5328.37 examples/s]
Converting and loading models...
Loading checkpoint shards: 100%|██████████████████████████████████████████████████████████████████████████████████████| 3/3 [00:09<00:00,  3.04s/it]
/home/bigdl/anaconda3/envs/yina-llm/lib/python3.9/site-packages/transformers/generation/configuration_utils.py:362: UserWarning: `do_sample` is set to `False`. However, `temperature` is set to `0.9` -- this flag is only used in sample-based generation modes. You should set `do_sample=True` or unset `temperature`. This was detected when initializing the generation config instance, which means the corresponding file may hold incorrect parameterization and should be fixed.
  warnings.warn(
/home/bigdl/anaconda3/envs/yina-llm/lib/python3.9/site-packages/transformers/generation/configuration_utils.py:367: UserWarning: `do_sample` is set to `False`. However, `top_p` is set to `0.6` -- this flag is only used in sample-based generation modes. You should set `do_sample=True` or unset `top_p`. This was detected when initializing the generation config instance, which means the corresponding file may hold incorrect parameterization and should be fixed.
  warnings.warn(
/home/bigdl/anaconda3/envs/yina-llm/lib/python3.9/site-packages/transformers/generation/utils.py:1411: UserWarning: You have modified the pretrained model configuration to control generation. This is a deprecated strategy to control generation and will be removed soon, in a future version. Please use a generation configuration file (see https://huggingface.co/docs/transformers/main_classes/text_generation )
  warnings.warn(
Calibrating...
Listening now...
Recognizing...

Whisper : 
 What is AI?

BigDL-LLM: 
 Artificial intelligence (AI) is the broader field of research and development aimed at creating machines that can perform tasks that typically require human intelligence,
Listening now...
Recognizing...

Whisper : 
 Tell me something about Intel

BigDL-LLM: 
 Intel is a well-known technology company that specializes in designing, manufacturing, and selling computer hardware components and semiconductor products.
```
