FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SING_BOX_VERSION=1.12.17

WORKDIR /app

# Install gost and sing-box for proxy conversion
RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates tar \
    && wget -q https://github.com/ginuerzh/gost/releases/download/v2.11.5/gost-linux-amd64-2.11.5.gz -O /tmp/gost.gz \
    && gunzip /tmp/gost.gz \
    && chmod +x /tmp/gost \
    && mv /tmp/gost /usr/local/bin/gost \
    && wget -q https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-amd64.tar.gz -O /tmp/sing-box.tar.gz \
    && tar -xzf /tmp/sing-box.tar.gz -C /tmp \
    && mv /tmp/sing-box-${SING_BOX_VERSION}-linux-amd64/sing-box /usr/local/bin/sing-box \
    && chmod +x /usr/local/bin/sing-box \
    && apt-get purge -y wget \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY web_console/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY . /app

RUN mkdir -p /app/web_console/runtime/tasks

EXPOSE 8000

CMD ["uvicorn", "web_console.app:app", "--host", "0.0.0.0", "--port", "8000"]
