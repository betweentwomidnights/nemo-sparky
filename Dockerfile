FROM vllm/vllm-openai:v0.20.0

RUN pip install --no-cache-dir "vllm[audio]"
