# Dedupper container image for Maastricht DSRI / OpenShift.
#
# Based on Red Hat UBI Python, which runs as a non-root user and works with
# OpenShift's arbitrary UIDs (the user is always in group 0).
FROM registry.access.redhat.com/ubi9/python-312:latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    DEDUPPER_JOB_ROOT=/opt/app-root/data/jobs \
    DEDUPPER_SECRET_KEY_FILE=/opt/app-root/data/.secret-key

WORKDIR /opt/app-root/src

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY --chown=1001:0 . .

USER 0
RUN mkdir -p /opt/app-root/data/jobs /opt/app-root/src/instance \
 && chgrp -R 0 /opt/app-root/data /opt/app-root/src \
 && chmod -R g=u /opt/app-root/data /opt/app-root/src
USER 1001

EXPOSE 8080
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:application"]
