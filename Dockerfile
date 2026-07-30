FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    DEDUPPER_JOB_ROOT=/data/jobs

WORKDIR /opt/app-root/src

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

# OpenShift starts containers with an arbitrary UID in group 0.
RUN mkdir -p /data/jobs \
    && chgrp -R 0 /opt/app-root/src /data \
    && chmod -R g=u /opt/app-root/src /data

EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 300 --access-logfile - --error-logfile - run:app"]
