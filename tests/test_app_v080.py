"""v0.8.0 end-to-end tests: transparency, decisions, MEDLINE import, operations."""

from __future__ import annotations

import csv
import io
import json
import os
import time
from pathlib import Path

from dedupper import __version__, create_app
from dedupper.storage import JobStore

SAMPLES = Path(__file__).resolve().parents[1] / "sample_data"

RIS_PREPRINT_AND_ARTICLE = b"""TY  - JOUR
TI  - Remote ischaemic conditioning after acute myocardial infarction
AU  - Jansen, Anne
PY  - 2024
JO  - Example Journal of Cardiology
DO  - 10.1000/example.2024.001
PT  - Journal Article
ER  -

TY  - JOUR
TI  - Remote ischaemic conditioning after acute myocardial infarction
AU  - Jansen, Anne
PY  - 2023
JO  - medRxiv
DO  - 10.1101/2023.11.01.23297811
ER  -

TY  - JOUR
TI  - A completely different study about sleep in nurses
AU  - Peeters, B
PY  - 2022
JO  - Sleep Journal
DO  - 10.1000/sleep.1
ER  -
"""


def make_app(tmp_path: Path, **extra):
    config = {"TESTING": True, "SECRET_KEY": "test", "JOB_ROOT": str(tmp_path / "jobs")}
    config.update(extra)
    return create_app(config)


