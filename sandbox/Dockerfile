FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl wget vim nano less tree jq \
      ca-certificates procps iproute2 dnsutils \
      build-essential \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask

WORKDIR /workspace

COPY server.py /srv/server.py

EXPOSE 8000
CMD ["python", "/srv/server.py"]
