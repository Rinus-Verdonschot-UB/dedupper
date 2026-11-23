"""WSGI entry point for gunicorn / OpenShift S2I.

The Red Hat / UBI Python S2I image starts ``gunicorn wsgi`` automatically when
this file exists and gunicorn is installed; the Dockerfile does the same.
"""

from dedupper import create_app

application = app = create_app()
