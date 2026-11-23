"""v0.8.0 regression tests: publication versions and related publications."""

from __future__ import annotations

import pytest

from dedupper.dedup import analyze_records, apply_pair_decisions, compare_fields
from dedupper.rules import RULES, settings_from_env
from dedupper.versions import classify_version, version_compatibility

TITLE = (
    "Effect of remote ischaemic conditioning on clinical outcomes after acute "
    "myocardial infarction"
)


def rec(record_id: str, title: str = TITLE, **fields: object) -> dict:
    base = {
        "record_id": record_id,
        "source_file_id": fields.pop("source_file_id", "file1"),
        "source_filename": "source.ris",
        "source_row": 1,
        "title": title,
        "year": "2021",
        "first_author": "Smith",
        "authors": "",
        "doi": "",
        "journal": "",
        "pages": "",
        "article_number": "",
        "issn": "",
        "isbn": "",
        "abstract": "",
        "publication_type": "",
        "raw_fields": {},
    }
    base.update(fields)
    return base


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "kind", "evidence_fragment"),
    [
        ({"doi": "10.1101/2020.05.01.20087411"}, "preprint", "bioRxiv / medRxiv"),
        ({"doi": "10.1101/123456"}, "preprint", "bioRxiv / medRxiv"),
        ({"doi": "10.48550/arXiv.2301.00001"}, "preprint", "arXiv"),
        ({"doi": "10.2139/ssrn.3456789"}, "preprint", "SSRN"),
        ({"doi": "10.21203/rs.3.rs-123/v1"}, "preprint", "Research Square"),
        ({"journal": "medRxiv"}, "preprint", "preprint server"),
        ({"publication_type": "Preprint"}, "preprint", "publication type"),
        ({"journal": "ClinicalTrials.gov"}, "registry", "trial registry"),
        (
            {"raw_fields": {"UR": ["https://clinicaltrials.gov/show/NCT01234567"]}},
            "registry",
            "trial registry",
        ),
        ({"publication_type": "Conference Abstract"}, "conference_abstract", "Conference Abstract"),
        ({"raw_fields": {"TY": ["CONF"]}}, "conference_paper", "RIS type CONF"),
        ({"publication_type": "Proceedings Paper"}, "conference_paper", "Proceedings Paper"),
        ({"raw_fields": {"TY": ["THES"]}}, "thesis", "THES"),
        ({"publication_type": "Clinical Trial Protocol"}, "protocol", "Protocol"),
        ({"publication_type": "Published Erratum"}, "correction", "Erratum"),
        ({"publication_type": "Retraction of Publication"}, "retraction", "Retraction"),
        ({"publication_type": "Journal Article"}, "article", "Journal Article"),
    ],
)
def test_classify_version_labels_and_explains(fields, kind, evidence_fragment) -> None:
    version = classify_version(rec("a", **fields))

    assert version["kind"] == kind
    assert any(evidence_fragment.casefold() in item.casefold() for item in version["evidence"])


def test_cold_spring_harbor_journal_doi_is_not_a_preprint() -> None:
    version = classify_version(rec("a", doi="10.1101/gr.123456.120", journal="Genome Research"))

    assert version["kind"] == "unspecified"


def test_retracted_publication_is_not_a_retraction_notice() -> None:
    version = classify_version(rec("a", publication_type="Retracted Publication"))

    assert version["kind"] == "unspecified"


def test_title_markers_are_detected() -> None:
    assert classify_version(rec("a", f"{TITLE}: study protocol"))["kind"] == "protocol"
    assert classify_version(rec("a", f"Erratum: {TITLE}"))["kind"] == "correction"
    assert classify_version(rec("a", f"{TITLE} [preprint]"))["kind"] == "preprint"
    assert classify_version(rec("a", "Protocol adherence in adolescent care"))["kind"] == "unspecified"


def test_preprint_type_beats_journal_article_type() -> None:
    version = classify_version(rec("a", publication_type="Journal Article; Preprint"))

    assert version["kind"] == "preprint"


def test_version_compatibility() -> None:
    article = classify_version(rec("a", publication_type="Journal Article"))
    untyped = classify_version(rec("b"))
    abstract = classify_version(rec("c", publication_type="Conference Abstract"))
    preprint_doi = classify_version(rec("d", doi="10.1101/2020.05.01.20087411"))

    assert version_compatibility(article, untyped) == "same"
    assert version_compatibility(abstract, untyped) == "type_known_once"
    assert version_compatibility(abstract, article) == "distinct"
    assert version_compatibility(preprint_doi, untyped) == "distinct"


