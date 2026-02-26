FROM python:3.11.9-alpine AS build-image

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_FALLBACK_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt /app/

RUN set -eux; \
    apk add --no-cache --virtual .build-deps gcc musl-dev; \
    pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt \
    || pip install --no-cache-dir --index-url "${PIP_FALLBACK_INDEX_URL}" -r requirements.txt; \
    apk del .build-deps; \
    rm -f requirements.txt

RUN apk add --no-cache rclone

FROM python:3.11.9-alpine AS runtime-image

WORKDIR /app

COPY --from=build-image /usr/bin/rclone /app/rclone/rclone

COPY --from=build-image /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

COPY config.yaml data.yaml setup.py media_downloader.py /app/
COPY module /app/module
COPY utils /app/utils

# Ensure runtime directories exist
RUN mkdir -p /app/sessions /app/configs /app/downloads /app/temp /app/log

CMD ["python", "media_downloader.py"]
