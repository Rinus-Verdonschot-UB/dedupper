from __future__ import annotations

from dedupper.dedup import (
    analyze_duplicates,
    analyze_records,
    apply_group_selections,
    conference_abstract_report,
    enrich_retained_records,
    find_duplicate_groups,
    is_conference_abstract,
    normalize_doi,
    normalize_title,
    publication_kind,
)


def record(
    record_id: str,
    title: str,
    *,
    year: str = "2024",
    author: str = "Smith",
    doi: str = "",
    journal: str = "Journal",
    extra: str = "",
    publication_type: str = "",
    source_file_id: str = "file1",
    authors: str = "",
    pages: str = "",
    article_number: str = "",
    issn: str = "",
    isbn: str = "",
    abstract: str = "",
) -> dict:
    return {
        "record_id": record_id,
        "title": title,
        "year": year,
        "first_author": author,
        "authors": authors,
        "doi": doi,
        "journal": journal,
        "pages": pages,
        "article_number": article_number,
        "issn": issn,
        "isbn": isbn,
        "abstract": abstract,
        "publication_type": publication_type,
        "source_file_id": source_file_id,
        "source_filename": f"{source_file_id}.ris",
        "raw_fields": {"extra": extra} if extra else {},
    }


def test_normalization_handles_doi_urls_html_entities_and_apostrophes() -> None:
    assert normalize_doi("https://doi.org/10.1000/ABC.1.") == "10.1000/abc.1"
    assert normalize_doi("doi:10.1016/S0140-6736(09)62124-3") == (
        "10.1016/s0140-6736(09)62124-3"
    )
    assert normalize_doi(
        "https://dx.doi.org/10.1016/S0140-6736%2824%2900915-2"
    ) == "10.1016/s0140-6736(24)00915-2"
    assert normalize_doi("(doi:10.1234/example(test))") == (
        "10.1234/example(test)"
    )
    left = "Children’s language &amp; memory: a review"
    right = "Children's language and memory - a review"
    assert normalize_title(left) == normalize_title(right)


def test_exact_doi_can_group_records_with_different_titles() -> None:
    records = [
        record(
            "a",
            "Online title",
            doi="https://doi.org/10.1000/X",
            authors="Smith J; Jones P",
        ),
        record(
            "b",
            "Print title",
            doi="doi:10.1000/x",
            authors="Smith, John; Jones, Peter",
            extra="more",
        ),
        record("c", "Unrelated"),
    ]

    groups = find_duplicate_groups(records)

    assert len(groups) == 1
    assert set(groups[0]["record_ids"]) == {"a", "b"}
    assert "Corroborated DOI" in groups[0]["methods"]
    assert "Same DOI" in groups[0]["edges"][0]["signals"]


def test_encoded_elsevier_doi_suffixes_do_not_collapse_to_the_same_prefix() -> None:
    records = [
        record(
            "a",
            "A deliberately identical title",
            doi=(
                "https://dx.doi.org/"
                "10.1016/S0140-6736%2824%2900915-2"
            ),
        ),
        record(
            "b",
            "A deliberately identical title",
            doi=(
                "https://dx.doi.org/"
                "10.1016/S0140-6736%2824%2912345-6"
            ),
        ),
    ]

    assert find_duplicate_groups(records) == []


def test_same_source_conference_abstracts_do_not_group_on_container_doi() -> None:
    records = [
        record(
            f"abstract-{index}",
            f"Scientific conference finding number {index} in retinal medicine",
            author=f"Author{index}",
            doi="10.1038/s41433-025-03831-0",
            publication_type="Conference Abstract",
        )
        for index in range(1, 39)
    ]

    groups = find_duplicate_groups(records)
    report = conference_abstract_report(records, groups)

    assert groups == []
    assert report["identified_records"] == 38
    assert report["protected_records"] == 38
    assert report["same_source_shared_doi_clusters"] == 1
    assert report["overlapping_dois"] == 1
    assert report["duplicate_groups"] == 0


