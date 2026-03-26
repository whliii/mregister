FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY mail_adapter/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY mail_adapter /app/mail_adapter

EXPOSE 8787

CMD ["uvicorn", "mail_adapter.gmail_relay_gptmail_adapter:app", "--host", "0.0.0.0", "--port", "8787"]
