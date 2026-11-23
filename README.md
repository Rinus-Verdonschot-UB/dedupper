# Dedupper

Dedupper is a parsing-first Flask prototype for combining and deduplicating
scientific article exports. It accepts multiple RIS, numbered PubMed abstract
TXT, CSV, TSV, XLS and XLSX files, asks the user to verify uncertain tabular
mappings, builds a complete master dataset, reports per-file ingestion counts,
and only then proposes duplicate groups.

## New in v0.8.0

See `CHANGELOG.md` for details and `DEPLOY_DSRI.md` for deployment.

- Every record gets a **publication-version label** (journal article, preprint,
  conference abstract/paper, protocol, trial registry record, thesis,
  correction, retraction, commentary, reply, expression of concern) with the
  evidence behind it.
- Records of different versions are **never merged automatically**. They are
  shown together as a related-publication family; every version is kept unless
  the user excludes it.
- Every decision carries a **rule ID** (see `/rules`) and a field-by-field
  comparison. Near misses are listed with the reason they were kept apart.
- Decision log and removed-records exports; extra decision columns in every
  export.
- Manual-review items can be resolved in the interface.
- PubMed MEDLINE/.nbib import; automatic job expiry; Dockerfile, gunicorn and
  OpenShift manifests.

## What v0.7.0 does (still applies unless changed above)

- Accepts 1–10 files in one upload (100 MB combined limit).
- Parses RIS records while retaining repeated tags, continuation lines and the
  final record even if its `ER` tag is missing.
- Parses PubMed's numbered plain-text abstract format, including wrapped
  citations, language notes, affiliations, DOI, PMID and multi-line abstracts.
- Detects CSV/TSV encoding, delimiter and likely header row.
- Reads every non-empty worksheet in XLS/XLSX workbooks.
- Suggests columns for title, publication year, first author, the complete
  author list, DOI, journal, pages, article/e-location number, ISSN, ISBN,
  abstract and publication/document type using header aliases plus value-shape
  analysis.
- Requires a human mapping review for every tabular table. The header row can
  also be corrected and rescanned.
- Retains every non-empty data row, even when its title or DOI is missing.
- Decodes URL-escaped DOI suffixes and preserves legitimate parenthesized
  suffixes, including Elsevier `S0140-6736(09)...`-style identifiers.
- Preserves all original fields as JSON alongside source file, worksheet and
  row number.
- Promotes abstracts from PubMed TXT, RIS `AB`/`N2` tags and mapped tabular
  columns into a canonical field that survives deduplication.
- Promotes complete authors, pagination, article/e-location numbers, ISSN and
  ISBN from supported source metadata. These fields remain available in CSV,
  XLSX and RIS output and contribute corroborating match evidence.
- Promotes publication type from RIS `PT`/`DT`/`M3`, PubMed publication-type
  metadata and mapped CSV/Excel columns, and preserves it in every export.
- Uses multi-field evidence instead of grouping on a DOI or title alone. DOI,
  cleaned/fuzzy title, complete/first authors, year, journal, ISSN/ISBN, pages,
  article number and publication type can corroborate one another.
- Accepts a one-year publication difference only when independent metadata
  supports the match, covering ahead-of-print versus final-publication dates.
- Keeps different valid DOIs separate. An identical DOI with conflicting or
  insufficient metadata is labelled **DOI metadata conflict**, retained, and
  shown in a manual-review section instead of being removed automatically.
- Treats an identical cleaned title without any independent supporting field as
  an uncorroborated review candidate rather than an automatic duplicate.
- Prevents transitive grouping from bridging components whose known DOI, year,
  publication kind or ISBN metadata conflict.
- Recognizes proceedings DOIs shared by multiple conference abstracts within
  one source file. DOI alone is ignored in that situation; conference
  abstracts are grouped only when their titles are near-identical and their
  normalized first-author surnames agree. The same rule finds the corresponding
  abstract across two uploaded files without merging every abstract published
  under the same proceedings DOI.
- Reports the number of detected conference abstracts, protected same-source
  DOI clusters, distinct overlapping DOI values, and remaining likely
  conference-abstract duplicate groups on screen and in the audit JSON.
- Keeps related publications such as corrections, errata, commentaries,
  responses, retractions and expressions of concern separate from the original
  article, while still allowing duplicate copies of the related publication to
  match each other.
- Within an already detected duplicate group, marks a title as a potential
  protocol only when it has a conventional protocol marker and a highly similar
  non-protocol companion title. Potential protocols are never selected as the
  automatic recommendation while a matching non-protocol record is available.
- Lets the user keep one or several chosen records per group, using
  Ctrl/Cmd-click or Shift-click for multi-selection, or automatically retain
  the most informative record (valid DOI first).
- After a choice is applied, safely enriches each retained record from its
  duplicate copies: missing canonical fields are filled only when the source
  values do not disagree, and a longer title is used only when it is clearly
  compatible. Every copied or withheld field is recorded in the audit trail.