def test_same_source_conference_abstract_true_duplicate_needs_title_and_author() -> None:
    doi = "10.1038/s41433-025-03831-0"
    records = [
        record(
            "a",
            "Efficacy of a novel retinal treatment: preliminary results",
            author="Van Dijk",
            doi=doi,
            publication_type="Conference Abstract",
        ),
        record(
            "b",
            "Efficacy of a novel retinal treatment - preliminary results",
            author="van Dijk",
            doi=doi,
            publication_type="Conference Abstract",
        ),
        record(
            "c",
            "Efficacy of a novel retinal treatment: preliminary results",
            author="Jones",
            doi=doi,
            publication_type="Conference Abstract",
        ),
    ]

    groups = find_duplicate_groups(records)

    assert len(groups) == 1
    assert set(groups[0]["record_ids"]) == {"a", "b"}
    assert groups[0]["conference_abstract_record_ids"] == ["a", "b"]
    assert "Conference abstract title + first author" in groups[0]["methods"]


def test_conference_abstract_initial_or_changed_number_is_not_enough() -> None:
    doi = "10.1038/s41433-025-03831-0"
    records = [
        record(
            "a",
            "Treatment response after 12 months in retinal disease",
            author="M.",
            doi=doi,
            publication_type="Conference Abstract",
        ),
        record(
            "b",
            "Treatment response after 24 months in retinal disease",
            author="M.",
            doi=doi,
            publication_type="Conference Abstract",
        ),
    ]

    assert find_duplicate_groups(records) == []


def test_cross_file_conference_abstracts_match_without_merging_proceedings() -> None:
    doi = "10.1038/s41433-025-03831-0"
    records = [
        record(
            "a1",
            "Novel imaging markers in diabetic retinopathy",
            author="Smith",
            doi=doi,
            publication_type="Conference Abstract",
            source_file_id="embase",
        ),
        record(
            "a2",
            "Social deprivation and quality of life in macular degeneration",
            author="Jones",
            doi=doi,
            publication_type="Conference Abstract",
            source_file_id="embase",
        ),
        record(
            "b1",
            "Novel imaging markers in diabetic retinopathy.",
            author="Smith",
            doi=doi,
            publication_type="Conference Abstract",
            source_file_id="scopus",
        ),
        record(
            "b2",
            "Safety of a biosimilar treatment in retinal disease",
            author="Brown",
            doi=doi,
            publication_type="Conference Abstract",
            source_file_id="scopus",
        ),
    ]

    groups = find_duplicate_groups(records)
    report = conference_abstract_report(records, groups)

    assert len(groups) == 1
    assert set(groups[0]["record_ids"]) == {"a1", "b1"}
    assert report["same_source_shared_doi_clusters"] == 2
    assert report["overlapping_dois"] == 1
    assert report["cross_file_duplicate_groups"] == 1


def test_conference_abstract_type_can_be_read_from_raw_pt() -> None:
    item = record("a", "A conference result")
    item["raw_fields"] = {"PT": ["Conference Abstract"]}

    assert is_conference_abstract(item)


def test_cleaned_and_fuzzy_titles_form_groups() -> None:
    records = [
        record("a", "Children’s language &amp; memory: a review"),
        record("b", "Children's language and memory - a review"),
        record("c", "Neural mechanisms underlying visual word recognition"),
        record("d", "Neural mechanism underlying visual word recognition"),
    ]

    groups = find_duplicate_groups(records, fuzzy_threshold=90)

    assert {frozenset(group["record_ids"]) for group in groups} == {
        frozenset({"a", "b"}),
        frozenset({"c", "d"}),
    }


def test_automatic_choice_prefers_valid_doi_then_completeness() -> None:
    records = [
        record("a", "Same title"),
        record("b", "Same title", doi="10.1234/test", extra="abstract"),
    ]
    groups = find_duplicate_groups(records)

    kept, removed, selections = apply_group_selections(records, groups)

    assert [item["record_id"] for item in kept] == ["b"]
    assert [item["record_id"] for item in removed] == ["a"]
    assert next(iter(selections.values())) == ["b"]


def test_manual_choice_can_keep_more_than_one_record_in_a_group() -> None:
    records = [
        record("a", "Same title"),
        record("b", "Same title", doi="10.1234/test", extra="abstract"),
        record("c", "Same title"),
    ]
    groups = find_duplicate_groups(records)

    kept, removed, selections = apply_group_selections(
        records,
        groups,
        {groups[0]["group_id"]: ["a", "b"]},
    )

    assert [item["record_id"] for item in kept] == ["a", "b"]
    assert [item["record_id"] for item in removed] == ["c"]
    assert selections[groups[0]["group_id"]] == ["a", "b"]


