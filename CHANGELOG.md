# Changelog

## 0.8.0 (2026-11-23)

Focus: transparency of every deduplication decision, and keeping related but
distinct publications (preprints, conference abstracts, protocols, notices)
visible instead of merging them.

### Behaviour changes (these can change review outcomes)

- **Publication versions are never merged automatically.** Each record is now
  labelled as journal article, preprint, conference abstract, conference paper,
  protocol, trial registry record, thesis, correction/erratum, retraction,
  commentary, reply or expression of concern, with the evidence shown
  (publication type, preprint DOI prefix, source name, title marker, RIS type).
  Records of different versions are linked as **related publications** (rule V1)
  and both are kept by default.
- Fixed: a conference abstract and the full article (years one apart, same first
  author) were grouped as duplicates, so automatic mode removed one of them.
- Fixed: a preprint without a DOI was merged with the journal article as a
  "Corroborated fuzzy title" duplicate.
- Fixed: a protocol that matched its results paper was placed inside the
  duplicate group, so automatic mode removed it. It is now a related publication.
  (Weak, unpunctuated "protocol" title markers keep the v0.7.0 contextual
  "Potential protocol" flag inside a group.)
- A record whose type is stated (e.g. "Conference Abstract") and an untyped copy
  are grouped only when the DOI or journal/ISSN also agrees; otherwise they are
  shown as related (rule V2).
- Preprints, errata, protocols and registry records with their **own DOI** were
  previously kept apart silently. They are now linked to the article.
- A DOI-less record whose title matches records with several different DOIs is
  now a manual-review item (R4) instead of being silently skipped.
- Group conflicts are labelled "Group conflict" (R3) instead of the misleading
  "Uncorroborated title match".

### Transparency

- Central rule catalogue (`dedupper/rules.py`) with IDs D1–D4 (duplicate),
  R1–R4 (review), V1–V2 (related), S1–S6 (kept separate) and all thresholds.
  Shown on a new **How matching works** page (`/rules`) and in the audit JSON.
- Every group edge, review item, related link and near miss carries its rule ID
  and a **field-by-field comparison** (match / compatible / differs / conflict /
  missing) rendered as a table.
- Related versions of one work are shown as one **publication family** card,
  with a duplicate group counted as one version.
- New **"similar pairs kept separate"** list (near misses) with the reason.
- Each duplicate group explains why its recommended record was chosen,
  including when two copies tie and upload order decided.
- New exports: **decision log** (CSV/XLSX, one row per record with status and
  reason) and **removed records** (CSV/XLSX/RIS). Master and deduplicated
  exports gain columns `publication_version`, `publication_version_evidence`,
  `duplicate_group`, `dedup_status`, `dedup_reason`, `related_records`,
  `review_items` (appended after the existing columns); RIS gets an extra N1 note.
- Audit JSON adds the app version, thresholds, rules, publication versions,
  related links/families and near misses.

### Human review

- Manual-review items can now be acted on: keep all (default) or keep only one.
- Any version in a related-publication family can be excluded; at least one
  version must remain. Choices are remembered and explained in the decision log.

### Import

- PubMed **MEDLINE / .nbib** format (`PMID- ` tagged), as `.nbib` or `.txt`.
  PubMed publication types such as "Preprint", "Published Erratum" and
  "Clinical Trial Protocol" feed the version labels.

### Operations / DSRI

- `Dockerfile` (UBI 9 Python 3.12, non-root, arbitrary-UID safe, port 8080),
  `wsgi.py`, `gunicorn.conf.py`, `openshift/dedupper.yaml` (PVC, Deployment,
  Service, edge-TLS Route) and `DEPLOY_DSRI.md`.
- Environment variables: `DEDUPPER_JOB_ROOT` (the README already promised a
  configurable job root but no variable existed), `DEDUPPER_MAX_UPLOAD_MB`,
  `DEDUPPER_JOB_TTL_HOURS` (default 24), `DEDUPPER_SECURE_COOKIES`,
  `DEDUPPER_FUZZY_TITLE_THRESHOLD`, `DEDUPPER_RELATED_TITLE_THRESHOLD`, `PORT`,
  `GUNICORN_WORKERS`, `GUNICORN_TIMEOUT`.
- Uploaded data is deleted automatically after the TTL, and users can delete a
  job immediately ("Delete this job now").
- Basic security headers, `no-store` caching on job pages, HttpOnly/SameSite
  session cookie. `/health` reports the version.
- `gunicorn` added to the dependencies.

### Tests

- 60 → 108 tests. The four v0.7.0 protocol tests were updated to the new
  related-publication behaviour; new tests cover version detection, related
  families, near misses, rule IDs and comparisons, user decisions, exports,
  MEDLINE import, job expiry and environment configuration.
