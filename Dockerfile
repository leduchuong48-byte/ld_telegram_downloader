FROM python:3.11.9-alpine AS build-image

WORKDIR /app

COPY requirements.txt /app/

RUN apk add --no-cache --virtual .build-deps gcc musl-dev \
    && pip install --no-cache-dir --trusted-host pypi.python.org -r requirements.txt \
    && apk del .build-deps && rm -rf requirements.txt

RUN apk add --no-cache rclone

FROM python:3.11.9-alpine AS runtime-image

WORKDIR /app

COPY --from=build-image /usr/bin/rclone /app/rclone/rclone

COPY --from=build-image /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

COPY config.yaml data.yaml setup.py media_downloader.py /app/
COPY module /app/module
COPY utils /app/utils

CMD ["python", "media_downloader.py"]
