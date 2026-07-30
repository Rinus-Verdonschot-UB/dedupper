from __future__ import annotations

from dedupper.dedup import (
    apply_group_selections,
    find_duplicate_groups,
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
) -> dict:
    return {
        "record_id": record_id,
        "title": title,
        "year": year,
        "first_author": author,
        "doi": doi,
        "journal": journal,
        "raw_fields": {"extra": extra} if extra else {},
    }


def test_normalization_handles_doi_urls_html_entities_and_apostrophes() -> None:
    assert normalize_doi("https://doi.org/10.1000/ABC.1.") == "10.1000/abc.1"
    left = "Children’s language &amp; memory: a review"
    right = "Children's language and memory - a review"
    assert normalize_title(left) == normalize_title(right)


def test_exact_doi_can_group_records_with_different_titles() -> None:
    records = [
        record("a", "Online title", doi="https://doi.org/10.1000/X"),
        record("b", "Print title", doi="doi:10.1000/x", extra="more"),
        record("c", "Unrelated"),
    ]

    groups = find_duplicate_groups(records)

    assert len(groups) == 1
    assert set(groups[0]["record_ids"]) == {"a", "b"}
    assert "Exact DOI" in groups[0]["methods"]


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

    groups = find_duplicate_groups(records)

    assert len(groups) == 1
    assert groups[0]["potential_protocol_record_ids"] == ["protocol"]
    assert groups[0]["recommended_record_id"] == "study"


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

    groups = find_duplicate_groups(records)

    assert len(groups) == 1
    assert groups[0]["potential_protocol_record_ids"] == ["protocol"]
    assert groups[0]["recommended_record_id"] == "study"


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
