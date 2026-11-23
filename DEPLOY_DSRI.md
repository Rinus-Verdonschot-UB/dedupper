# Deploying Dedupper on DSRI (OpenShift)

## Updating an existing deployment that builds from GitHub

If DSRI builds from the GitHub repository (build config `dedupper-git`),
replace the files in your local clone with the files from the new zip, commit,
push, then rebuild:

```bash
oc start-build dedupper-git --follow
oc rollout status deployment/dedupper
```

The rest of this document covers first-time deployments.

The app listens on port **8080**, runs as a non-root user and works with
OpenShift's random UIDs. Keep **one replica**: jobs are stored as files on one
volume.

## Option A: build from this folder (no external registry needed)

```bash
oc login <DSRI API URL>          # copy the login command from the DSRI web console
oc project <your-project>

# 1. One-time: build config using the Dockerfile in this folder
oc new-build --name dedupper --strategy docker --binary
# 2. Upload this folder and build (repeat for every new version)
oc start-build dedupper --from-dir . --follow

# 3. Secret for signed sessions (one-time)
oc create secret generic dedupper-secret \
  --from-literal=DEDUPPER_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# 4. Point the manifest at the image you just built, then apply it
sed -i "s#REPLACE-PROJECT/dedupper:0.8.0#$(oc project -q)/dedupper:latest#" openshift/dedupper.yaml
oc apply -f openshift/dedupper.yaml
oc rollout status deployment/dedupper
oc get route dedupper -o jsonpath='{.spec.host}{"\n"}'
```

## Option B: you already have a Dedupper deployment from v0.7.0

If it was created with the Python S2I builder (`oc new-app python~...` or the
DSRI catalog), uploading the new source is enough: the S2I image now finds
`wsgi.py` and `gunicorn` and starts the app with them.

```bash
oc start-build <your-build-config> --from-dir . --follow
oc set env deployment/<name> \
  DEDUPPER_JOB_ROOT=/opt/app-root/data/jobs \
  DEDUPPER_SECRET_KEY_FILE=/opt/app-root/data/.secret-key \
  DEDUPPER_JOB_TTL_HOURS=24 \
  APP_CONFIG=gunicorn.conf.py
# Optional but recommended: a volume for the jobs folder
oc set volume deployment/<name> --add --name data --type pvc \
  --claim-name dedupper-data --claim-size 2Gi --mount-path /opt/app-root/data
oc scale deployment/<name> --replicas=1
```

Keep the existing `DEDUPPER_SECRET_KEY` if you already set one.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | 8080 | Port gunicorn binds to |
| `DEDUPPER_SECRET_KEY` | (key file) | Session signing key; set from a Secret |
| `DEDUPPER_JOB_ROOT` | `instance/jobs` | Where uploads and results are stored |
| `DEDUPPER_JOB_TTL_HOURS` | 24 | Jobs older than this are deleted on the next upload (0 = never) |
| `DEDUPPER_MAX_UPLOAD_MB` | 100 | Combined upload limit |
| `DEDUPPER_SECURE_COOKIES` | off | Set `true` behind the HTTPS route |
| `DEDUPPER_FUZZY_TITLE_THRESHOLD` | 92 | Changes duplicate outcomes; recorded in each audit |
| `DEDUPPER_RELATED_TITLE_THRESHOLD` | 88 | Changes related-publication links |
| `GUNICORN_WORKERS` / `GUNICORN_TIMEOUT` | 2 / 300 | Server tuning; the Route timeout is also 300 s |

## Check after deploying

- `https://<route>/health` returns `{"status": "ok", "version": "<the new version>"}`.
- Upload `sample_data/publication_versions.ris`. You should see one duplicate
  group (two copies of the journal article) and one related-publication family
  with a preprint, a conference abstract, a protocol and a correction notice.
