from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.styles import Alignment, Font, PatternFill
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.exceptions import NotFound
from werkzeug.utils import secure_filename

from .decisions import (
    ANNOTATION_FIELDS,
    DECISION_LOG_FIELDS,
    build_annotations,
    decision_log_rows,
    removal_reasons,
)
from .dedup import (
    COMPARED_FIELDS,
    FIELD_LABELS,
    analyze_records,
    apply_cluster_decisions,
    apply_group_selections,
    apply_family_decisions,
    build_review_clusters,
    apply_pair_decisions,
    conference_abstract_report,
    enrich_retained_records,
)
from .rules import RULES, settings_from_env
from .versions import VERSION_LABELS
from .mapping import column_profiles, infer_mapping
from .models import CANONICAL_FIELDS, SourceTable
from .parsers import (
    SUPPORTED_EXTENSIONS,
    ParseError,
    parse_uploaded_file,
    rebuild_source_table,
    table_to_records,
)
from .storage import JobStore

__version__ = "0.8.3"

EXPORT_FIELDS = [
    "record_id",
    "source_filename",
    "source_sheet",
    "source_row",
    "title",
    "year",
    "first_author",
    "authors",
    "doi",
    "journal",
    "pages",
    "article_number",
    "issn",
    "isbn",
    "publication_type",
    "abstract",
    "source_provenance_json",
    "enrichment_audit_json",
    "parse_warnings",
    "raw_fields_json",
    *ANNOTATION_FIELDS,
]