# --- related publications instead of silent merges --------------------------


def test_preprint_and_journal_article_are_linked_not_merged() -> None:
    records = [
        rec("article", doi="10.1016/j.example.2021.01.001", journal="Lancet"),
        rec("preprint", doi="10.1101/2020.05.01.20087411", journal="medRxiv", year="2020"),
    ]

    analysis = analyze_records(records)

    assert analysis["groups"] == []
    [link] = analysis["related_links"]
    assert link["rule"] == "V1"
    assert link["relationship"] == "Preprint and journal version"
    assert link["confidence"] == "likely"
    assert "both are kept" in link["reason"]
    assert link["comparison"]["doi"]["status"] == "conflict"


def test_preprint_without_doi_is_no_longer_a_fuzzy_duplicate() -> None:
    """v0.7.0 grouped these as 'Corroborated fuzzy title'."""

    records = [
        rec("article", journal="Lancet"),
        rec("preprint", f"{TITLE} [preprint]", journal="medRxiv"),
    ]

    analysis = analyze_records(records)

    assert analysis["groups"] == []
    assert len(analysis["related_links"]) == 1


def test_conference_abstract_and_full_article_are_not_merged() -> None:
    """v0.7.0 grouped these (year one apart) and could delete one automatically."""

    records = [
        rec("article", doi="10.1016/j.example.2021.01.001", journal="Lancet"),
        rec(
            "abstract",
            year="2020",
            journal="European Heart Journal",
            publication_type="Conference Abstract",
            source_file_id="embase",
        ),
    ]

    analysis = analyze_records(records)

    assert analysis["groups"] == []
    [link] = analysis["related_links"]
    assert link["relationship"] == "Conference abstract and full publication"
    assert link["rule"] == "V2"


def test_conference_abstract_and_untyped_copy_in_same_journal_still_group() -> None:
    records = [
        rec("untyped", year="2020", journal="European Heart Journal", source_file_id="scopus"),
        rec(
            "typed",
            year="2020",
            journal="European Heart Journal",
            publication_type="Conference Abstract",
            source_file_id="embase",
        ),
    ]

    analysis = analyze_records(records)

    [group] = analysis["groups"]
    assert set(group["record_ids"]) == {"untyped", "typed"}
    assert group["cautions"] == ["Publication type stated for only one copy"]
    assert group["needs_attention"]


def test_correction_with_its_own_doi_is_linked_to_the_article() -> None:
    records = [
        rec("article", doi="10.1016/j.example.2021.01.001"),
        rec("erratum", f"Erratum: {TITLE}", doi="10.1016/j.example.2021.01.099"),
    ]

    analysis = analyze_records(records)

    assert analysis["groups"] == []
    [link] = analysis["related_links"]
    assert "Correction" in link["relationship"]


def test_unrelated_preprint_is_not_linked() -> None:
    records = [
        rec("article", "Sleep quality and burnout among hospital nurses in Belgium"),
        rec(
            "preprint",
            "Machine learning prediction of sepsis in emergency departments",
            doi="10.1101/2020.05.01.20087411",
        ),
    ]

    assert analyze_records(records)["related_links"] == []


# --- explanations for every decision ----------------------------------------


def test_every_edge_and_decision_has_a_known_rule_and_comparison() -> None:
    records = [
        rec("a", doi="10.1000/x", journal="J", pages="10-20"),
        rec("b", doi="10.1000/x", journal="J", pages="10-20", source_file_id="file2"),
        rec("c", f"{TITLE}: study protocol", doi="10.1000/protocol"),
        rec("d", doi="10.1000/other", year="2021"),
    ]

    analysis = analyze_records(records)

    for group in analysis["groups"]:
        assert group["rules"]
        assert group["recommendation_reason"].startswith("Recommended because")
        for edge in group["edges"]:
            assert edge["rule"] in RULES
            assert edge["comparison"]["title"]["status"] == "match"
    for item in analysis["related_links"] + analysis["separated_pairs"]:
        assert item["rule"] in RULES
        assert item["comparison"]
    assert any(pair["rule"] == "S1" for pair in analysis["separated_pairs"])


def test_same_title_with_different_dois_is_reported_as_a_near_miss() -> None:
    records = [
        rec("a", doi="10.1000/article-a"),
        rec("b", doi="10.1000/article-b"),
    ]

    analysis = analyze_records(records)

    assert analysis["groups"] == []
    [pair] = analysis["separated_pairs"]
    assert pair["rule"] == "S1"
    assert "different DOIs" in pair["reason"]


