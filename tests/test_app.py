from __future__ import annotations

import io
from pathlib import Path

from openpyxl import load_workbook

from dedupper import create_app
from dedupper.parsers import parse_ris


def test_csv_upload_mapping_audit_and_export(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "JOB_ROOT": str(tmp_path / "jobs"),
        }
    )
    client = app.test_client()
    payload = (
        b"Title,Year,Authors,DOI,Journal,Abstract,Unmapped\n"
        b"One useful scientific title,2024,\"Smith, J\",10.1234/a,Journal A,"
        b"First abstract,x\n"
        b"Another useful scientific title,2023,Jones P,,Journal B,"
        b"Second abstract,y\n"
    )

    response = client.post(
        "/upload",
        data={"file_count": "1", "files": (io.BytesIO(payload), "source.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Confirm the meaning of each column" in response.data

    with client.session_transaction() as flask_session:
        job_id = flask_session["job_id"]
    response = client.post(
        f"/jobs/{job_id}/process",
        data={
            "mapping.file1:table.title": "Title",
            "mapping.file1:table.year": "Year",
            "mapping.file1:table.first_author": "Authors",
            "mapping.file1:table.doi": "DOI",
            "mapping.file1:table.journal": "Journal",
            "mapping.file1:table.abstract": "Abstract",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"2 records entered the master dataset" in response.data
    assert b"Complete" in response.data

    export = client.get(f"/jobs/{job_id}/export/master")
    assert export.status_code == 200
    assert b"raw_fields_json" in export.data
    assert b"Unmapped" in export.data
    assert b"First abstract" in export.data


def test_two_file_mapping_survives_a_different_app_worker(
    tmp_path: Path,
) -> None:
    shared_config = {
        "TESTING": True,
        "SECRET_KEY": None,
        "SECRET_KEY_FILE": str(tmp_path / "session-secret"),
        "JOB_ROOT": str(tmp_path / "jobs"),
    }
    first_worker = create_app(shared_config)
    second_worker = create_app(shared_config)
    first_client = first_worker.test_client()
    second_client = second_worker.test_client()

    csv_payload = (
        b"Title,Year,Authors,DOI,Journal\n"
        b"First scientific article,2024,Smith,10.1234/first,Journal A\n"
    )
    ris_payload = (
        b"TY  - JOUR\nTI  - Second scientific article\n"
        b"PY  - 2023\nDO  - 10.1234/second\nER  -\n"
    )
    response = first_client.post(
        "/upload",
        data={
            "file_count": "2",
            "files": [
                (io.BytesIO(csv_payload), "source.csv"),
                (io.BytesIO(ris_payload), "source.ris"),
            ],
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/mapping")

    session_cookie = first_client.get_cookie("session")
    assert session_cookie is not None
    second_client.set_cookie("session", session_cookie.value)

    with first_client.session_transaction() as flask_session:
        job_id = flask_session["job_id"]
    response = second_client.post(
        f"/jobs/{job_id}/process",
        data={
            "mapping.file1:table.title": "Title",
            "mapping.file1:table.year": "Year",
            "mapping.file1:table.first_author": "Authors",
            "mapping.file1:table.doi": "DOI",
            "mapping.file1:table.journal": "Journal",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"2 records entered the master dataset" in response.data


def test_missing_job_session_has_a_useful_error(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "JOB_ROOT": str(tmp_path / "jobs"),
        }
    )
    response = app.test_client().post("/jobs/notmyjob/process")

    assert response.status_code == 404
    assert b"no longer available in the current browser session" in response.data


def test_ris_only_upload_skips_mapping_step(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "JOB_ROOT": str(tmp_path / "jobs"),
        }
    )
    client = app.test_client()
    payload = b"TY  - JOUR\nTI  - A title\nPY  - 2024\nER  -\n"

    response = client.post(
        "/upload",
        data={"file_count": "1", "files": (io.BytesIO(payload), "source.ris")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"1 records entered the master dataset" in response.data
    assert b"Source-by-source record counts" in response.data


def test_conference_abstract_safeguard_stats_are_shown_and_audited(
    tmp_path: Path,
) -> None:
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "JOB_ROOT": str(tmp_path / "jobs"),
        }
    )
    client = app.test_client()
    doi = b"10.1038/s41433-025-03831-0"
    payload = b"".join(
        (
            b"TY  - JOUR\n"
            + f"TI  - Distinct conference finding number {index}\n".encode()
            + f"AU  - Author{index}, A\n".encode()
            + b"PT  - Conference Abstract\nDO  - "
            + doi
            + b"\nER  -\n"
        )
        for index in range(1, 4)
    )

    response = client.post(
        "/upload",
        data={"file_count": "1", "files": (io.BytesIO(payload), "abstracts.ris")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Conference abstract safeguard" in response.data
    assert b"3 conference abstract record(s) were detected" in response.data
    assert b"1 same-source shared-DOI" in response.data
    assert b"No corroborated duplicate groups were found" in response.data

    with client.session_transaction() as flask_session:
        job_id = flask_session["job_id"]
    report = app.extensions["job_store"].load_json(
        job_id, "conference_abstract_report"
    )
    assert report["protected_records"] == 3
    assert report["overlapping_dois"] == 1

    audit = client.get(f"/jobs/{job_id}/audit")
    assert audit.status_code == 200
    assert b'"conference_abstract_report"' in audit.data

    ris_export = client.get(f"/jobs/{job_id}/export/master?format=ris")
    assert ris_export.data.count(b"PT  - Conference Abstract") == 3


def test_pubmed_txt_upload_and_csv_xlsx_ris_exports_keep_abstract(
    tmp_path: Path,
) -> None:
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "JOB_ROOT": str(tmp_path / "jobs"),
        }
    )
    client = app.test_client()
    payload = b"""1. Journal of Tests. 2024;12(3):1-5. doi: 10.1234/example.

An exported PubMed article.

Smith AB(1), Jones CD.

Author information:
(1)Department of Testing, Example University.

This complete abstract must appear in every output format.

Publication Types: Journal Article

DOI: 10.1234/example
PMID: 12345678 [Indexed for MEDLINE]
"""
    response = client.post(
        "/upload",
        data={"file_count": "1", "files": (io.BytesIO(payload), "pubmed.txt")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"1 records entered the master dataset" in response.data
    assert b"Excel (.xlsx)" in response.data
    assert b"RIS" in response.data

    with client.session_transaction() as flask_session:
        job_id = flask_session["job_id"]

    csv_export = client.get(f"/jobs/{job_id}/export/master?format=csv")
    assert csv_export.status_code == 200
    assert b"This complete abstract must appear" in csv_export.data
    assert b"Journal Article" in csv_export.data
    assert csv_export.headers["Content-Disposition"].endswith(
        '"dedupper_master_with_duplicates.csv"'
    )

    xlsx_export = client.get(f"/jobs/{job_id}/export/master?format=xlsx")
    assert xlsx_export.status_code == 200
    workbook = load_workbook(io.BytesIO(xlsx_export.data), read_only=True)
    rows = list(workbook.active.iter_rows(values_only=True))
    abstract_column = rows[0].index("abstract")
    publication_type_column = rows[0].index("publication_type")
    assert rows[1][abstract_column] == (
        "This complete abstract must appear in every output format."
    )
    assert rows[1][publication_type_column] == "Journal Article"

    ris_export = client.get(f"/jobs/{job_id}/export/master?format=ris")
    assert ris_export.status_code == 200
    assert b"AB  - This complete abstract must appear" in ris_export.data
    assert b"PT  - Journal Article" in ris_export.data
    roundtrip, diagnostics = parse_ris(ris_export.data, "file2", "roundtrip.ris")
    assert diagnostics["parsed_records"] == 1
    assert roundtrip[0].abstract == (
        "This complete abstract must appear in every output format."
    )
    assert roundtrip[0].publication_type == "Journal Article"


def test_doi_metadata_conflict_is_visible_and_excluded_from_automatic_groups(
    tmp_path: Path,
) -> None:
    app = create_app(
        {"TESTING": True, "SECRET_KEY": "test", "JOB_ROOT": str(tmp_path / "jobs")}
    )
    client = app.test_client()
    payload = b"""TY  - JOUR
TI  - Effects of treatment on childhood language
AU  - Smith, John
PY  - 2024
DO  - 10.1000/reused
ER  -
TY  - JOUR
TI  - Quantum signatures in distant stellar systems
AU  - Jones, Anna
PY  - 2024
DO  - 10.1000/reused
ER  -
"""

    response = client.post(
        "/upload",
        data={"file_count": "1", "files": (io.BytesIO(payload), "conflict.ris")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"DOI metadata conflict" in response.data
    assert b"Kept separate" in response.data
    assert b"No corroborated duplicate groups were found" in response.data
    with client.session_transaction() as flask_session:
        job_id = flask_session["job_id"]
    audit = client.get(f"/jobs/{job_id}/audit")
    assert b'"review_candidates"' in audit.data
    assert b'"DOI metadata conflict"' in audit.data


def test_automatic_output_is_enriched_and_provenance_is_exported(tmp_path: Path) -> None:
    app = create_app(
        {"TESTING": True, "SECRET_KEY": "test", "JOB_ROOT": str(tmp_path / "jobs")}
    )
    client = app.test_client()
    payload = b"""TY  - JOUR
TI  - A complete enrichment example
AU  - Smith, John
PY  - 2024
ER  -
TY  - JOUR
TI  - A complete enrichment example.
AU  - Smith, John
AU  - Jones, Anna
PY  - 2024
SP  - 23
EP  - 29
SN  - 1234-5678
AB  - This abstract comes from the duplicate record.
ER  -
"""
    response = client.post(
        "/upload",
        data={"file_count": "1", "files": (io.BytesIO(payload), "enrich.ris")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    with client.session_transaction() as flask_session:
        job_id = flask_session["job_id"]

    response = client.post(
        f"/jobs/{job_id}/deduplicate",
        data={"mode": "automatic"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"field-level audit" in response.data

    csv_export = client.get(f"/jobs/{job_id}/export/deduplicated?format=csv")
    assert b"This abstract comes from the duplicate record" in csv_export.data
    assert b"source_provenance_json" in csv_export.data
    assert b"enrichment_audit_json" in csv_export.data

    ris_export = client.get(f"/jobs/{job_id}/export/deduplicated?format=ris")
    assert b"SP  - 23" in ris_export.data
    assert b"EP  - 29" in ris_export.data
    assert b"SN  - 1234-5678" in ris_export.data
    assert b"N1  - Dedupper merged sources:" in ris_export.data


def test_manual_dedup_keeps_multiple_records_and_collapses_review(
    tmp_path: Path,
) -> None:
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "JOB_ROOT": str(tmp_path / "jobs"),
        }
    )
    client = app.test_client()
    payload = (
        b"TY  - JOUR\nTI  - Shared scientific title\nAU  - Smith\nER  -\n"
        b"TY  - JOUR\nTI  - Shared scientific title\nAU  - Smith\n"
        b"DO  - 10.1234/shared\nER  -\n"
        b"TY  - JOUR\nTI  - Shared scientific title\nAU  - Smith\nER  -\n"
    )
    response = client.post(
        "/upload",
        data={"file_count": "1", "files": (io.BytesIO(payload), "source.ris")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    with client.session_transaction() as flask_session:
        job_id = flask_session["job_id"]
    store = app.extensions["job_store"]
    group = store.load_json(job_id, "groups")[0]
    selected = group["record_ids"][:2]

    response = client.post(
        f"/jobs/{job_id}/deduplicate",
        data={
            "mode": "manual",
            f"keep.{group['group_id']}": selected,
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("#deduplicated-output")
    assert len(store.load_json(job_id, "deduplicated")) == 2
    report = store.load_json(job_id, "deduplication_report")
    assert report["selections"][group["group_id"]] == selected

    results = client.get(f"/jobs/{job_id}/results")
    assert results.status_code == 200
    assert b"2 records retained" in results.data
    assert b'<details class="review-toggle">' in results.data
    assert b'<details class="review-toggle" open>' not in results.data
    for record_id in selected:
        assert f'value="{record_id}"'.encode() in results.data
    assert results.data.count(b"checked") == 2


def test_protocol_label_is_rendered_and_non_protocol_is_recommended(
    tmp_path: Path,
) -> None:
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "JOB_ROOT": str(tmp_path / "jobs"),
        }
    )
    client = app.test_client()
    payload = (
        b"TY  - JOUR\n"
        b"TI  - Online Mindfulness-Based Cognitive Behavioral Therapy "
        b"Intervention for Youth With Major Depressive Disorders: "
        b"Randomized Controlled Trial\n"
        b"PY  - 2021\nER  -\n"
        b"TY  - JOUR\n"
        b"TI  - An Online Mindfulness-Based Cognitive Behavioral Therapy "
        b"Intervention for Youth Diagnosed With Major Depressive Disorders: "
        b"Protocol for a Randomized Controlled Trial\n"
        b"PY  - 2021\nDO  - 10.2196/12041\nAB  - More complete protocol metadata\n"
        b"ER  -\n"
    )

    response = client.post(
        "/upload",
        data={"file_count": "1", "files": (io.BytesIO(payload), "source.ris")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert response.data.count(b"Potential protocol") == 1
    assert response.data.count(b"Recommended") == 1

    with client.session_transaction() as flask_session:
        job_id = flask_session["job_id"]
    store = app.extensions["job_store"]
    group = store.load_json(job_id, "groups")[0]
    protocol_id = group["potential_protocol_record_ids"][0]
    assert group["recommended_record_id"] != protocol_id