def test_manual_choice_requires_at_least_one_record_per_group() -> None:
    records = [
        record("a", "Same title"),
        record("b", "Same title", doi="10.1234/test"),
    ]
    groups = find_duplicate_groups(records)

    try:
        apply_group_selections(records, groups, {groups[0]["group_id"]: []})
    except ValueError as exc:
        assert "at least one record" in str(exc)
    else:
        raise AssertionError("An empty manual selection should be rejected.")


def test_different_dois_override_an_identical_cleaned_title() -> None:
    records = [
        record("a", "The same article title", doi="10.1000/article-a"),
        record("b", "The same article title", doi="10.1000/article-b"),
    ]

    assert find_duplicate_groups(records) == []


def test_doi_less_exact_title_is_not_arbitrarily_assigned_between_two_dois() -> None:
    title = "A recurring title attached to two known publications"
    records = [
        record("unknown", title, doi=""),
        record("a", title, doi="10.1000/article-a"),
        record("b", title, doi="10.1000/article-b"),
    ]

    assert find_duplicate_groups(records) == []


def test_commentary_is_not_grouped_with_the_article_it_discusses() -> None:
    original_title = (
        "A randomized controlled evaluation of a secondary school mindfulness "
        "program for early adolescents: Do we have the recipe right yet?"
    )
    records = [
        record("pubmed", original_title, year="2017", doi=""),
        record(
            "commentary",
            "Commentary regarding Johnson et al (2017) "
            f"'{original_title}'",
            year="2018",
            author="Bailey",
            doi="10.1007/s12671-018-0936-x",
        ),
        record(
            "original",
            original_title,
            year="2017",
            author="Johnson",
            doi="10.1016/j.brat.2017.09.001",
        ),
    ]

    groups = find_duplicate_groups(records)

    assert len(groups) == 1
    assert set(groups[0]["record_ids"]) == {"pubmed", "original"}


def test_correction_is_not_grouped_with_the_article_it_corrects() -> None:
    original_title = (
        "Effects of a mindful parenting workshop for parents of adolescents "
        "and young adults following social unrest in Hong Kong"
    )
    records = [
        record("pubmed", original_title, year="2022", doi=""),
        record(
            "original",
            original_title,
            year="2022",
            author="Lo",
            doi="10.1007/s12671-021-01790-7",
        ),
        record(
            "correction",
            f"'{original_title}': Correction",
            year="2022",
            author="Lo",
            doi="10.1007/s12671-021-01812-4",
        ),
    ]

    groups = find_duplicate_groups(records)

    assert len(groups) == 1
    assert set(groups[0]["record_ids"]) == {"pubmed", "original"}
    assert publication_kind(records[-1]["title"]) == "correction"


def test_true_mindful_balance_duplicates_still_form_one_group() -> None:
    records = [
        record(
            "a",
            "Mindful BALANCE (breathe act learn about 'now' care everyday): "
            "A pilot project for depressed female adolescents",
            year="",
            author="Rich BW",
        ),
        record(
            "b",
            "Mindful BALANCE (breathe act learn about ‘now’ care everyday): "
            "A pilot project for depressed female adolescents",
            year="2021",
            author="Rich",
            doi="10.1111/jcap.12308",
        ),
        record(
            "c",
            "Mindful balance (breathe act learn about ‘now’ care every day): "
            "A pilot project for depressed female adolescents",
            year="2020",
            author="Rich",
        ),
    ]

    groups = find_duplicate_groups(records, fuzzy_threshold=90)

    assert len(groups) == 1
    assert set(groups[0]["record_ids"]) == {"a", "b", "c"}


def test_same_related_publication_can_still_be_deduplicated() -> None:
    title = "Correction to: A useful scientific article"
    records = [
        record("a", title, doi=""),
        record("b", title, doi="10.1000/correction"),
    ]

    groups = find_duplicate_groups(records)

    assert len(groups) == 1
    assert set(groups[0]["record_ids"]) == {"a", "b"}


def test_mixed_methods_protocol_is_flagged_and_not_recommended() -> None:
    records = [
        record(
            "study",
            "Mindfulness-based cognitive therapy experiences in youth with "
            "inflammatory bowel disease and depression: findings from a mixed "
            "methods qualitative study",
            year="2020",
        ),
        record(
            "protocol",
            "Mindfulness-Based Cognitive Therapy Experiences in Youth With "
            "Inflammatory Bowel Disease and Depression: Protocol for a Mixed "
            "Methods Qualitative Study",
            year="2020",
            doi="10.2196/14432",
            extra="More complete metadata that would otherwise win",
        ),
    ]

    # v0.8.0: a protocol and its results paper are different publications.
    # They are linked as related records instead of grouped as duplicates, so
    # neither can be removed automatically.
    analysis = analyze_records(records)

    assert analysis["groups"] == []
    assert len(analysis["related_links"]) == 1
    link = analysis["related_links"][0]
    assert set(link["record_ids"]) == {"study", "protocol"}
    assert link["rule"] == "V1"
    assert link["relationship"] == "Protocol and results publication"
    assert analysis["versions"]["protocol"]["kind"] == "protocol"
    assert analysis["versions"]["study"]["kind"] == "unspecified"