def upload(client, payload: bytes, name: str):
    return client.post(
        "/upload",
        data={"file_count": "1", "files": (io.BytesIO(payload), name)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def job_id_of(client) -> str:
    with client.session_transaction() as flask_session:
        return flask_session["job_id"]


def read_csv(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def test_related_publication_is_shown_with_evidence_and_kept_by_default(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    response = upload(client, RIS_PREPRINT_AND_ARTICLE, "mixed.ris")

    assert response.status_code == 200
    body = response.data.decode()
    assert "Preprint and journal version" in body
    assert "DOI prefix belongs to bioRxiv / medRxiv" in body
    assert "V1 · Related publication" in body
    assert "Exclude this version from the deduplicated output" in body

    job_id = job_id_of(client)
    client.post(f"/jobs/{job_id}/deduplicate", data={"mode": "automatic"})
    store = app.extensions["job_store"]
    assert len(store.load_json(job_id, "deduplicated")) == 3

    log = read_csv(client.get(f"/jobs/{job_id}/export/decisions?format=csv").data)
    assert len(log) == 3
    preprint = next(row for row in log if row["doi"].startswith("10.1101"))
    assert preprint["publication_version"] == "Preprint"
    assert preprint["dedup_status"] == "kept"
    assert "Family #1" in preprint["related_records"]
    assert "journal article" in preprint["related_records"]


def test_user_can_exclude_one_version_of_a_related_family(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    upload(client, RIS_PREPRINT_AND_ARTICLE, "mixed.ris")
    job_id = job_id_of(client)
    store = app.extensions["job_store"]
    [family] = store.load_json(job_id, "related_families")
    preprint_unit = next(unit for unit in family["units"] if unit["kind"] == "preprint")
    [preprint_id] = preprint_unit["record_ids"]

    response = client.post(
        f"/jobs/{job_id}/deduplicate",
        data={"mode": "manual", f"exclude.{family['family_id']}": preprint_unit["unit_id"]},
        follow_redirects=True,
    )

    assert response.status_code == 200
    kept = store.load_json(job_id, "deduplicated")
    assert len(kept) == 2
    report = store.load_json(job_id, "deduplication_report")
    assert report["family_choices"] == {family["family_id"]: [preprint_unit["unit_id"]]}
    reason = report["removals"][preprint_id]
    assert reason["via"] == "related publication family"
    assert reason["version"] == "Preprint"

    removed = read_csv(client.get(f"/jobs/{job_id}/export/removed?format=csv").data)
    assert [row["record_id"] for row in removed] == [preprint_id]
    assert removed[0]["dedup_status"] == "removed"
    assert "Removed via related publication family #1" in removed[0]["dedup_reason"]

    # The page remembers the choice.
    assert f'value="{preprint_unit["unit_id"]}" checked'.encode() in response.data

    # Excluding every version is refused.
    all_units = [unit["unit_id"] for unit in family["units"]]
    response = client.post(
        f"/jobs/{job_id}/deduplicate",
        data={"mode": "manual", f"exclude.{family['family_id']}": all_units},
        follow_redirects=True,
    )
    assert b"keep at least one version" in response.data


def test_review_item_can_be_resolved_by_the_user(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    payload = b"""TY  - JOUR
TI  - A short title
PY  - 2024
ER  -
TY  - JOUR
TI  - A short title
PY  - 2019
ER  -
"""
    upload(client, payload, "short.ris")
    job_id = job_id_of(client)
    store = app.extensions["job_store"]
    reviews = store.load_json(job_id, "review_candidates")
    assert reviews == [] or all(item["rule"] for item in reviews)

    payload = b"""TY  - JOUR
TI  - Identical cleaned title of a reasonable length here
ER  -
TY  - JOUR
TI  - Identical cleaned title of a reasonable length here
ER  -
"""
    upload(client, payload, "review.ris")
    job_id = job_id_of(client)
    [review] = store.load_json(job_id, "review_candidates")
    [cluster] = store.load_json(job_id, "review_clusters")
    assert cluster["candidate_ids"] == [review["candidate_id"]]
    assert review["rule"] == "R2"
    keep = review["record_ids"][1]
    client.post(
        f"/jobs/{job_id}/deduplicate",
        data={
            "mode": "automatic",
            f"reviewcluster.{cluster['cluster_id']}": "1",
            f"reviewkeep.{cluster['cluster_id']}": keep,
        },
    )
    kept = store.load_json(job_id, "deduplicated")
    assert [record["record_id"] for record in kept] == [keep]


def test_exports_carry_version_and_decision_columns(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    upload(client, RIS_PREPRINT_AND_ARTICLE, "mixed.ris")
    job_id = job_id_of(client)

    master = read_csv(client.get(f"/jobs/{job_id}/export/master?format=csv").data)
    assert {"publication_version", "dedup_status", "dedup_reason", "related_records"} <= set(master[0])
    assert {row["dedup_status"] for row in master} == {"unique"}

    ris = client.get(f"/jobs/{job_id}/export/master?format=ris").data.decode()
    assert "N1  - Dedupper version: Preprint" in ris

    xlsx = client.get(f"/jobs/{job_id}/export/decisions?format=xlsx")
    assert xlsx.status_code == 200
    assert client.get(f"/jobs/{job_id}/export/decisions?format=ris").status_code == 400


def test_audit_json_contains_rules_settings_and_related_links(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    upload(client, RIS_PREPRINT_AND_ARTICLE, "mixed.ris")
    job_id = job_id_of(client)

    audit = json.loads(client.get(f"/jobs/{job_id}/audit").data)

    assert audit["dedupper_version"] == __version__
    assert "V1" in audit["rules"]
    assert audit["analysis"]["settings"]["fuzzy_title_threshold"] == 92.0
    assert len(audit["related_links"]) == 1
    assert len(audit["related_families"]) == 1
    assert len(audit["publication_versions"]) == 3


def test_medline_nbib_upload(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    response = upload(client, (SAMPLES / "pubmed_medline.nbib").read_bytes(), "pubmed.nbib")

    assert response.status_code == 200
    assert b"2 records entered the master dataset" in response.data
    assert b"Preprint and journal version" in response.data


def test_medline_saved_as_txt_is_detected(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    response = upload(client, (SAMPLES / "pubmed_medline.nbib").read_bytes(), "pubmed.txt")

    assert b"2 records entered the master dataset" in response.data


def test_rules_page_lists_every_rule(tmp_path: Path) -> None:
    client = make_app(tmp_path).test_client()

    body = client.get("/rules").data.decode()

    for rule_id in ("D1", "D4", "R1", "R4", "V1", "V2", "S1", "S6"):
        assert f'id="rule-{rule_id}"' in body
    assert "fuzzy_title_threshold" in body


def test_user_can_delete_their_job(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    upload(client, RIS_PREPRINT_AND_ARTICLE, "mixed.ris")
    job_id = job_id_of(client)
    store = app.extensions["job_store"]

    response = client.post(f"/jobs/{job_id}/delete", follow_redirects=True)

    assert b"were deleted from the server" in response.data
    assert not store.job_dir(job_id).exists()


def test_expired_jobs_are_purged(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    store.create("oldjob")
    store.save_json("oldjob", "manifest", {})
    store.create("newjob")
    store.save_json("newjob", "manifest", {})
    old = time.time() - 48 * 3600
    for path in [store.job_dir("oldjob"), *store.job_dir("oldjob").glob("*.json")]:
        os.utime(path, (old, old))

    assert store.purge_expired(24) == 1
    assert not store.job_dir("oldjob").exists()
    assert store.job_dir("newjob").exists()
    assert store.purge_expired(0) == 0


def test_job_root_and_limits_come_from_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEDUPPER_JOB_ROOT", str(tmp_path / "volume"))
    monkeypatch.setenv("DEDUPPER_MAX_UPLOAD_MB", "5")
    monkeypatch.setenv("DEDUPPER_JOB_TTL_HOURS", "2")
    monkeypatch.setenv("DEDUPPER_SECRET_KEY", "x")

    app = create_app({"TESTING": True})

    assert app.config["JOB_ROOT"] == str(tmp_path / "volume")
    assert app.config["MAX_CONTENT_LENGTH"] == 5 * 1024 * 1024
    assert app.config["JOB_TTL_HOURS"] == 2
    assert (tmp_path / "volume").is_dir()


def test_security_headers_and_health(tmp_path: Path) -> None:
    client = make_app(tmp_path).test_client()

    response = client.get("/health")

    assert response.json == {"status": "ok", "version": __version__}
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_house_style_layout_and_self_hosted_font(tmp_path: Path) -> None:
    client = make_app(tmp_path).test_client()

    body = client.get("/").data.decode()

    assert 'href="#content"' in body and 'id="content"' in body
    assert 'aria-current="step"' in body
    assert "fonts.googleapis.com" not in body
    assert "library.maastrichtuniversity.nl/wp-content" not in body
    font = client.get("/static/fonts/open-sans-latin-400-normal.woff2")
    assert font.status_code == 200
    assert len(font.data) > 10_000
    css = client.get("/static/style.css").data.decode()
    assert "#001c3d" in css and "#d2460f" in css.casefold() and "Open Sans" in css


THREE_WAY_DOI = b"""TY  - CHAP
TI  - Randomized denoising autoencoders for smaller and efficient imaging based AD clinical trials
AU  - Ithapu, V. K.
AU  - Singh, V.
AU  - Okonkwo, O.
AU  - Johnson, S. C.
PY  - 2014
SP  - 470
EP  - 478
DO  - 10.1007/978-3-319-10470-6_59
ER  -
TY  - CHAP
TI  - Randomized Denoising Autoencoders for Smaller and Efficient Imaging Based AD Clinical Trials
AU  - Ithapu, Vamsi K.
AU  - Singh, Vikas
AU  - Okonkwo, Ozioma
AU  - Johnson, Sterling C.
PY  - 2022
DO  - 10.1007/978-3-319-10470-6_59
ER  -
TY  - CHAP
TI  - Randomized denoising autoencoders for smaller and efficient imaging based AD clinical trials
AU  - Ithapu, V. K.
AU  - Singh, V.
AU  - Okonkwo, O.
AU  - Johnson, S. C.
PY  - 2014
SP  - 470
EP  - 478
DO  - 10.1007/978-3-319-10470-6_59
ER  -
"""


def test_three_records_sharing_a_doi_are_reviewed_together(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    response = upload(client, THREE_WAY_DOI, "chapters.ris")

    job_id = job_id_of(client)
    store = app.extensions["job_store"]
    [group] = store.load_json(job_id, "groups")
    assert len(group["record_ids"]) == 2
    reviews = store.load_json(job_id, "review_candidates")
    assert len(reviews) == 2  # pairwise checks are kept for transparency
    [cluster] = store.load_json(job_id, "review_clusters")
    assert len(cluster["record_ids"]) == 3
    assert len(cluster["candidate_ids"]) == 2
    master = {record["source_row"]: record["record_id"] for record in store.load_json(job_id, "master")}
    first, odd, third = master[1], master[2], master[3]
    assert cluster["identical_to"][first] == [third]
    assert cluster["same_group_members"][first] == [third]
    assert cluster["agreement"]["year"]["odd"] == [odd]

    body = response.data.decode()
    assert body.count('class="card conflict-card review-cluster"') == 1
    assert "1 ambiguous match(es) kept separate" in body
    assert "Identical to row 3" in body
    assert "Duplicate of row 3" in body
    assert "Pairwise checks behind this review (2)" in body

    # Keep a chosen subset: drop only the odd 2022 record.
    client.post(
        f"/jobs/{job_id}/deduplicate",
        data={
            "mode": "automatic",
            f"reviewcluster.{cluster['cluster_id']}": "1",
            f"reviewkeep.{cluster['cluster_id']}": [first, third],
        },
    )
    kept_ids = {record["record_id"] for record in store.load_json(job_id, "deduplicated")}
    # The group keeps one of the two identical copies; the 2022 record is gone.
    assert odd not in kept_ids
    assert len(kept_ids) == 1
    report = store.load_json(job_id, "deduplication_report")
    assert report["removals"][odd]["via"] == "manual review"

    # Keeping nothing is refused; leaving every box ticked keeps all.
    response = client.post(
        f"/jobs/{job_id}/deduplicate",
        data={"mode": "automatic", f"reviewcluster.{cluster['cluster_id']}": "1"},
        follow_redirects=True,
    )
    assert b"keep at least one record" in response.data
    client.post(
        f"/jobs/{job_id}/deduplicate",
        data={
            "mode": "manual",
            f"keep.{group['group_id']}": group["record_ids"],
            f"reviewcluster.{cluster['cluster_id']}": "1",
            f"reviewkeep.{cluster['cluster_id']}": [first, odd, third],
        },
    )
    assert len(store.load_json(job_id, "deduplicated")) == 3
    assert store.load_json(job_id, "deduplication_report")["review_cluster_choices"] == {}


def test_review_clusters_are_rebuilt_for_older_jobs(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    upload(client, THREE_WAY_DOI, "chapters.ris")
    job_id = job_id_of(client)
    store = app.extensions["job_store"]
    store.job_dir(job_id).joinpath("review_clusters.json").unlink()

    response = client.get(f"/jobs/{job_id}/results")

    assert response.status_code == 200
    assert len(store.load_json(job_id, "review_clusters")) == 1