def test_doi_less_copy_of_a_title_with_two_dois_becomes_a_review_item() -> None:
    records = [
        rec("unknown"),
        rec("a", doi="10.1000/article-a"),
        rec("b", doi="10.1000/article-b"),
    ]

    analysis = analyze_records(records)

    assert analysis["groups"] == []
    review = next(item for item in analysis["review_candidates"] if item["rule"] == "R4")
    assert review["record_ids"][0] == "unknown"
    assert set(review["record_ids"]) == {"unknown", "a", "b"}


def test_group_conflict_has_its_own_label() -> None:
    records = [
        rec("a", year="2022"),
        rec("bridge", year="2023"),
        rec("c", year="2024"),
    ]

    analysis = analyze_records(records)

    labels = {item["label"] for item in analysis["review_candidates"]}
    assert "Group conflict" in labels
    assert "Uncorroborated title match" not in labels


def test_compare_fields_statuses() -> None:
    left = rec("a", doi="10.1000/x", year="2020", journal="Lancet", issn="1234-5678")
    right = rec("b", doi="10.1000/y", year="2021", journal="The Lancet", first_author="Jones")

    comparison = compare_fields(left, right)

    assert comparison["doi"]["status"] == "conflict"
    assert comparison["year"]["status"] == "compatible"
    assert comparison["journal"]["status"] == "compatible"
    assert comparison["first_author"]["status"] == "conflict"
    assert comparison["issn"]["status"] == "missing"
    assert comparison["isbn"]["status"] == "absent"


# --- user decisions on review items / related links -------------------------


def test_apply_pair_decisions_keeps_only_the_chosen_record() -> None:
    records = [rec("a"), rec("b"), rec("c")]
    items = [{"link_id": "L1", "record_ids": ["a", "b"]}]

    kept, removed, applied = apply_pair_decisions(records, [], items, {"L1": "b"}, "link_id")

    assert [record["record_id"] for record in kept] == ["b", "c"]
    assert [record["record_id"] for record in removed] == ["a"]
    assert applied == {"L1": "b"}

    kept, removed, applied = apply_pair_decisions(records, [], items, {"L1": "all"}, "link_id")
    assert len(kept) == 3 and removed == [] and applied == {}

    with pytest.raises(ValueError):
        apply_pair_decisions(records, [], items, {"L1": "zzz"}, "link_id")


def test_threshold_overrides_are_validated() -> None:
    assert settings_from_env({"DEDUPPER_FUZZY_TITLE_THRESHOLD": "95"})["fuzzy_title_threshold"] == 95
    with pytest.raises(ValueError):
        settings_from_env({"DEDUPPER_FUZZY_TITLE_THRESHOLD": "12"})
    with pytest.raises(ValueError):
        settings_from_env({"DEDUPPER_RELATED_TITLE_THRESHOLD": "high"})


def test_related_versions_form_one_family_with_duplicate_group_as_one_unit() -> None:
    title = (
        "Remote ischaemic conditioning after acute myocardial infarction: a "
        "randomised controlled trial"
    )
    records = [
        rec("article", title, doi="10.1000/ex.1", journal="Example J", pages="101-110",
            publication_type="Journal Article"),
        rec("article_copy", title, doi="10.1000/EX.1", journal="Example J", pages="101-10",
            source_file_id="file2"),
        rec("preprint", title, doi="10.1101/2023.11.01.23297811", journal="medRxiv", year="2023"),
        rec("abstract", "Remote ischaemic conditioning after acute myocardial infarction",
            journal="Eur Heart J", year="2022", publication_type="Conference Abstract"),
        rec("protocol", title.replace(": a randomised", ": study protocol for a randomised"),
            doi="10.1186/s13063-021-1", journal="Trials", year="2021"),
    ]

    analysis = analyze_records(records)

    [group] = analysis["groups"]
    assert set(group["record_ids"]) == {"article", "article_copy"}
    [family] = analysis["related_families"]
    kinds = sorted(unit["kind"] for unit in family["units"])
    assert kinds == ["article", "conference_abstract", "preprint", "protocol"]
    article_unit = next(unit for unit in family["units"] if unit["kind"] == "article")
    assert article_unit["group_id"] == group["group_id"]
    # Pairwise links to both copies of the article collapse to one per unit pair.
    unit_pairs = {tuple(link["units"]) for link in family["links"]}
    assert len(unit_pairs) == len(family["links"])
