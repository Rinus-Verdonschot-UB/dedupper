"""Gunicorn settings, overridable with environment variables.

OpenShift runs the container with an arbitrary non-root UID, so the server
binds to an unprivileged port (8080 by default).
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = int(os.environ.get("GUNICORN_WORKERS", os.environ.get("WEB_CONCURRENCY", "2")))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
# Large uploads are parsed and analysed inside the request.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
# Do not log query strings or bodies; uploads may contain sensitive working data.
access_log_format = '%(h)s "%(m)s %(U)s" %(s)s %(b)s %(L)ss'
# /tmp is a memory-backed emptyDir on many clusters and always writable.
worker_tmp_dir = os.environ.get("GUNICORN_WORKER_TMP_DIR", "/tmp")