- Preserves the filename, worksheet and row number of every merged source in
  CSV/XLSX provenance JSON, RIS notes and the audit JSON.
- Displays pair-level evidence such as `Same DOI`, `title 98%`, `full author
  list match`, `year +1`, `ISSN match` and `pages match` inside each group.
- Collapses candidate groups after either deduplication action and brings the
  retained-record result and download action directly into view; the groups
  remain available behind a review toggle.
- Uses the Maastricht University Library digital house style: Open Sans-style
  typography, accessible UM navy, orange and light-blue colours, and a clear
  primary/secondary action hierarchy.
- Exports both the untouched master and deduplicated datasets as CSV, XLSX or
  RIS, plus a separate JSON audit file. CSV and XLSX retain the complete raw
  source-field JSON plus provenance and enrichment audits; RIS contains the
  standard canonical bibliographic fields, including authors, pagination,
  serial identifiers, publication type and merged-source notes when available.
- Counts records with normalization warnings per source; record-level warning
  text is included in the master export.

## Run locally

Python 3.10 or newer is required.

```bash
cd dedupper
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
flask --app run.py run --debug
```

Open <http://127.0.0.1:5000>. For a non-development deployment, set a stable
random `DEDUPPER_SECRET_KEY` and run the app through a production WSGI server.
If that variable is omitted, Dedupper now creates one stable key file for all
workers sharing the same container filesystem instead of generating a different
key in each worker.

## OpenShift

See `DEPLOY_DSRI.md` for the full procedure. In short: set `DEDUPPER_SECRET_KEY` to a stable random value in the Deployment. Jobs are
currently stored on the container filesystem, so use one replica unless
`JOB_ROOT` points to a persistent volume shared by all replicas:

```bash
oc set env deployment/<deployment-name> DEDUPPER_SECRET_KEY=<random-secret>
oc scale deployment/<deployment-name> --replicas=1
oc rollout status deployment/<deployment-name>
```

The stable secret prevents a mapping or deduplication request handled by a
different WSGI worker from losing the signed job session. A single replica
prevents a request from reaching another pod that cannot see the job's JSON
files.

## Tests

```bash
pytest
```

The tests cover malformed-but-recoverable RIS, repeated RIS tags, CSV preamble
and blank rows, Dutch headers, gibberish headers inferred from values,
multi-worksheet XLSX parsing, preservation of unmapped fields, DOI/title
normalization, corroborated DOI and title decisions, DOI metadata conflicts,
one-year publication tolerance, full-author comparison, pagination and serial
identifier parsing, transitive-conflict prevention, related-publication
safeguards, fuzzy grouping, contextual protocol detection, shared proceedings
DOI safeguards, cross-file
conference-abstract matching, conference audit statistics, single- and
multi-record retention, collapsed post-action review and an
end-to-end Flask upload, PubMed TXT ingestion, abstract preservation,
safe enrichment with provenance, CSV/XLSX/RIS export round trips, and a
two-file mapping request moving between separate WSGI app workers.

## Important interpretation of counts

For tabular files, a **logical item** is every non-empty row after the confirmed
header. Blank rows and non-empty preamble rows are reported separately. For RIS,
a logical item is every record delimited by `TY`/`ER`, including a recoverable
record that reaches end-of-file without `ER`. For PubMed TXT, a logical item is
every numbered PubMed citation header recognized in the export.

The parser does not reject a logical item merely because a canonical field is
empty. Therefore `logical items` should equal `parsed into master`; missing or
malformed mapped values become record-level warnings rather than silent losses.

## Current boundaries

Jobs are stored as JSON under
`instance/jobs` (or `DEDUPPER_JOB_ROOT`) and deleted after
`DEDUPPER_JOB_TTL_HOURS` (default 24), which makes the audit trail inspectable but is not intended
for multi-user production scale. Fuzzy title grouping is conservative and
should remain a reviewed candidate process. CSV files with several unrelated
tables in one file are not automatically split. Old `.xls` files depend on
`xlrd`; `.xlsx` files use `openpyxl`. TXT ingestion targets PubMed's numbered
abstract layout rather than arbitrary prose files.

Conference-abstract protection depends on an explicit publication type such as
`Conference Abstract` or `Meeting Abstract` (or an explicit type marker at the
end of the title). For CSV and Excel input, verify the optional publication /
document type mapping when the source provides one. Without type metadata, a
shared proceedings DOI cannot always be distinguished from an ordinary article
DOI safely.

The next accuracy step is a labelled validation corpus of known duplicate and
non-duplicate pairs from real exports (Scopus, Web of Science, PubMed, Embase,
PsycINFO, EndNote, Zotero and local institutional formats). Thresholds should
be tuned against measured false-positive and false-negative rates rather than
changed from isolated examples. A persistent database remains the main
production-scale engineering step.
