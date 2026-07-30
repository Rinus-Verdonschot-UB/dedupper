from __future__ import annotations

import csv
import io
import json
import os
import re
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from openpyxl import Workbook
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
from werkzeug.utils import secure_filename

from .dedup import apply_group_selections, find_duplicate_groups
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


EXPORT_FIELDS = [
    "record_id",
    "source_filename",
    "source_sheet",
    "source_row",
    "title",
    "year",
    "first_author",
    "doi",
    "journal",
    "abstract",
    "parse_warnings",
    "raw_fields_json",
]


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("DEDUPPER_SECRET_KEY") or os.urandom(32),
        MAX_CONTENT_LENGTH=100 * 1024 * 1024,
        JOB_ROOT=os.environ.get("DEDUPPER_JOB_ROOT")
        or str(Path(app.instance_path) / "jobs"),
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    store = JobStore(app.config["JOB_ROOT"])
    app.extensions["job_store"] = store

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_: RequestEntityTooLarge) -> tuple[str, int]:
        return (
            render_template(
                "error.html",
                message="The combined upload is larger than the 100 MB limit.",
            ),
            413,
        )

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            supported=", ".join(sorted(SUPPORTED_EXTENSIONS)),
        )

    @app.get("/healthz")
    def healthz() -> tuple[dict[str, str], int]:
        return {"status": "ok"}, 200

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

        job_id = uuid.uuid4().hex
        job_dir = store.create(job_id)
        manifest: dict[str, Any] = {
            "job_id": job_id,
            "files": [],
            "tables": [],
            "direct_records": [],
            "stage": "mapping",
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
        _finalize_master(store, job_id, manifest, {})
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
                "doi": "DOI",
                "journal": "Journal name",
                "abstract": "Abstract",
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
            chosen = [value for value in table_mapping.values() if value]
            if len(chosen) != len(set(chosen)):
                flash(
                    f"Each mapped field in {table.filename} must use a different "
                    "source column.",
                    "error",
                )
                return redirect(url_for("mapping_review", job_id=job_id))
            mappings[table.table_id] = table_mapping
        _finalize_master(store, job_id, manifest, mappings)
        return redirect(url_for("results", job_id=job_id))

    @app.get("/jobs/<job_id>/results")
    def results(job_id: str) -> str:
        manifest = _owned_manifest(store, job_id)
        records = store.load_json(job_id, "master", [])
        reports = store.load_json(job_id, "reports", [])
        groups = store.load_json(job_id, "groups", [])
        deduplicated = store.load_json(job_id, "deduplicated")
        deduplication = store.load_json(job_id, "deduplication_report")
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
            by_id=by_id,
            deduplicated=deduplicated,
            deduplication=deduplication,
            selected_by_group=selected_by_group,
            total_duplicate_records=sum(len(group["record_ids"]) for group in groups),
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
        try:
            kept, removed, final_selections = apply_group_selections(
                records, groups, selections
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("results", job_id=job_id))
        store.save_json(job_id, "deduplicated", kept)
        store.save_json(
            job_id,
            "deduplication_report",
            {
                "input_records": len(records),
                "output_records": len(kept),
                "removed_records": len(removed),
                "selections": final_selections,
            },
        )
        flash(
            f"Deduplication complete: {len(removed)} record(s) removed and "
            f"{len(kept)} retained.",
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
        if dataset == "master":
            records = store.load_json(job_id, "master", [])
            basename = "dedupper_master_with_duplicates"
        elif dataset == "deduplicated":
            records = store.load_json(job_id, "deduplicated")
            if records is None:
                abort(404)
            basename = "dedupper_deduplicated"
        else:
            abort(404)

        if export_format == "xlsx":
            return send_file(
                _records_to_xlsx(records),
                mimetype=(
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
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
            "reports": store.load_json(job_id, "reports", []),
            "duplicate_groups": store.load_json(job_id, "groups", []),
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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.jinja_env.globals["mapping_name"] = _mapping_name
    return app


def _owned_manifest(store: JobStore, job_id: str) -> dict[str, Any]:
    if session.get("job_id") != job_id:
        abort(404)
    manifest = store.load_json(job_id, "manifest")
    if manifest is None:
        abort(404)
    return manifest


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

    groups = find_duplicate_groups(records)
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


def _records_to_csv(records: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow(_record_to_export_row(record))
    return "\ufeff" + output.getvalue()


def _record_to_export_row(record: dict[str, Any]) -> dict[str, Any]:
    row = {field: record.get(field, "") for field in EXPORT_FIELDS}
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
    sheet.auto_filter.ref = f"A1:L{max(len(records) + 1, 1)}"

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
                wrap_text=field in {"title", "abstract", "raw_fields_json"},
            )

    widths = {
        "A": 34,
        "B": 28,
        "C": 18,
        "D": 12,
        "E": 55,
        "F": 11,
        "G": 22,
        "H": 30,
        "I": 34,
        "J": 85,
        "K": 34,
        "L": 85,
    }
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width

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
        output.extend(_ris_field("DO", record.get("doi", "")))
        output.extend(_ris_field("AB", record.get("abstract", "")))
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


def _ris_field(tag: str, value: Any) -> list[str]:
    text = "" if value is None else str(value).strip()
    if not text:
        return []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result = [f"{tag}  - {lines[0]}"]
    result.extend(f"      {line}" for line in lines[1:])
    return result