def test_randomized_trial_protocol_variant_is_flagged() -> None:
    records = [
        record(
            "study",
            "Online Mindfulness-Based Cognitive Behavioral Therapy Intervention "
            "for Youth With Major Depressive Disorders: Randomized Controlled Trial",
            year="2021",
        ),
        record(
            "protocol",
            "An Online Mindfulness-Based Cognitive Behavioral Therapy Intervention "
            "for Youth Diagnosed With Major Depressive Disorders: Protocol for a "
            "Randomized Controlled Trial",
            year="2021",
        ),
    ]

    analysis = analyze_records(records)

    assert analysis["groups"] == []
    assert [set(link["record_ids"]) for link in analysis["related_links"]] == [
        {"study", "protocol"}
    ]
    assert analysis["related_links"][0]["confidence"] == "likely"


def test_duplicate_copies_of_protocol_are_not_contextually_flagged() -> None:
    title = "Study protocol for a randomized controlled trial of treatment"
    records = [
        record("a", title),
        record("b", title, doi="10.1234/protocol", extra="abstract"),
    ]

    groups = find_duplicate_groups(records)

    assert len(groups) == 1
    assert groups[0]["potential_protocol_record_ids"] == []
    assert groups[0]["recommended_record_id"] == "b"


def test_ordinary_early_use_of_protocol_is_not_a_protocol_marker() -> None:
    records = [
        record(
            "a",
            "Protocol adherence and treatment outcomes in adolescent depression",
        ),
        record(
            "b",
            "Adherence and treatment outcomes in adolescent depression",
        ),
    ]

    groups = find_duplicate_groups(records, fuzzy_threshold=80)

    assert len(groups) == 1
    assert groups[0]["potential_protocol_record_ids"] == []


def test_identical_doi_with_conflicting_title_and_author_requires_review() -> None:
    records = [
        record(
            "a",
            "Effects of treatment on childhood language",
            author="Smith",
            doi="10.1000/reused",
        ),
        record(
            "b",
            "Quantum signatures in distant stellar systems",
            author="Jones",
            doi="10.1000/reused",
        ),
    ]

    groups, reviews = analyze_duplicates(records)

    assert groups == []
    assert len(reviews) == 1
    assert reviews[0]["label"] == "DOI metadata conflict"
    assert "title and first-author metadata conflict" in reviews[0]["reason"]


def test_same_doi_with_only_year_and_journal_is_not_enough() -> None:
    records = [
        record(
            "a",
            "A retinal treatment outcome in children",
            author="",
            doi="10.1000/container",
        ),
        record(
            "b",
            "A distinct surgical technique in older adults",
            author="",
            doi="10.1000/container",
        ),
    ]

    groups, reviews = analyze_duplicates(records)

    assert groups == []
    assert reviews[0]["label"] == "DOI metadata conflict"


def test_identical_title_without_corroboration_is_review_only() -> None:
    title = "A distinctive but otherwise uncorroborated publication title"
    records = [
        record("a", title, year="", author="", journal=""),
        record("b", title, year="", author="", journal=""),
    ]

    groups, reviews = analyze_duplicates(records)

    assert groups == []
    assert reviews[0]["label"] == "Uncorroborated title match"


def test_generic_publication_type_alone_does_not_corroborate_a_title() -> None:
    title = "A distinctive but otherwise uncorroborated publication title"
    records = [
        record(
            "a",
            title,
            year="",
            author="",
            journal="",
            publication_type="Journal Article",
        ),
        record(
            "b",
            title,
            year="",
            author="",
            journal="",
            publication_type="Journal Article",
        ),
    ]

    groups, reviews = analyze_duplicates(records)

    assert groups == []
    assert reviews[0]["label"] == "Uncorroborated title match"