class JobUnavailable(NotFound):
    description = (
        "This upload job is no longer available in the current browser session. "
        "Please return to the upload page and try again."
    )


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    max_upload_mb = _env_number("DEDUPPER_MAX_UPLOAD_MB", 100)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("DEDUPPER_SECRET_KEY"),
        SECRET_KEY_FILE=(
            os.environ.get("DEDUPPER_SECRET_KEY_FILE")
            or str(Path(app.instance_path) / ".secret-key")
        ),
        MAX_CONTENT_LENGTH=int(max_upload_mb * 1024 * 1024),
        JOB_ROOT=(
            os.environ.get("DEDUPPER_JOB_ROOT")
            or str(Path(app.instance_path) / "jobs")
        ),
        # Uploaded bibliographic data is working data: delete jobs after this
        # many hours (0 disables automatic deletion).
        JOB_TTL_HOURS=_env_number("DEDUPPER_JOB_TTL_HOURS", 24),
        MATCH_SETTINGS=settings_from_env(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("DEDUPPER_SECURE_COOKIES", "").casefold()
        in {"1", "true", "yes"},
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = _load_or_create_secret_key(
            Path(app.config["SECRET_KEY_FILE"])
        )
    store = JobStore(app.config["JOB_ROOT"])
    app.extensions["job_store"] = store

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {"app_version": __version__}

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/jobs/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_: RequestEntityTooLarge) -> tuple[str, int]:
        return (
            render_template(
                "error.html",
                message=(
                    "The combined upload is larger than the "
                    f"{max_upload_mb:g} MB limit."
                ),
            ),
            413,
        )

    @app.errorhandler(JobUnavailable)
    def job_unavailable(error: JobUnavailable) -> tuple[str, int]:
        return render_template("error.html", message=error.description), 404

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            supported=", ".join(sorted(SUPPORTED_EXTENSIONS)),
            ttl_hours=app.config["JOB_TTL_HOURS"],
        )

    @app.get("/rules")
    def rules_page() -> str:
        return render_template(
            "rules.html",
            rules=RULES,
            settings=app.config["MATCH_SETTINGS"],
            version_labels=VERSION_LABELS,
        )

    @app.post("/upload")
    def upload() -> Response:
        files = [item for item in request.files.getlist("files") if item.filename]
        try:
            expected = int(request.form.get("file_count", "0"))
        except ValueError:
            expected = 0
        if not 1 <= expected <= 10 or len(files) != expected:
            flash("Select exactly the number of files shown in the form.", "error")
            return redirect(url_for("index"))

        invalid = [
            item.filename
            for item in files
            if Path(item.filename or "").suffix.lower() not in SUPPORTED_EXTENSIONS
        ]
        if invalid:
            flash(f"Unsupported file type: {', '.join(invalid)}", "error")
            return redirect(url_for("index"))

        store.purge_expired(app.config["JOB_TTL_HOURS"])
        job_id = uuid.uuid4().hex
        job_dir = store.create(job_id)
        manifest: dict[str, Any] = {
            "job_id": job_id,
            "files": [],
            "tables": [],
            "direct_records": [],
            "stage": "mapping",
            "created_at": time.time(),
            "app_version": __version__,
        }
        used_names: set[str] = set()
        try:
            for position, uploaded in enumerate(files, start=1):
                original_name = uploaded.filename or f"upload-{position}"
                safe_name = secure_filename(original_name) or f"upload-{position}"
                safe_name = _unique_filename(safe_name, used_names)
                used_names.add(safe_name)
                file_id = f"file{position}"
                destination = job_dir / "uploads" / safe_name
                uploaded.save(destination)
                result = parse_uploaded_file(destination, file_id)
                file_meta = {
                    "file_id": file_id,
                    "filename": original_name,
                    "stored_filename": safe_name,
                    "file_type": destination.suffix.lower().lstrip("."),
                    "kind": result["kind"],
                }
                if result["kind"] == "records":
                    file_meta["diagnostics"] = result["diagnostics"]
                    manifest["direct_records"].extend(result["records"])
                else:
                    for table_dict in result["tables"]:
                        table = SourceTable.from_dict(table_dict)
                        table.inferred_mapping = infer_mapping(table)
                        manifest["tables"].append(table.to_dict())
                manifest["files"].append(file_meta)
        except (ParseError, OSError, ValueError) as exc:
            store.clean(job_id)
            flash(f"Upload could not be parsed: {exc}", "error")
            return redirect(url_for("index"))

        store.save_json(job_id, "manifest", manifest)
        session["job_id"] = job_id
        if manifest["tables"]:
            return redirect(url_for("mapping_review", job_id=job_id))
        _finalize_master(store, job_id, manifest, {}, app.config["MATCH_SETTINGS"])
        return redirect(url_for("results", job_id=job_id))

    @app.get("/jobs/<job_id>/mapping")
    def mapping_review(job_id: str) -> str:
        manifest = _owned_manifest(store, job_id)
        tables = [SourceTable.from_dict(item) for item in manifest["tables"]]
        return render_template(
            "mapping.html",
            job_id=job_id,
            tables=tables,
            fields=CANONICAL_FIELDS,
            profiles={table.table_id: column_profiles(table) for table in tables},
            field_labels={
                "title": "Article title",
                "year": "Publication year",
                "first_author": "First author / authors",
                "authors": "Complete author list",
                "doi": "DOI",
                "journal": "Journal name",
                "pages": "Pages / page range",
                "article_number": "Article or e-location number",
                "issn": "ISSN",
                "isbn": "ISBN",
                "abstract": "Abstract",
                "publication_type": "Publication / document type",
            },
        )

    @app.post("/jobs/<job_id>/tables/<path:table_id>/header")
    def update_header(job_id: str, table_id: str) -> Response:
        manifest = _owned_manifest(store, job_id)
        try:
            header_index = int(request.form["header_index"])
        except (KeyError, ValueError):
            flash("Choose a valid header row.", "error")
            return redirect(url_for("mapping_review", job_id=job_id))
        found = False
        rebuilt_tables: list[dict[str, Any]] = []
        try:
            for table_dict in manifest["tables"]:
                table = SourceTable.from_dict(table_dict)
                if table.table_id == table_id:
                    table = rebuild_source_table(table, header_index)
                    table.inferred_mapping = infer_mapping(table)
                    found = True
                rebuilt_tables.append(table.to_dict())
        except ParseError as exc:
            flash(str(exc), "error")
            return redirect(url_for("mapping_review", job_id=job_id))
        if not found:
            abort(404)
        manifest["tables"] = rebuilt_tables
        store.save_json(job_id, "manifest", manifest)
        flash("Header row updated; column suggestions were recalculated.", "success")
        return redirect(url_for("mapping_review", job_id=job_id))

    @app.post("/jobs/<job_id>/process")
    def process_mapping(job_id: str) -> Response:
        manifest = _owned_manifest(store, job_id)
        mappings: dict[str, dict[str, str | None]] = {}
        for table_dict in manifest["tables"]:
            table = SourceTable.from_dict(table_dict)
            table_mapping: dict[str, str | None] = {}
            for field in CANONICAL_FIELDS:
                selected = request.form.get(_mapping_name(table.table_id, field), "")
                if selected and selected not in table.columns:
                    abort(400)
                table_mapping[field] = selected or None
            chosen_by_column: dict[str, list[str]] = defaultdict(list)
            for field, column in table_mapping.items():
                if column:
                    chosen_by_column[column].append(field)
            invalid_reuse = any(
                len(fields) > 1 and set(fields) != {"first_author", "authors"}
                for fields in chosen_by_column.values()
            )
            if invalid_reuse:
                flash(
                    f"Each mapped field in {table.filename} must use a different "
                    "source column, except that the complete-author column may "
                    "also supply the first author.",
                    "error",
                )
                return redirect(url_for("mapping_review", job_id=job_id))
            mappings[table.table_id] = table_mapping
        _finalize_master(store, job_id, manifest, mappings, app.config["MATCH_SETTINGS"])
        return redirect(url_for("results", job_id=job_id))

    @app.get("/jobs/<job_id>/results")
    def results(job_id: str) -> str:
        manifest = _owned_manifest(store, job_id)
        records = store.load_json(job_id, "master", [])
        reports = store.load_json(job_id, "reports", [])
        groups = store.load_json(job_id, "groups", [])
        review_candidates = store.load_json(job_id, "review_candidates", [])
        review_clusters = _review_clusters(store, job_id)
        conference_report = store.load_json(
            job_id, "conference_abstract_report", {}
        )
        deduplicated = store.load_json(job_id, "deduplicated")
        deduplication = store.load_json(job_id, "deduplication_report")
        related_links = store.load_json(job_id, "related_links", [])
        related_families = store.load_json(job_id, "related_families", [])
        separated_pairs = store.load_json(job_id, "separated_pairs", [])
        versions = store.load_json(job_id, "versions", {})
        analysis_meta = store.load_json(job_id, "analysis_meta", {})
        version_counts: dict[str, int] = defaultdict(int)
        for version in versions.values():
            version_counts[version.get("kind", "unspecified")] += 1
        selected_by_group: dict[str, list[str]] = {}
        if deduplication:
            for group_id, selected in deduplication.get("selections", {}).items():
                selected_by_group[group_id] = (
                    [selected] if isinstance(selected, str) else list(selected)
                )
        by_id = {record["record_id"]: record for record in records}
        return render_template(
            "results.html",
            job_id=job_id,
            manifest=manifest,
            records=records,
            reports=reports,
            groups=groups,
            review_candidates=review_candidates,
            review_clusters=review_clusters,
            candidates_by_id={item["candidate_id"]: item for item in review_candidates},
            conference_report=conference_report,
            by_id=by_id,
            deduplicated=deduplicated,
            deduplication=deduplication,
            selected_by_group=selected_by_group,
            total_duplicate_records=sum(len(group["record_ids"]) for group in groups),
            related_links=related_links,
            related_families=related_families,
            separated_pairs=separated_pairs,
            separated_pairs_total=analysis_meta.get(
                "separated_pairs_total", len(separated_pairs)
            ),
            versions=versions,
            version_counts=dict(version_counts),
            version_labels=VERSION_LABELS,
            rules=RULES,
            compared_fields=COMPARED_FIELDS,
            field_labels=FIELD_LABELS,
            review_choices=(deduplication or {}).get("review_cluster_choices", {}),
            family_choices=(deduplication or {}).get("family_choices", {}),
            ttl_hours=app.config["JOB_TTL_HOURS"],
        )

    @app.post("/jobs/<job_id>/deduplicate")
    def deduplicate(job_id: str) -> Response:
        _owned_manifest(store, job_id)
        records = store.load_json(job_id, "master", [])
        groups = store.load_json(job_id, "groups", [])
        mode = request.form.get("mode", "automatic")
        selections: dict[str, list[str]] = {}
        if mode == "manual":
            for group in groups:
                selections[group["group_id"]] = request.form.getlist(
                    f"keep.{group['group_id']}"
                )
        review_clusters = _review_clusters(store, job_id)
        families = store.load_json(job_id, "related_families", [])
        # A cluster's checkboxes are only meaningful when its card was in the
        # submitted form; otherwise every record is kept.
        review_choices = {
            cluster["cluster_id"]: request.form.getlist(f"reviewkeep.{cluster['cluster_id']}")
            for cluster in review_clusters
            if request.form.get(f"reviewcluster.{cluster['cluster_id']}")
        }
        family_choices = {
            family["family_id"]: request.form.getlist(f"exclude.{family['family_id']}")
            for family in families
        }
        try:
            kept, removed, final_selections = apply_group_selections(
                records, groups, selections
            )
            enriched, enrichment_audit = enrich_retained_records(
                kept, records, groups, final_selections
            )
            enriched, removed, applied_reviews = apply_cluster_decisions(
                enriched, removed, review_clusters, review_choices
            )
            enriched, removed, applied_families = apply_family_decisions(
                enriched, removed, families, family_choices
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("results", job_id=job_id))
        removed_ids = {record["record_id"] for record in removed}
        store.save_json(job_id, "deduplicated", enriched)
        store.save_json(
            job_id,
            "deduplication_report",
            {
                "mode": mode,
                "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "input_records": len(records),
                "output_records": len(enriched),
                "removed_records": len(removed),
                "selections": final_selections,
                "review_cluster_choices": applied_reviews,
                "family_choices": applied_families,
                "removals": removal_reasons(
                    groups,
                    final_selections,
                    review_clusters,
                    applied_reviews,
                    families,
                    applied_families,
                    removed_ids,
                ),
                "enrichment": enrichment_audit,
            },
        )
        flash(
            f"Deduplication complete: {len(removed)} record(s) removed and "
            f"{len(enriched)} retained.",
            "success",
        )
        return redirect(
            f"{url_for('results', job_id=job_id)}#deduplicated-output"
        )

    @app.get("/jobs/<job_id>/export/<dataset>")
    def export(job_id: str, dataset: str) -> Response:
        _owned_manifest(store, job_id)
        export_format = request.args.get("format", "csv").casefold()
        if export_format not in {"csv", "xlsx", "ris"}:
            abort(400)
        master = store.load_json(job_id, "master", [])
        annotations = build_annotations(
            master, _load_analysis(store, job_id), store.load_json(job_id, "deduplication_report")
        )
        if dataset == "master":
            records = master
            basename = "dedupper_master_with_duplicates"
        elif dataset == "deduplicated":
            records = store.load_json(job_id, "deduplicated")
            if records is None:
                abort(404)
            basename = "dedupper_deduplicated"
        elif dataset == "removed":
            kept_ids = {
                record["record_id"]
                for record in store.load_json(job_id, "deduplicated") or abort(404)
            }
            records = [record for record in master if record["record_id"] not in kept_ids]
            basename = "dedupper_removed_records"
        elif dataset == "decisions":
            rows = decision_log_rows(master, annotations)
            if export_format == "ris":
                abort(400)
            if export_format == "xlsx":
                return send_file(
                    _rows_to_xlsx(rows, DECISION_LOG_FIELDS, "Decisions"),
                    mimetype=XLSX_MIMETYPE,
                    as_attachment=True,
                    download_name="dedupper_decision_log.xlsx",
                )
            return Response(
                _rows_to_csv(rows, DECISION_LOG_FIELDS),
                mimetype="text/csv",
                headers={
                    "Content-Disposition": 'attachment; filename="dedupper_decision_log.csv"'
                },
            )
        else:
            abort(404)
        records = [
            {**record, **annotations.get(record["record_id"], {})} for record in records
        ]

        if export_format == "xlsx":
            return send_file(
                _records_to_xlsx(records),
                mimetype=XLSX_MIMETYPE,
                as_attachment=True,
                download_name=f"{basename}.xlsx",
            )
        if export_format == "ris":
            return Response(
                _records_to_ris(records).encode("utf-8"),
                mimetype="application/x-research-info-systems",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{basename}.ris"'
                    )
                },
            )
        return Response(
            _records_to_csv(records),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{basename}.csv"'
            },
        )

    @app.get("/jobs/<job_id>/audit")
    def download_audit(job_id: str) -> Response:
        _owned_manifest(store, job_id)
        payload = {
            "dedupper_version": __version__,
            "analysis": store.load_json(job_id, "analysis_meta", {}),
            "rules": RULES,
            "reports": store.load_json(job_id, "reports", []),
            "publication_versions": store.load_json(job_id, "versions", {}),
            "duplicate_groups": store.load_json(job_id, "groups", []),
            "review_candidates": store.load_json(job_id, "review_candidates", []),
            "review_clusters": _review_clusters(store, job_id),
            "related_links": store.load_json(job_id, "related_links", []),
            "related_families": store.load_json(job_id, "related_families", []),
            "separated_pairs": store.load_json(job_id, "separated_pairs", []),
            "conference_abstract_report": store.load_json(
                job_id, "conference_abstract_report", {}
            ),
            "deduplication": store.load_json(job_id, "deduplication_report"),
        }
        buffer = io.BytesIO(
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )
        return send_file(
            buffer,
            mimetype="application/json",
            as_attachment=True,
            download_name="dedupper_audit.json",
        )

    @app.post("/jobs/<job_id>/delete")
    def delete_job(job_id: str) -> Response:
        _owned_manifest(store, job_id)
        store.clean(job_id)
        session.pop("job_id", None)
        flash("Your uploaded files and results were deleted from the server.", "success")
        return redirect(url_for("index"))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.jinja_env.globals["mapping_name"] = _mapping_name
    return app


XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _env_number(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}.") from exc


def _load_analysis(store: JobStore, job_id: str) -> dict[str, Any]:
    return {
        "groups": store.load_json(job_id, "groups", []),
        "review_candidates": store.load_json(job_id, "review_candidates", []),
        "review_clusters": _review_clusters(store, job_id),
        "related_links": store.load_json(job_id, "related_links", []),
        "related_families": store.load_json(job_id, "related_families", []),
        "versions": store.load_json(job_id, "versions", {}),
    }


def _review_clusters(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    """Stored review clusters, rebuilt (and saved) for jobs from v0.8.1 or older."""

    clusters = store.load_json(job_id, "review_clusters")
    if clusters is None:
        clusters = build_review_clusters(
            store.load_json(job_id, "master", []),
            store.load_json(job_id, "review_candidates", []),
            store.load_json(job_id, "groups", []),
        )
        store.save_json(job_id, "review_clusters", clusters)
    return clusters


def _owned_manifest(store: JobStore, job_id: str) -> dict[str, Any]:
    if session.get("job_id") != job_id:
        raise JobUnavailable()
    manifest = store.load_json(job_id, "manifest")
    if manifest is None:
        raise JobUnavailable()
    return manifest


def _load_or_create_secret_key(path: Path) -> str:
    """Return one stable session key for every worker sharing this filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        # A second worker can observe the new path just before the first worker
        # has finished writing it. Briefly retry rather than accepting an empty
        # key and invalidating sessions between workers.
        for _ in range(50):
            value = path.read_text(encoding="ascii").strip()
            if value:
                return value
            time.sleep(0.01)
        raise RuntimeError(f"Session key file is empty: {path}")

    value = secrets.token_hex(32)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    return value


def _mapping_name(table_id: str, field: str) -> str:
    return f"mapping.{table_id}.{field}"


def _unique_filename(filename: str, used: set[str]) -> str:
    if filename not in used:
        return filename
    path = Path(filename)
    counter = 2
    while True:
        candidate = f"{path.stem}-{counter}{path.suffix}"
        if candidate not in used:
            return candidate
        counter += 1


def _finalize_master(
    store: JobStore,
    job_id: str,
    manifest: dict[str, Any],
    mappings: dict[str, dict[str, str | None]],
    settings: dict[str, float] | None = None,
) -> None:
    records = list(manifest["direct_records"])
    report_data: dict[str, dict[str, Any]] = {}
    for file_meta in manifest["files"]:
        file_id = file_meta["file_id"]
        report_data[file_id] = {
            "file_id": file_id,
            "filename": file_meta["filename"],
            "file_type": file_meta["file_type"],
            "logical_items": 0,
            "parsed_records": 0,
            "blank_rows": 0,
            "preamble_rows": 0,
            "tables": 0,
            "record_warnings": 0,
            "warnings": [],
        }
        if file_meta["kind"] == "records":
            diagnostics = file_meta["diagnostics"]
            report_data[file_id].update(
                {
                    "logical_items": diagnostics["logical_items"],
                    "parsed_records": diagnostics["parsed_records"],
                    "blank_rows": diagnostics.get("blank_rows", 0),
                    "preamble_rows": diagnostics.get("preamble_rows", 0),
                    "tables": 1,
                    "warnings": diagnostics.get("warnings", []),
                }
            )

    for table_dict in manifest["tables"]:
        table = SourceTable.from_dict(table_dict)
        table_records = table_to_records(table, mappings.get(table.table_id, {}))
        records.extend(record.to_dict() for record in table_records)
        report = report_data[table.file_id]
        report["logical_items"] += len(table.rows)
        report["parsed_records"] += len(table_records)
        report["blank_rows"] += table.blank_rows
        report["preamble_rows"] += table.preamble_rows
        report["tables"] += 1
        report["warnings"].extend(
            (
                f"{table.sheet_name}: {warning}"
                if table.sheet_name
                else warning
            )
            for warning in table.warnings
        )

    analysis = analyze_records(records, settings=settings)
    groups = analysis["groups"]
    review_candidates = analysis["review_candidates"]
    conference_report = conference_abstract_report(records, groups)
    warning_counts: dict[str, int] = defaultdict(int)
    for record in records:
        if record.get("parse_warnings"):
            warning_counts[record["source_file_id"]] += 1
    for file_id, report in report_data.items():
        report["record_warnings"] = warning_counts[file_id]
    manifest["stage"] = "results"
    manifest["mappings"] = mappings
    store.save_json(job_id, "manifest", manifest)
    store.save_json(job_id, "master", records)
    store.save_json(job_id, "reports", list(report_data.values()))
    store.save_json(job_id, "groups", groups)
    store.save_json(job_id, "review_candidates", review_candidates)
    store.save_json(job_id, "review_clusters", analysis["review_clusters"])
    store.save_json(job_id, "conference_abstract_report", conference_report)
    store.save_json(job_id, "related_links", analysis["related_links"])
    store.save_json(job_id, "related_families", analysis["related_families"])
    store.save_json(job_id, "separated_pairs", analysis["separated_pairs"])
    store.save_json(job_id, "versions", analysis["versions"])
    store.save_json(
        job_id,
        "analysis_meta",
        {
            "dedupper_version": __version__,
            "settings": analysis["settings"],
            "separated_pairs_total": analysis["separated_pairs_total"],
            "separated_pairs_stored": len(analysis["separated_pairs"]),
        },
    )


def _records_to_csv(records: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow(_record_to_export_row(record))
    return "\ufeff" + output.getvalue()


def _rows_to_csv(rows: list[dict[str, Any]], fields: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return "\ufeff" + output.getvalue()


def _rows_to_xlsx(rows: list[dict[str, Any]], fields: list[str], title: str) -> io.BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(fields))}{max(len(rows) + 1, 1)}"
    header_fill = PatternFill("solid", fgColor="001C3D")
    for column, field in enumerate(fields, start=1):
        cell = sheet.cell(row=1, column=column, value=field)
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = header_fill
        sheet.column_dimensions[get_column_letter(column)].width = (
            60 if field in {"title", "dedup_reason", "related_records"} else 22
        )
    for row_index, row in enumerate(rows, start=2):
        for column, field in enumerate(fields, start=1):
            value = row.get(field, "")
            cell = sheet.cell(row=row_index, column=column, value="" if value is None else str(value))
            cell.data_type = "s"
            cell.alignment = Alignment(vertical="top", wrap_text=field in {"title", "dedup_reason", "related_records"})
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def _record_to_export_row(record: dict[str, Any]) -> dict[str, Any]:
    row = {field: record.get(field, "") for field in EXPORT_FIELDS}
    row["source_provenance_json"] = json.dumps(
        record.get("source_provenance", []), ensure_ascii=False, separators=(",", ":")
    )
    row["enrichment_audit_json"] = json.dumps(
        record.get("enrichment_audit", []), ensure_ascii=False, separators=(",", ":")
    )
    row["parse_warnings"] = " | ".join(record.get("parse_warnings", []))
    row["raw_fields_json"] = json.dumps(
        record.get("raw_fields", {}), ensure_ascii=False, separators=(",", ":")
    )
    return row


def _records_to_xlsx(records: list[dict[str, Any]]) -> io.BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Articles"
    sheet.freeze_panes = "A2"
    final_column = get_column_letter(len(EXPORT_FIELDS))
    sheet.auto_filter.ref = f"A1:{final_column}{max(len(records) + 1, 1)}"

    header_fill = PatternFill("solid", fgColor="001C3D")
    for column, field in enumerate(EXPORT_FIELDS, start=1):
        cell = sheet.cell(row=1, column=column, value=field)
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="top")

    for row_index, record in enumerate(records, start=2):
        values = _record_to_export_row(record)
        for column, field in enumerate(EXPORT_FIELDS, start=1):
            value = values.get(field, "")
            cell = sheet.cell(
                row=row_index,
                column=column,
                value="" if value is None else str(value),
            )
            # Bibliographic content must remain text even when it begins with
            # characters Excel otherwise interprets as a formula.
            cell.data_type = "s"
            cell.alignment = Alignment(
                vertical="top",
                wrap_text=field in {
                    "title",
                    "authors",
                    "abstract",
                    "source_provenance_json",
                    "enrichment_audit_json",
                    "raw_fields_json",
                    "dedup_reason",
                    "related_records",
                },
            )

    field_widths = {
        "record_id": 34,
        "source_filename": 28,
        "source_sheet": 18,
        "source_row": 12,
        "title": 55,
        "year": 11,
        "first_author": 22,
        "authors": 50,
        "doi": 34,
        "journal": 30,
        "pages": 16,
        "article_number": 20,
        "issn": 16,
        "isbn": 18,
        "publication_type": 28,
        "abstract": 85,
        "source_provenance_json": 70,
        "enrichment_audit_json": 70,
        "parse_warnings": 34,
        "raw_fields_json": 85,
        "publication_version": 24,
        "publication_version_evidence": 40,
        "duplicate_group": 12,
        "dedup_status": 16,
        "dedup_reason": 60,
        "related_records": 60,
        "review_items": 30,
    }
    for index, field in enumerate(EXPORT_FIELDS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = field_widths.get(
            field, 24
        )

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def _records_to_ris(records: list[dict[str, Any]]) -> str:
    output: list[str] = []
    for record in records:
        raw_fields = record.get("raw_fields", {})
        record_type = _first_raw_value(raw_fields, ("TY",)) or "JOUR"
        output.extend(_ris_field("TY", record_type))
        output.extend(_ris_field("TI", record.get("title", "")))
        for author in _ris_authors(record):
            output.extend(_ris_field("AU", author))
        output.extend(_ris_field("PY", record.get("year", "")))
        output.extend(_ris_field("JO", record.get("journal", "")))
        start_page, end_page = _split_page_range(record.get("pages", ""))
        output.extend(_ris_field("SP", start_page))
        output.extend(_ris_field("EP", end_page))
        output.extend(_ris_field("C7", record.get("article_number", "")))
        output.extend(_ris_field("SN", record.get("issn", "")))
        output.extend(_ris_field("SN", record.get("isbn", "")))
        output.extend(_ris_field("DO", record.get("doi", "")))
        output.extend(_ris_field("PT", record.get("publication_type", "")))
        output.extend(_ris_field("AB", record.get("abstract", "")))
        output.extend(_ris_field("N1", _provenance_note(record)))
        output.extend(_ris_field("N1", _dedupper_note(record)))
        output.append("ER  -")
        output.append("")
    return "\r\n".join(output)


def _first_raw_value(raw_fields: Any, tags: tuple[str, ...]) -> str:
    if not isinstance(raw_fields, dict):
        return ""
    for tag in tags:
        value = raw_fields.get(tag)
        if isinstance(value, list):
            for item in value:
                if str(item).strip():
                    return str(item).strip()
        elif value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _ris_authors(record: dict[str, Any]) -> list[str]:
    canonical = str(record.get("authors", "")).strip()
    if canonical:
        authors = [
            item.strip()
            for item in re.split(r"\s*;\s*|\s*\|\s*|\s+and\s+", canonical)
            if item.strip()
        ]
        if len(authors) > 1:
            return authors
    raw_fields = record.get("raw_fields", {})
    if isinstance(raw_fields, dict):
        for tag in ("AU", "A1"):
            value = raw_fields.get(tag)
            if isinstance(value, list):
                authors = [str(item).strip() for item in value if str(item).strip()]
                if authors:
                    return authors
        pubmed_authors = raw_fields.get("authors")
        if isinstance(pubmed_authors, str) and pubmed_authors.strip():
            authors = [
                re.sub(r"\(\d[\d,\- ]*\)", "", item).strip(" .")
                for item in pubmed_authors.split(",")
            ]
            return [author for author in authors if author]
    first_author = str(record.get("first_author", "")).strip()
    return [first_author] if first_author else []


def _split_page_range(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    parts = re.split(r"\s*[-–—]\s*", text, maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (text, "")


def _provenance_note(record: dict[str, Any]) -> str:
    provenance = record.get("source_provenance", [])
    if not isinstance(provenance, list) or not provenance:
        return ""
    sources = []
    for item in provenance:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_filename", ""))
        if item.get("source_sheet"):
            source += f"/{item['source_sheet']}"
        if item.get("source_row") not in (None, ""):
            source += f" row {item['source_row']}"
        if source:
            sources.append(source)
    return "Dedupper merged sources: " + "; ".join(sources) if sources else ""


def _dedupper_note(record: dict[str, Any]) -> str:
    parts = []
    if record.get("publication_version") and record.get("publication_version_evidence"):
        parts.append(
            f"version: {record['publication_version']} "
            f"({record['publication_version_evidence']})"
        )
    if record.get("dedup_status"):
        parts.append(f"status: {record['dedup_status']} – {record.get('dedup_reason', '')}")
    if record.get("related_records"):
        parts.append(f"related: {record['related_records']}")
    return "Dedupper " + "; ".join(parts) if parts else ""


def _ris_field(tag: str, value: Any) -> list[str]:
    text = "" if value is None else str(value).strip()
    if not text:
        return []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result = [f"{tag}  - {lines[0]}"]
    result.extend(f"      {line}" for line in lines[1:])
    return result
