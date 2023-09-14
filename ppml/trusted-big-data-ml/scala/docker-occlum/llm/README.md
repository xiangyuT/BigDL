# Trusted BigDL-LLM using fastchat with Occlum
For running fastchat using bigdl-llm transformers int4 in Occlum

## Prerequisites
1.Check SGX and Kubernetes env.

2.Pull image from dockerhub.
```bash
docker pull intelanalytics/bigdl-ppml-trusted-llm-fastchat-occlum:2.4.0-SNAPSHOT
```

## Deploy fastchat with openAI restful API

0. prepare model and models_path(host or nfs), change model_name with bigdl.
```bash
mv vicuna-7b-hf vicuna-7b-bigdl
```
1. get `controller-service.yaml` and `controller.yaml` and `worker.yaml`.
2. deploy controller-service and controller.
```bash
kubectl apply -f controller-service.yaml
kubectl apply -f controller.yaml
kcbectl get service | grep bigdl
# get controller-service's cluster-ip
```
3. modify `worker.yaml`, set CONTROLLER_HOST=controller-service's cluster-ip, set models mount path and `MODEL_PATH`.
```bash
kubectl apply -f worker.yaml
```
4. using openAI api to access
```
curl http://$controller_ip:8000/v1/models
# choose a model
curl http://$controller_ip:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "vicuna-7b-bigdl",
    "prompt": "Once upon a time",
    "max_tokens": 64,
    "temperature": 0.5
  }'
```
More api details refer to [here](https://github.com/lm-sys/FastChat/blob/main/docs/openai_api.md)