def test_one_year_difference_is_allowed_with_supporting_evidence() -> None:
    records = [
        record(
            "a",
            "Neural mechanisms supporting visual word recognition",
            year="2023",
            author="Van Dijk",
            authors="Van Dijk P; Jones AB",
            journal="Journal of Reading",
        ),
        record(
            "b",
            "Neural mechanism supporting visual word recognition",
            year="2024",
            author="van Dijk",
            authors="Van Dijk, Peter; Jones, Anna",
            journal="Journal of Reading",
        ),
    ]

    groups = find_duplicate_groups(records, fuzzy_threshold=90)

    assert len(groups) == 1
    assert "year +1" in groups[0]["edges"][0]["signals"]
    assert "full author list match" in groups[0]["edges"][0]["signals"]


def test_abbreviated_and_full_page_ranges_share_start_page_evidence() -> None:
    records = [
        record(
            "a",
            "A nearly identical article title in its online version",
            year="2024",
            author="",
            journal="",
            pages="482-91",
        ),
        record(
            "b",
            "A nearly identical article title in the online version",
            year="2024",
            author="",
            journal="",
            pages="482-491",
        ),
    ]

    groups = find_duplicate_groups(records, fuzzy_threshold=90)

    assert len(groups) == 1
    assert "pages match" in groups[0]["edges"][0]["signals"]


def test_transitive_grouping_cannot_bridge_a_two_year_conflict() -> None:
    title = "A stable scientific title for an ahead of print article"
    records = [
        record("a", title, year="2022", author="Smith"),
        record("bridge", title, year="2023", author="Smith"),
        record("c", title, year="2024", author="Smith"),
    ]

    groups, reviews = analyze_duplicates(records)

    assert max(len(group["record_ids"]) for group in groups) == 2
    assert any("joining its existing groups" in item["reason"] for item in reviews)


def test_enrichment_fills_only_unambiguous_missing_metadata_and_audits_it() -> None:
    records = [
        record(
            "selected",
            "A useful scientific title",
            authors="Smith J; Jones P",
            abstract="",
            pages="",
            issn="",
            source_file_id="scopus",
        ),
        record(
            "copy",
            "A useful scientific title.",
            authors="Smith J; Jones P",
            abstract="A complete abstract copied from the duplicate.",
            pages="23-29",
            issn="1234-5678",
            source_file_id="pubmed",
        ),
    ]
    groups = find_duplicate_groups(records)
    selections = {groups[0]["group_id"]: ["selected"]}
    kept, _, final = apply_group_selections(records, groups, selections)

    enriched, audit = enrich_retained_records(kept, records, groups, final)

    assert enriched[0]["abstract"].startswith("A complete abstract")
    assert enriched[0]["pages"] == "23-29"
    assert enriched[0]["issn"] == "1234-5678"
    assert len(enriched[0]["source_provenance"]) == 2
    assert {entry["field"] for entry in audit[0]["fields"]} >= {
        "abstract",
        "pages",
        "issn",
    }


def test_enrichment_does_not_guess_when_duplicate_values_disagree() -> None:
    records = [
        record("selected", "A shared article title", pages=""),
        record("a", "A shared article title", pages="10-12"),
        record("b", "A shared article title", pages="20-22"),
    ]
    groups = find_duplicate_groups(records)
    selections = {groups[0]["group_id"]: ["selected"]}
    kept, _, final = apply_group_selections(records, groups, selections)

    enriched, audit = enrich_retained_records(kept, records, groups, final)

    assert enriched[0]["pages"] == ""
    page_audit = next(item for item in audit[0]["fields"] if item["field"] == "pages")
    assert page_audit["action"] == "left missing because duplicates disagree"


def test_protocol_metadata_is_not_copied_into_the_non_protocol_study() -> None:
    records = [
        record(
            "study",
            "Online therapy for youth depression: randomized controlled trial",
            abstract="",
            doi="",
        ),
        record(
            "protocol",
            "Online therapy for youth depression: protocol for a randomized controlled trial",
            doi="10.1000/protocol",
            abstract="Protocol methods, not the final study abstract.",
        ),
    ]
    analysis = analyze_records(records, fuzzy_threshold=85)
    groups = analysis["groups"]
    assert groups == []
    assert len(analysis["related_links"]) == 1
    kept, _, final = apply_group_selections(records, groups, {})

    enriched, _ = enrich_retained_records(kept, records, groups, final)

    study = next(item for item in enriched if item["record_id"] == "study")
    assert study["doi"] == ""
    assert study["abstract"] == ""
    assert len(enriched) == 2
