from __future__ import annotations

import html
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from typing import Any

from rapidfuzz.fuzz import ratio, token_set_ratio

from .doi import normalize_doi
from .normalize import author_key as _author_key
from .normalize import normalize_title
from .normalize import normalized_metadata as _normalized_metadata
from .rules import DEFAULT_SETTINGS, RULES, rule_name
from .versions import (
    PROTOCOL_WORD,
    VERSION_LABELS,
    classify_version,
    has_protocol_marker as _has_protocol_marker,
    is_conference_abstract,
    publication_kind,
    publication_type_values as _publication_type_values,
    related_title_score,
    relationship_label,
    strip_version_markers,
    version_compatibility,
)

# Re-exported for backwards compatibility with v0.7.0 imports.
__all__ = [
    "analyze_duplicates",
    "analyze_records",
    "apply_group_selections",
    "apply_family_decisions",
    "apply_pair_decisions",
    "build_related_families",
    "choose_best_record",
    "conference_abstract_report",
    "enrich_retained_records",
    "find_duplicate_groups",
    "is_conference_abstract",
    "normalize_doi",
    "normalize_title",
    "publication_kind",
]

CONFERENCE_TITLE_THRESHOLD = DEFAULT_SETTINGS["conference_title_threshold"]
CONFERENCE_AUTHOR_THRESHOLD = DEFAULT_SETTINGS["conference_author_threshold"]
SEPARATED_PAIR_LIMIT = 5000

STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "de",
    "der",
    "des",
    "die",
    "een",
    "en",
    "for",
    "from",
    "het",
    "in",
    "la",
    "le",
    "of",
    "on",
    "the",
    "to",
    "van",
    "with",
}


def _conference_title_score(left: str, right: str) -> float:
    left_title = normalize_title(left)
    right_title = normalize_title(right)
    if not left_title or not right_title:
        return 0.0
    plain_score = ratio(left_title, right_title)
    token_score = token_set_ratio(left_title, right_title)
    return max(plain_score, 0.8 * plain_score + 0.2 * token_score)


def _conference_author_score(left: str, right: str) -> float:
    left_author = _author_key(left)
    right_author = _author_key(right)
    # A lone initial is not reliable surname evidence in conference exports.
    if len(left_author) < 2 or len(right_author) < 2:
        return 0.0
    return ratio(left_author, right_author)


def _conference_records_match(
    left: dict[str, Any], right: dict[str, Any]
) -> bool:
    """Require near-identity of both title and first-author surname."""

    left_title = normalize_title(str(left.get("title", "")))
    right_title = normalize_title(str(right.get("title", "")))
    left_numbers = re.findall(r"\b\d+(?:\.\d+)?\b", left_title)
    right_numbers = re.findall(r"\b\d+(?:\.\d+)?\b", right_title)
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return False
    if publication_kind(left.get("title", "")) != publication_kind(
        right.get("title", "")
    ):
        return False
    return bool(
        _conference_title_score(left_title, right_title)
        >= CONFERENCE_TITLE_THRESHOLD
        and _conference_author_score(
            str(left.get("first_author", "")),
            str(right.get("first_author", "")),
        )
        >= CONFERENCE_AUTHOR_THRESHOLD
    )


def _conference_context(records: list[dict[str, Any]]) -> dict[str, Any]:
    conference_ids = {
        record["record_id"]
        for record in records
        if is_conference_abstract(record)
    }
    source_doi_buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record in records:
        doi = normalize_doi(record.get("doi", ""))
        if doi:
            source_doi_buckets[(str(record.get("source_file_id", "")), doi)].append(
                record["record_id"]
            )

    protected_clusters = {
        key: member_ids
        for key, member_ids in source_doi_buckets.items()
        if len(set(member_ids) & conference_ids) >= 2
    }
    return {
        "conference_ids": conference_ids,
        "protected_clusters": protected_clusters,
        "shared_conference_dois": {
            doi for (_, doi) in protected_clusters
        },
    }


def _conference_sensitive_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    conference_context: dict[str, Any],
) -> bool:
    conference_ids = conference_context["conference_ids"]
    if (
        left["record_id"] in conference_ids
        or right["record_id"] in conference_ids
    ):
        return True
    shared_dois = conference_context["shared_conference_dois"]
    return bool(
        normalize_doi(left.get("doi", "")) in shared_dois
        or normalize_doi(right.get("doi", "")) in shared_dois
    )


def _protocol_title_score(protocol_title: str, other_title: str) -> float:
    left = normalize_title(protocol_title)
    right = normalize_title(other_title)
    if not left or not right:
        return 0.0
    plain_score = ratio(left, right)
    token_score = token_set_ratio(left, right)
    return max(plain_score, 0.55 * plain_score + 0.45 * token_score)


def _potential_protocol_record_ids(
    records: list[dict[str, Any]], fuzzy_threshold: float
) -> list[str]:
    """Flag protocol-like records only inside a plausible mixed publication group."""

    protocol_candidates = [
        record
        for record in records
        if _has_protocol_marker(record.get("title", ""))
    ]
    non_protocol_records = [
        record
        for record in records
        if not PROTOCOL_WORD.search(record.get("title", ""))
    ]
    if not protocol_candidates or not non_protocol_records:
        return []

    return [
        candidate["record_id"]
        for candidate in protocol_candidates
        if any(
            _protocol_title_score(
                candidate.get("title", ""),
                other.get("title", ""),
            )
            >= fuzzy_threshold
            for other in non_protocol_records
        )
    ]


def _title_score(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_title = normalize_title(str(left.get("title", "")))
    right_title = normalize_title(str(right.get("title", "")))
    if not left_title or not right_title:
        return 0.0
    plain = ratio(left_title, right_title)
    token = token_set_ratio(left_title, right_title)
    return max(plain, 0.55 * plain + 0.45 * token)


def _year_distance(left: dict[str, Any], right: dict[str, Any]) -> int | None:
    try:
        left_year = int(str(left.get("year", "")))
        right_year = int(str(right.get("year", "")))
    except ValueError:
        return None
    return abs(left_year - right_year)


def _author_entries(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .;")
    if not text:
        return []
    if re.search(r";|\||\s+and\s+", text, re.IGNORECASE):
        return [
            item.strip(" .")
            for item in re.split(r"\s*;\s*|\s*\|\s*|\s+and\s+", text, flags=re.I)
            if item.strip(" .")
        ]
    comma_parts = [item.strip(" .") for item in text.split(",") if item.strip(" .")]
    if len(comma_parts) > 1 and all(
        len(part.split()) >= 2 and re.search(r"\b[A-Z]{1,8}(?:\(\d+\))?$", part)
        for part in comma_parts
    ):
        return comma_parts
    return [text]


def _author_signature(value: str) -> tuple[str, str]:
    value = re.sub(r"\(\d[\d,\- ]*\)", "", value or "").strip(" .")
    if not value:
        return "", ""
    if "," in value:
        surname, given = value.split(",", 1)
        initials = "".join(re.findall(r"\b([A-Za-z])", given))
        return _author_key(surname), initials.casefold()
    tokens = value.split()
    if len(tokens) == 1:
        return _author_key(tokens[0]), ""
    if re.fullmatch(r"(?:[A-Z]\.?){1,8}", tokens[-1]):
        return _author_key(" ".join(tokens[:-1])), _author_key(tokens[-1])
    if re.fullmatch(r"(?:[A-Z]\.?){1,8}", tokens[0]):
        return _author_key(tokens[-1]), _author_key(tokens[0])
    return _author_key(tokens[-1]), "".join(token[0].casefold() for token in tokens[:-1])


def _full_authors_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_entries = _author_entries(left.get("authors", ""))
    right_entries = _author_entries(right.get("authors", ""))
    if len(left_entries) < 2 or len(right_entries) < 2:
        return False
    left_signatures = [_author_signature(value) for value in left_entries]
    right_signatures = [_author_signature(value) for value in right_entries]
    compared = min(len(left_signatures), len(right_signatures))
    matches = 0
    for (left_surname, left_initials), (right_surname, right_initials) in zip(
        left_signatures, right_signatures
    ):
        initials_compatible = (
            not left_initials
            or not right_initials
            or left_initials.startswith(right_initials)
            or right_initials.startswith(left_initials)
        )
        matches += bool(left_surname and left_surname == right_surname and initials_compatible)
    return compared > 0 and matches / compared >= 0.8


def _same_value(left: dict[str, Any], right: dict[str, Any], field: str) -> bool:
    if field == "pages":
        left_value = _page_start_key(left.get(field, ""))
        right_value = _page_start_key(right.get(field, ""))
    else:
        left_value = _normalized_metadata(left.get(field, ""))
        right_value = _normalized_metadata(right.get(field, ""))
    return bool(left_value and left_value == right_value)


def _page_start_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    match = re.search(r"(?<![a-z0-9])([a-z]?)[-\s]*(\d+[a-z]?)", text)
    return "".join(match.groups()) if match else ""


def _pair_evidence(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_doi = normalize_doi(left.get("doi", ""))
    right_doi = normalize_doi(right.get("doi", ""))
    score = _title_score(left, right)
    left_author = _author_key(str(left.get("first_author", "")))
    right_author = _author_key(str(right.get("first_author", "")))
    first_author_match = bool(left_author and left_author == right_author)
    full_authors_match = _full_authors_match(left, right)
    year_distance = _year_distance(left, right)
    journal_match = _same_value(left, right, "journal")
    publication_type_match = _same_value(left, right, "publication_type")
    issn_match = _same_value(left, right, "issn")
    isbn_match = _same_value(left, right, "isbn")
    pages_match = _same_value(left, right, "pages")
    article_number_match = _same_value(left, right, "article_number")
    signals: list[str] = []
    if left_doi and left_doi == right_doi:
        signals.append("Same DOI")
    if score:
        signals.append(f"title {score:.0f}%")
    if full_authors_match:
        signals.append("full author list match")
    elif first_author_match:
        signals.append("first author match")
    if year_distance == 0:
        signals.append("same year")
    elif year_distance == 1:
        signals.append("year +1")
    if issn_match:
        signals.append("ISSN match")
    elif journal_match:
        signals.append("journal match")
    if pages_match:
        signals.append("pages match")
    if article_number_match:
        signals.append("article number match")
    if isbn_match:
        signals.append("ISBN match")
    if publication_type_match:
        signals.append("publication type match")
    return {
        "same_doi": bool(left_doi and left_doi == right_doi),
        "different_doi": bool(left_doi and right_doi and left_doi != right_doi),
        "doi": left_doi if left_doi == right_doi else "",
        "title_score": score,
        "first_author_match": first_author_match,
        "full_authors_match": full_authors_match,
        "year_distance": year_distance,
        "journal_match": journal_match,
        "publication_type_match": publication_type_match,
        "issn_match": issn_match,
        "isbn_match": isbn_match,
        "pages_match": pages_match,
        "article_number_match": article_number_match,
        "signals": signals,
    }


COMPARED_FIELDS = (
    "doi",
    "title",
    "first_author",
    "authors",
    "year",
    "journal",
    "issn",
    "isbn",
    "pages",
    "article_number",
    "publication_type",
)
FIELD_LABELS = {
    "doi": "DOI",
    "title": "Title",
    "first_author": "First author",
    "authors": "Authors",
    "year": "Year",
    "journal": "Journal / source",
    "issn": "ISSN",
    "isbn": "ISBN",
    "pages": "Pages",
    "article_number": "Article number",
    "publication_type": "Publication type",
}


def compare_fields(
    left: dict[str, Any],
    right: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    settings: dict[str, float] | None = None,
) -> dict[str, dict[str, str]]:
    """Return a per-field status table explaining how two records compare.

    Status values: ``match``, ``compatible``, ``differs`` (a soft field that
    legitimately varies between databases), ``conflict`` (an identifier that
    should not vary), ``missing`` (only one record has a value) and ``absent``.
    """

    settings = settings or DEFAULT_SETTINGS
    evidence = evidence or _pair_evidence(left, right)
    result: dict[str, dict[str, str]] = {}

    def presence(field: str) -> tuple[bool, bool]:
        return bool(str(left.get(field, "") or "").strip()), bool(
            str(right.get(field, "") or "").strip()
        )

    for field in COMPARED_FIELDS:
        has_left, has_right = presence(field)
        if not has_left and not has_right:
            result[field] = {"status": "absent", "note": "neither record has a value"}
            continue
        if has_left != has_right:
            result[field] = {"status": "missing", "note": "only one record has a value"}
            continue
        status, note = "differs", ""
        if field == "doi":
            left_doi = normalize_doi(left.get("doi", ""))
            right_doi = normalize_doi(right.get("doi", ""))
            if left_doi and left_doi == right_doi:
                status, note = "match", "identical DOI"
            elif left_doi and right_doi:
                status, note = "conflict", "different DOIs"
            else:
                status, note = "differs", "a DOI value could not be normalized"
        elif field == "title":
            score = evidence["title_score"]
            if normalize_title(str(left.get("title", ""))) == normalize_title(
                str(right.get("title", ""))
            ):
                status, note = "match", "identical after cleaning"
            elif score >= settings["fuzzy_title_threshold"]:
                status, note = "compatible", f"{score:.0f}% similar"
            else:
                related = related_title_score(
                    str(left.get("title", "")), str(right.get("title", ""))
                )
                if related >= settings["related_title_threshold"]:
                    status, note = (
                        "compatible",
                        f"{score:.0f}% similar; {related:.0f}% after removing "
                        "version markers",
                    )
                else:
                    status, note = "differs", f"{score:.0f}% similar"
        elif field == "first_author":
            if evidence["first_author_match"]:
                status, note = "match", "same surname"
            else:
                status, note = "conflict", "different surnames"
        elif field == "authors":
            if evidence["full_authors_match"]:
                status, note = "match", "≥ 80% of listed authors agree"
            elif (
                len(_author_entries(left.get("authors", ""))) >= 2
                and len(_author_entries(right.get("authors", ""))) >= 2
            ):
                status, note = "differs", "author lists do not line up"
            else:
                status, note = "differs", "too few separable authors to compare"
        elif field == "year":
            distance = evidence["year_distance"]
            if distance == 0:
                status, note = "match", "same year"
            elif distance == 1:
                status, note = "compatible", "one year apart (e.g. online-first)"
            elif distance is None:
                status, note = "differs", "year not numeric"
            else:
                status, note = "conflict", f"{distance} years apart"
        elif field in {"journal", "publication_type"}:
            if _same_value(left, right, field):
                status, note = "match", "identical"
            elif (
                token_set_ratio(
                    normalize_title(str(left.get(field, ""))),
                    normalize_title(str(right.get(field, ""))),
                )
                >= 90
            ):
                status, note = "compatible", "near-identical wording"
            else:
                status, note = "differs", "different wording"
        elif field == "isbn":
            if evidence["isbn_match"]:
                status, note = "match", "identical"
            else:
                status, note = "conflict", "different ISBNs"
        else:
            if _same_value(left, right, field):
                status, note = "match", "identical" if field != "pages" else "same start page"
            else:
                status, note = "differs", "different values"
        result[field] = {"status": status, "note": note}
    return result


def _decision(rule_id: str, decision: str, reason: str) -> dict[str, str]:
    return {
        "decision": decision,
        "rule": rule_id,
        "method": rule_name(rule_id) if decision == "duplicate" else "",
        "rule_name": rule_name(rule_id),
        "reason": reason,
    }


def _evaluate_standard(
    left: dict[str, Any],
    right: dict[str, Any],
    evidence: dict[str, Any],
    conference_context: dict[str, Any],
    settings: dict[str, float],
) -> dict[str, str]:
    """The v0.7.0 matching rules, now returning an explicit rule identifier."""

    fuzzy_threshold = settings["fuzzy_title_threshold"]
    if evidence["different_doi"]:
        return _decision(
            "S1",
            "none",
            "Both records have valid but different DOIs, which identify different "
            "publications.",
        )
    if _conference_sensitive_pair(left, right, conference_context):
        if _conference_records_match(left, right):
            return _decision(
                "D4",
                "duplicate",
                "Conference abstracts: near-identical title and the same first-author "
                "surname. A shared proceedings DOI was not used as evidence.",
            )
        return _decision(
            "S5",
            "none",
            "Conference abstract(s): the title or first author is not near-identical, "
            "so a shared proceedings DOI or similar title is not enough.",
        )

    year_distance = evidence["year_distance"]
    if year_distance is not None and year_distance > settings["max_year_difference"]:
        if evidence["same_doi"]:
            return _decision(
                "R1",
                "review",
                f"The DOI is identical, but the publication years differ by "
                f"{year_distance} years.",
            )
        return _decision(
            "S2",
            "none",
            f"The publication years differ by {year_distance} years.",
        )

    left_isbn = _normalized_metadata(left.get("isbn", ""))
    right_isbn = _normalized_metadata(right.get("isbn", ""))
    if left_isbn and right_isbn and left_isbn != right_isbn:
        return _decision("S3", "none", "The records carry different ISBNs.")

    strong_support = any(
        (
            evidence["first_author_match"],
            evidence["full_authors_match"],
            evidence["issn_match"],
            evidence["isbn_match"],
            evidence["pages_match"],
            evidence["article_number_match"],
        )
    )
    moderate_support = sum(
        bool(value)
        for value in (
            year_distance is not None and year_distance <= 1,
            evidence["journal_match"],
            evidence["publication_type_match"],
        )
    )
    title_score = evidence["title_score"]

    if evidence["same_doi"]:
        left_author = _author_key(str(left.get("first_author", "")))
        right_author = _author_key(str(right.get("first_author", "")))
        clear_author_conflict = bool(
            left_author and right_author and not evidence["first_author_match"]
        )
        if (
            title_score
            and title_score < settings["doi_title_conflict"]
            and clear_author_conflict
        ):
            return _decision(
                "R1",
                "review",
                "The DOI is identical, but both title and first-author metadata conflict.",
            )
        both_titles_present = bool(
            normalize_title(str(left.get("title", "")))
            and normalize_title(str(right.get("title", "")))
        )
        clear_title_conflict = bool(
            both_titles_present and title_score < settings["doi_title_conflict"]
        )
        identifier_support = any(
            (evidence["pages_match"], evidence["article_number_match"])
        )
        if (
            title_score >= settings["doi_title_confirm"]
            or evidence["full_authors_match"]
            or identifier_support
            or (
                evidence["first_author_match"]
                and moderate_support >= 1
                and not clear_title_conflict
            )
            or (title_score >= 75 and moderate_support >= 2)
        ):
            if not (
                clear_title_conflict
                and not (evidence["full_authors_match"] or identifier_support)
            ):
                return _decision(
                    "D1",
                    "duplicate",
                    "The DOI is identical and is supported by compatible bibliographic "
                    "metadata.",
                )
        return _decision(
            "R1",
            "review",
            "The DOI is identical, but there is not enough supporting metadata for "
            "automatic removal.",
        )

    left_title = normalize_title(str(left.get("title", "")))
    right_title = normalize_title(str(right.get("title", "")))
    if left_title and left_title == right_title:
        exact_title_support = sum(
            bool(value)
            for value in (
                year_distance is not None and year_distance <= 1,
                evidence["journal_match"],
            )
        )
        if strong_support or exact_title_support >= 1:
            if (
                min(len(left_title), len(right_title)) >= 28
                or strong_support
                or exact_title_support >= 2
            ):
                return _decision(
                    "D2",
                    "duplicate",
                    "The cleaned title is identical and at least one independent field "
                    "agrees.",
                )
        return _decision(
            "R2",
            "review",
            "The cleaned title is identical, but no independent metadata corroborates it.",
        )

    if title_score < fuzzy_threshold:
        return _decision(
            "S4",
            "none",
            f"Title similarity {title_score:.0f}% is below the {fuzzy_threshold:.0f}% "
            "threshold.",
        )
    protocol_pair = _has_protocol_marker(str(left.get("title", ""))) != _has_protocol_marker(
        str(right.get("title", ""))
    )
    fuzzy_supported = bool(
        strong_support
        or (year_distance is not None and year_distance <= 1 and evidence["journal_match"])
        or (protocol_pair and (evidence["first_author_match"] or year_distance is not None))
    )
    if fuzzy_supported:
        return _decision(
            "D3",
            "duplicate",
            f"The titles are {title_score:.0f}% similar and supported by compatible "
            "bibliographic metadata.",
        )
    return _decision(
        "S4",
        "none",
        f"The titles are {title_score:.0f}% similar, but no author, identifier or "
        "year-and-journal evidence supports a duplicate.",
    )


def _relationship_kind(record: dict[str, Any], version: dict[str, Any]) -> str:
    """Treat an untyped record published in a journal as the journal version."""

    if version["kind"] == "unspecified" and (
        str(record.get("journal", "")).strip() or normalize_doi(record.get("doi", ""))
    ):
        return "article"
    return version["kind"]


def _related_decision(
    left: dict[str, Any],
    right: dict[str, Any],
    left_version: dict[str, Any],
    right_version: dict[str, Any],
    evidence: dict[str, Any],
    settings: dict[str, float],
    rule_id: str,
    preface: str = "",
) -> dict[str, Any]:
    score = related_title_score(str(left.get("title", "")), str(right.get("title", "")))
    left_author = _author_key(str(left.get("first_author", "")))
    right_author = _author_key(str(right.get("first_author", "")))
    author_conflict = bool(
        left_author and right_author and not evidence["first_author_match"]
    )
    author_support = bool(evidence["first_author_match"] or evidence["full_authors_match"])
    threshold = settings[
        "related_title_threshold_author_conflict"
        if author_conflict
        else "related_title_threshold"
    ]
    relationship = relationship_label(
        _relationship_kind(left, left_version), _relationship_kind(right, right_version)
    )
    if score < threshold:
        result = _decision(
            "S6",
            "none",
            f"{preface}Different publication versions ({left_version['label']} vs "
            f"{right_version['label'].lower()}); titles are {score:.0f}% similar after "
            f"removing version markers, below the {threshold:.0f}% threshold.",
        )
        result["related_score"] = round(score, 1)
        return result
    author_text = (
        "same first author"
        if author_support
        else "first authors differ"
        if author_conflict
        else "first author not available for both"
    )
    result = _decision(
        rule_id,
        "related",
        f"{preface}{relationship}: titles agree at {score:.0f}% after removing version "
        f"markers ({author_text}). They are different publications, so both are kept "
        "unless you choose otherwise.",
    )
    result.update(
        relationship=relationship,
        related_score=round(score, 1),
        confidence="likely" if author_support else "possible",
    )
    return result


def _evaluate_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    conference_context: dict[str, Any],
    fuzzy_threshold: float | dict[str, float],
) -> dict[str, Any]:
    settings = (
        dict(fuzzy_threshold)
        if isinstance(fuzzy_threshold, dict)
        else {**DEFAULT_SETTINGS, "fuzzy_title_threshold": float(fuzzy_threshold)}
    )
    versions = conference_context.get("versions") or {}
    left_version = versions.get(left["record_id"]) or classify_version(left)
    right_version = versions.get(right["record_id"]) or classify_version(right)
    evidence = _pair_evidence(left, right)
    compatibility = version_compatibility(left_version, right_version)
    result: dict[str, Any] = {
        **evidence,
        "compatibility": compatibility,
        "versions": [left_version["kind"], right_version["kind"]],
    }

    if compatibility == "distinct":
        result.update(
            _related_decision(
                left, right, left_version, right_version, evidence, settings, "V1"
            )
        )
        return result

    standard = _evaluate_standard(left, right, evidence, conference_context, settings)
    result.update(standard)
    if compatibility != "type_known_once":
        return result

    special = left_version if left_version["kind"] != "unspecified" else right_version
    if standard["decision"] == "duplicate":
        venue_confirms = bool(
            evidence["same_doi"] or evidence["issn_match"] or evidence["journal_match"]
        )
        if venue_confirms:
            result["reason"] += (
                f" Only one copy states its publication type ({special['label'].lower()}); "
                "the DOI or journal confirms both describe the same item."
            )
            result["caution"] = "Publication type stated for only one copy"
            return result
        result.update(
            _related_decision(
                left,
                right,
                left_version,
                right_version,
                evidence,
                settings,
                "V2",
                preface=(
                    f"Only one copy is labelled {special['label'].lower()} and neither "
                    "the DOI nor the journal confirms it is the same item. "
                ),
            )
        )
        if result["decision"] != "related":
            # The standard rules matched; do not let the pair disappear.
            result.update(
                _decision(
                    "V2",
                    "related",
                    f"Only one copy is labelled {special['label'].lower()}; the records "
                    "match on other fields, so they are linked for your review.",
                ),
                relationship=relationship_label(
                    _relationship_kind(left, left_version),
                    _relationship_kind(right, right_version),
                ),
                confidence="possible",
            )
        return result
    if standard["decision"] == "none" and standard["rule"] in {"S1", "S2", "S4", "S5"}:
        related = _related_decision(
            left, right, left_version, right_version, evidence, settings, "V2"
        )
        if related["decision"] == "related":
            result.update(related)
    return result


class UnionFind:
    def __init__(
        self,
        records: list[dict[str, Any]],
        versions: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        versions = versions or {}
        ids = [record["record_id"] for record in records]
        self.parent = {item: item for item in ids}
        self.rank = {item: 0 for item in ids}
        self.metadata: dict[str, dict[str, set[Any]]] = {}
        for record in records:
            record_id = record["record_id"]
            year = str(record.get("year", ""))
            version = versions.get(record_id) or classify_version(record)
            self.metadata[record_id] = {
                "dois": {value for value in (normalize_doi(record.get("doi", "")),) if value},
                "years": {int(year)} if year.isdigit() else set(),
                # An unstated type is compatible with any version.
                "kinds": set() if version["kind"] == "unspecified" else {version["kind"]},
                "isbns": {value for value in (_normalized_metadata(record.get("isbn", "")),) if value},
            }

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            parent = self.parent[item]
            self.parent[item] = root
            item = parent
        return root

    def component_conflict(self, left: str, right: str) -> str:
        left_meta = self.metadata[self.find(left)]
        right_meta = self.metadata[self.find(right)]
        dois = left_meta["dois"] | right_meta["dois"]
        years = left_meta["years"] | right_meta["years"]
        kinds = left_meta["kinds"] | right_meta["kinds"]
        isbns = left_meta["isbns"] | right_meta["isbns"]
        if len(dois) > 1:
            return "two different DOIs (" + ", ".join(sorted(dois)) + ")"
        if len(kinds) > 1:
            return "different publication versions (" + ", ".join(
                VERSION_LABELS.get(kind, kind).lower() for kind in sorted(kinds)
            ) + ")"
        if len(isbns) > 1:
            return "two different ISBNs"
        if years and max(years) - min(years) > 1:
            return f"publication years {min(years)}–{max(years)}"
        return ""

    def has_component_conflict(self, left: str, right: str) -> bool:
        return bool(self.component_conflict(left, right))

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        for field, values in self.metadata[right_root].items():
            self.metadata[left_root][field].update(values)
        del self.metadata[right_root]
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


REVIEW_LABELS = {
    "R1": "DOI metadata conflict",
    "R2": "Uncorroborated title match",
    "R3": "Group conflict",
    "R4": "Title shared by several DOIs",
}


def _review_candidate(
    record_ids: list[str], evidence: dict[str, Any]
) -> dict[str, Any]:
    rule_id = evidence.get("rule") or ("R1" if evidence.get("same_doi") else "R2")
    return {
        "candidate_id": uuid.uuid4().hex,
        "record_ids": list(record_ids),
        "rule": rule_id,
        "label": REVIEW_LABELS.get(rule_id, rule_name(rule_id)),
        "reason": evidence.get("reason", "The metadata needs manual review."),
        "signals": evidence.get("signals", []),
        "doi": evidence.get("doi", ""),
        "comparison": evidence.get("comparison", {}),
        "title_score": round(evidence.get("title_score", 0.0), 1),
    }


def _information_summary(record: dict[str, Any]) -> dict[str, Any]:
    has_doi, core, raw, title_length = record_information_score(record)
    return {
        "has_doi": bool(has_doi),
        "core_fields": core,
        "raw_fields": raw,
        "title_length": title_length,
    }


def _recommendation_reason(
    recommended: dict[str, Any],
    members: list[dict[str, Any]],
    protocol_ids: list[str],
) -> str:
    summary = _information_summary(recommended)
    parts = []
    if summary["has_doi"]:
        parts.append("it has a valid DOI")
    parts.append(f"{summary['core_fields']} of 12 bibliographic fields are filled")
    parts.append(f"{summary['raw_fields']} non-empty source fields")
    others = [
        _information_summary(member)
        for member in members
        if member["record_id"] != recommended["record_id"]
    ]
    tie = any(
        (item["has_doi"], item["core_fields"], item["raw_fields"], item["title_length"])
        == (
            summary["has_doi"],
            summary["core_fields"],
            summary["raw_fields"],
            summary["title_length"],
        )
        for item in others
    )
    text = "Recommended because " + ", ".join(parts) + " (the most informative copy)."
    if protocol_ids:
        text += " Records flagged as a potential protocol were not considered."
    if tie:
        text += " Another copy is equally complete; the first one in upload order was chosen."
    return text


def analyze_records(
    records: list[dict[str, Any]],
    fuzzy_threshold: float | None = None,
    settings: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Run the full analysis and return every decision with its explanation.

    Keys: ``groups``, ``review_candidates``, ``related_links``,
    ``separated_pairs``, ``versions`` and ``settings``.
    """

    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    if fuzzy_threshold is not None:
        settings["fuzzy_title_threshold"] = float(fuzzy_threshold)
    fuzzy = settings["fuzzy_title_threshold"]
    versions = {record["record_id"]: classify_version(record) for record in records}
    empty = {
        "groups": [],
        "review_candidates": [],
        "related_links": [],
        "related_families": [],
        "separated_pairs": [],
        "separated_pairs_total": 0,
        "versions": versions,
        "settings": settings,
    }
    if len(records) < 2:
        return empty
    ids = [record["record_id"] for record in records]
    by_id = {record["record_id"]: record for record in records}
    union_find = UnionFind(records, versions)
    edges: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    related_links: list[dict[str, Any]] = []
    separated: list[dict[str, Any]] = []
    separated_total = 0
    conference_context = _conference_context(records)
    conference_context["versions"] = versions
    seen_pairs: set[tuple[str, str]] = set()

    def consider(left: dict[str, Any], right: dict[str, Any]) -> None:
        nonlocal separated_total
        pair = tuple(sorted((left["record_id"], right["record_id"])))
        if pair in seen_pairs:
            return
        seen_pairs.add(pair)
        evidence = _evaluate_pair(left, right, conference_context, settings)
        evidence["comparison"] = compare_fields(left, right, evidence, settings)
        decision = evidence["decision"]
        record_ids = [left["record_id"], right["record_id"]]
        if decision == "review":
            reviews.append(_review_candidate(record_ids, evidence))
            return
        if decision == "related":
            related_links.append(
                {
                    "link_id": uuid.uuid4().hex,
                    "record_ids": record_ids,
                    "rule": evidence["rule"],
                    "relationship": evidence.get("relationship", ""),
                    "confidence": evidence.get("confidence", "possible"),
                    "reason": evidence["reason"],
                    "signals": evidence["signals"],
                    "title_score": round(evidence["title_score"], 1),
                    "related_score": evidence.get("related_score"),
                    "versions": evidence["versions"],
                    "comparison": evidence["comparison"],
                }
            )
            return
        if decision != "duplicate":
            separated_total += 1
            if len(separated) < SEPARATED_PAIR_LIMIT:
                separated.append(
                    {
                        "record_ids": record_ids,
                        "rule": evidence["rule"],
                        "rule_name": evidence.get("rule_name", ""),
                        "reason": evidence["reason"],
                        "signals": evidence["signals"],
                        "title_score": round(evidence["title_score"], 1),
                        "comparison": evidence["comparison"],
                    }
                )
            return
        conflict = union_find.component_conflict(left["record_id"], right["record_id"])
        if conflict:
            evidence["rule"] = "R3"
            evidence["reason"] = (
                f"This pair matched under {evidence['rule_name']}, but joining its existing "
                f"groups would combine {conflict}. It is kept separate for "
                "your review."
            )
            reviews.append(_review_candidate(record_ids, evidence))
            return
        union_find.union(left["record_id"], right["record_id"])
        edges.append(
            {
                "left": left["record_id"],
                "right": right["record_id"],
                "rule": evidence["rule"],
                "method": evidence["method"],
                "value": evidence.get("doi", ""),
                "score": round(evidence["title_score"], 1),
                "signals": evidence["signals"],
                "reason": evidence["reason"],
                "caution": evidence.get("caution", ""),
                "comparison": evidence["comparison"],
            }
        )

    # 1. Records sharing a DOI.
    doi_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        doi = normalize_doi(record.get("doi", ""))
        if doi:
            doi_buckets[doi].append(record)
    for doi, bucket in doi_buckets.items():
        if len(bucket) < 2:
            continue
        conference_sensitive = bool(
            doi in conference_context["shared_conference_dois"]
            or any(item["record_id"] in conference_context["conference_ids"] for item in bucket)
        )
        for left_index, left in enumerate(bucket):
            for right in bucket[left_index + 1 :]:
                if conference_sensitive and not _conference_records_match(left, right):
                    continue
                consider(left, right)

    # 2. Records sharing an identical cleaned title.
    title_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        title = normalize_title(str(record.get("title", "")))
        if title:
            title_buckets[title].append(record)
    for bucket in title_buckets.values():
        if len(bucket) < 2:
            continue
        known_dois = sorted(
            {
                normalize_doi(record.get("doi", ""))
                for record in bucket
                if normalize_doi(record.get("doi", ""))
            }
        )
        if len(known_dois) > 1:
            # A DOI-less copy cannot be assigned safely when this exact title is
            # attached to more than one known publication. Records that do have
            # DOIs are still compared, so related versions remain visible.
            with_doi = [item for item in bucket if normalize_doi(item.get("doi", ""))]
            without_doi = [item for item in bucket if not normalize_doi(item.get("doi", ""))]
            for left_index, left in enumerate(with_doi):
                for right in with_doi[left_index + 1 :]:
                    consider(left, right)
            for orphan in without_doi:
                reviews.append(
                    _review_candidate(
                        [orphan["record_id"]] + [item["record_id"] for item in with_doi],
                        {
                            "rule": "R4",
                            "reason": (
                                "This record has no DOI and its title is identical to "
                                f"records with {len(known_dois)} different DOIs "
                                f"({', '.join(known_dois)}). It cannot be assigned to "
                                "one of them automatically."
                            ),
                            "signals": ["identical title", f"{len(known_dois)} DOIs"],
                            "title_score": 100.0,
                        },
                    )
                )
            continue
        for left_index, left in enumerate(bucket):
            for right in bucket[left_index + 1 :]:
                consider(left, right)

    # 3. Fuzzy titles, blocked on rare title words.
    title_by_id = {
        record["record_id"]: normalize_title(str(record.get("title", "")))
        for record in records
    }
    stripped_by_id: dict[str, str] = {}
    token_sets = {
        record_id: {
            token
            for token in title.split()
            if len(token) >= 3 and token not in STOPWORDS
        }
        for record_id, title in title_by_id.items()
    }
    document_frequency = Counter(token for tokens in token_sets.values() for token in tokens)
    token_index: dict[str, list[str]] = defaultdict(list)
    for record_id, tokens in token_sets.items():
        for token in tokens:
            token_index[token].append(record_id)
    maximum_block_size = max(40, int(len(records) * 0.12))
    related_threshold = settings["related_title_threshold"]

    def stripped(record_id: str) -> str:
        if record_id not in stripped_by_id:
            stripped_by_id[record_id] = strip_version_markers(
                str(by_id[record_id].get("title", ""))
            )
        return stripped_by_id[record_id]

    def related_candidate(record_id: str, other_id: str) -> bool:
        if version_compatibility(versions[record_id], versions[other_id]) == "same":
            return False
        left_stripped, right_stripped = stripped(record_id), stripped(other_id)
        if not left_stripped or not right_stripped:
            return False
        if min(len(left_stripped), len(right_stripped)) / max(
            len(left_stripped), len(right_stripped)
        ) < 0.6:
            return False
        return (
            related_title_score(
                str(by_id[record_id].get("title", "")),
                str(by_id[other_id].get("title", "")),
            )
            >= related_threshold
        )

    for record in records:
        record_id = record["record_id"]
        title = title_by_id[record_id]
        if len(title) < 12:
            continue
        rare_tokens = sorted(
            (token for token in token_sets[record_id] if document_frequency[token] <= maximum_block_size),
            key=lambda token: (document_frequency[token], token),
        )[:6]
        candidates = {candidate for token in rare_tokens for candidate in token_index[token]}
        for other_id in sorted(candidates):
            if other_id == record_id:
                continue
            pair = tuple(sorted((record_id, other_id)))
            if pair in seen_pairs:
                continue
            other_title = title_by_id[other_id]
            if not other_title or title == other_title:
                continue
            length_ratio = min(len(title), len(other_title)) / max(len(title), len(other_title))
            if length_ratio >= 0.72 and _title_score(record, by_id[other_id]) >= fuzzy:
                consider(record, by_id[other_id])
            elif related_candidate(record_id, other_id):
                consider(record, by_id[other_id])

    # Build duplicate groups from the union-find components.
    components: dict[str, list[str]] = defaultdict(list)
    for record_id in ids:
        components[union_find.find(record_id)].append(record_id)
    groups: list[dict[str, Any]] = []
    for member_ids in components.values():
        if len(member_ids) < 2:
            continue
        member_set = set(member_ids)
        group_edges = [
            edge for edge in edges if edge["left"] in member_set and edge["right"] in member_set
        ]
        member_records = [by_id[item] for item in member_ids]
        conference_abstract_ids = [
            record_id for record_id in member_ids if record_id in conference_context["conference_ids"]
        ]
        potential_protocol_ids = _potential_protocol_record_ids(member_records, fuzzy)
        recommendation_candidates = [
            record for record in member_records if record["record_id"] not in potential_protocol_ids
        ]
        recommendation = choose_best_record(recommendation_candidates or member_records)
        cautions = sorted({edge["caution"] for edge in group_edges if edge.get("caution")})
        if potential_protocol_ids:
            cautions.append("Contains a potential protocol (weak title marker)")
        groups.append(
            {
                "group_id": uuid.uuid4().hex,
                "record_ids": member_ids,
                "recommended_record_id": recommendation["record_id"],
                "recommendation_reason": _recommendation_reason(
                    recommendation, member_records, potential_protocol_ids
                ),
                "information": {
                    member["record_id"]: _information_summary(member)
                    for member in member_records
                },
                "potential_protocol_record_ids": potential_protocol_ids,
                "conference_abstract_record_ids": conference_abstract_ids,
                "edges": group_edges,
                "methods": sorted({edge["method"] for edge in group_edges}),
                "rules": sorted({edge["rule"] for edge in group_edges}),
                "cautions": cautions,
                "needs_attention": bool(
                    cautions
                    or {"D3", "D4"} & {edge["rule"] for edge in group_edges}
                ),
            }
        )
    groups.sort(
        key=lambda group: (
            -len(group["record_ids"]),
            normalize_title(str(by_id[group["record_ids"][0]].get("title", ""))),
        )
    )
    reviews.sort(key=lambda candidate: (candidate["rule"], candidate["label"], candidate["record_ids"]))
    related_links.sort(
        key=lambda link: (
            link["relationship"],
            normalize_title(str(by_id[link["record_ids"][0]].get("title", ""))),
        )
    )
    separated.sort(key=lambda item: (item["rule"], -item["title_score"]))
    families = build_related_families(records, groups, related_links, versions)
    return {
        "groups": groups,
        "review_candidates": reviews,
        "related_links": related_links,
        "related_families": families,
        "separated_pairs": separated,
        "separated_pairs_total": separated_total,
        "versions": versions,
        "settings": settings,
    }


def _unit_version(member_ids: list[str], versions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    kinds = [versions[record_id] for record_id in member_ids if record_id in versions]
    for preferred in kinds:
        if preferred["kind"] not in {"unspecified", "article"}:
            return preferred
    for preferred in kinds:
        if preferred["kind"] == "article":
            return preferred
    return kinds[0] if kinds else {"kind": "unspecified", "label": VERSION_LABELS["unspecified"], "evidence": []}


def build_related_families(
    records: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    related_links: list[dict[str, Any]],
    versions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cluster pairwise related links into publication families.

    A *unit* is either a duplicate group (all copies of one publication) or a
    single record. Units connected by related links form one family, e.g. a
    protocol, a conference abstract, a preprint and the journal article (with
    its duplicate copies) of the same study.
    """

    unit_of: dict[str, str] = {}
    units: dict[str, dict[str, Any]] = {}
    for number, group in enumerate(groups, start=1):
        unit_id = f"g{group['group_id']}"
        units[unit_id] = {
            "unit_id": unit_id,
            "record_ids": list(group["record_ids"]),
            "group_id": group["group_id"],
            "group_number": number,
        }
        for record_id in group["record_ids"]:
            unit_of[record_id] = unit_id
    for record in records:
        record_id = record["record_id"]
        if record_id not in unit_of:
            unit_id = f"r{record_id}"
            units[unit_id] = {
                "unit_id": unit_id,
                "record_ids": [record_id],
                "group_id": None,
                "group_number": None,
            }
            unit_of[record_id] = unit_id

    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    best_links: dict[tuple[str, str], dict[str, Any]] = {}
    for link in related_links:
        left_unit, right_unit = (unit_of[record_id] for record_id in link["record_ids"])
        if left_unit == right_unit:
            continue
        key = tuple(sorted((left_unit, right_unit)))
        current = best_links.get(key)
        if current is None or (link.get("related_score") or 0) > (current.get("related_score") or 0):
            best_links[key] = link
        parent[find(left_unit)] = find(right_unit)

    members: dict[str, list[str]] = defaultdict(list)
    for unit_id in units:
        if unit_id in parent:
            members[find(unit_id)].append(unit_id)
    order = {record["record_id"]: index for index, record in enumerate(records)}
    families: list[dict[str, Any]] = []
    for unit_ids in members.values():
        if len(unit_ids) < 2:
            continue
        unit_set = set(unit_ids)
        family_units = []
        for unit_id in sorted(unit_ids, key=lambda item: order[units[item]["record_ids"][0]]):
            unit = dict(units[unit_id])
            version = _unit_version(unit["record_ids"], versions)
            unit["kind"] = version["kind"]
            unit["label"] = version["label"]
            unit["evidence"] = version.get("evidence", [])
            family_units.append(unit)
        links = [
            {**link, "units": list(key)}
            for key, link in best_links.items()
            if key[0] in unit_set and key[1] in unit_set
        ]
        families.append(
            {
                "family_id": uuid.uuid4().hex,
                "units": family_units,
                "links": links,
                "link_ids": [link["link_id"] for link in links],
                "relationships": sorted({link["relationship"] for link in links}),
                "rules": sorted({link["rule"] for link in links}),
                "confidence": (
                    "likely" if all(link["confidence"] == "likely" for link in links) else "possible"
                ),
            }
        )
    families.sort(key=lambda family: order[family["units"][0]["record_ids"][0]])
    return families


def apply_family_decisions(
    kept: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    families: list[dict[str, Any]],
    decisions: dict[str, list[str]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    """Exclude chosen versions (units) of related-publication families.

    ``decisions`` maps a family id to the unit ids the user chose to exclude.
    Nothing is excluded by default, and at least one version must remain.
    """

    decisions = decisions or {}
    applied: dict[str, list[str]] = {}
    remove_ids: set[str] = set()
    for number, family in enumerate(families, start=1):
        chosen = [unit_id for unit_id in decisions.get(family["family_id"], []) if unit_id]
        if not chosen:
            continue
        valid = {unit["unit_id"]: unit for unit in family["units"]}
        if not set(chosen) <= set(valid):
            raise ValueError(f"Invalid version choice for related publication family {number}.")
        if set(chosen) == set(valid):
            raise ValueError(
                f"Related publication family {number}: keep at least one version."
            )
        applied[family["family_id"]] = chosen
        for unit_id in chosen:
            remove_ids.update(valid[unit_id]["record_ids"])
    still_kept = [record for record in kept if record["record_id"] not in remove_ids]
    newly_removed = [record for record in kept if record["record_id"] in remove_ids]
    return still_kept, removed + newly_removed, applied


def analyze_duplicates(
    records: list[dict[str, Any]], fuzzy_threshold: float = 92.0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    analysis = analyze_records(records, fuzzy_threshold)
    return analysis["groups"], analysis["review_candidates"]


def apply_pair_decisions(
    kept: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    items: list[dict[str, Any]],
    decisions: dict[str, str] | None,
    id_key: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """Apply "keep only one" choices made for review candidates or related links.

    ``decisions`` maps an item id to ``"all"`` (default: keep every record) or
    to the record id to keep; the item's other records are then removed.
    """

    decisions = decisions or {}
    applied: dict[str, str] = {}
    remove_ids: set[str] = set()
    for item in items:
        item_id = item[id_key]
        choice = decisions.get(item_id, "all") or "all"
        if choice == "all":
            continue
        if choice not in item["record_ids"]:
            raise ValueError(f"Invalid record choice for review item {item_id}.")
        applied[item_id] = choice
        remove_ids.update(record_id for record_id in item["record_ids"] if record_id != choice)
    # A record explicitly chosen to keep in one item wins over a removal elsewhere.
    remove_ids -= set(applied.values())
    still_kept = [record for record in kept if record["record_id"] not in remove_ids]
    newly_removed = [record for record in kept if record["record_id"] in remove_ids]
    return still_kept, removed + newly_removed, applied


def find_duplicate_groups(
    records: list[dict[str, Any]], fuzzy_threshold: float = 92.0
) -> list[dict[str, Any]]:
    return analyze_duplicates(records, fuzzy_threshold)[0]


def conference_abstract_report(
    records: list[dict[str, Any]], groups: list[dict[str, Any]]
) -> dict[str, Any]:
    """Summarize explicit conference abstracts and protected DOI containers."""

    context = _conference_context(records)
    conference_ids: set[str] = context["conference_ids"]
    by_id = {record["record_id"]: record for record in records}
    protected_record_ids: set[str] = set()
    clusters: list[dict[str, Any]] = []
    for (source_file_id, doi), member_ids in sorted(
        context["protected_clusters"].items()
    ):
        conference_member_ids = [
            record_id for record_id in member_ids if record_id in conference_ids
        ]
        protected_record_ids.update(conference_member_ids)
        first = by_id[member_ids[0]]
        clusters.append(
            {
                "source_file_id": source_file_id,
                "source_filename": first.get("source_filename", ""),
                "doi": doi,
                "conference_abstract_records": len(conference_member_ids),
                "total_records": len(member_ids),
            }
        )

    conference_groups = [
        group
        for group in groups
        if set(group["record_ids"]) & conference_ids
    ]
    matched_record_ids = {
        record_id
        for group in conference_groups
        for record_id in group["record_ids"]
        if record_id in conference_ids
    }
    cross_file_groups = 0
    for group in conference_groups:
        source_ids = {
            str(by_id[record_id].get("source_file_id", ""))
            for record_id in group["record_ids"]
        }
        if len(source_ids) > 1:
            cross_file_groups += 1

    return {
        "identified_records": len(conference_ids),
        "protected_records": len(protected_record_ids),
        "same_source_shared_doi_clusters": len(clusters),
        "overlapping_dois": len({cluster["doi"] for cluster in clusters}),
        "duplicate_groups": len(conference_groups),
        "cross_file_duplicate_groups": cross_file_groups,
        "matched_records": len(matched_record_ids),
        "clusters": clusters,
    }


def record_information_score(record: dict[str, Any]) -> tuple[int, int, int, int]:
    canonical = [
        record.get("title", ""),
        record.get("year", ""),
        record.get("first_author", ""),
        record.get("authors", ""),
        record.get("doi", ""),
        record.get("journal", ""),
        record.get("pages", ""),
        record.get("article_number", ""),
        record.get("issn", ""),
        record.get("isbn", ""),
        record.get("abstract", ""),
        record.get("publication_type", ""),
    ]
    raw_fields = record.get("raw_fields", {})
    raw_nonempty = sum(
        bool(value)
        for value in raw_fields.values()
        if not isinstance(value, list)
    ) + sum(
        bool(item)
        for value in raw_fields.values()
        if isinstance(value, list)
        for item in value
    )
    return (
        1 if normalize_doi(record.get("doi", "")) else 0,
        sum(bool(value) for value in canonical),
        raw_nonempty,
        len(record.get("title", "")),
    )


def choose_best_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    return max(records, key=record_information_score)


def apply_group_selections(
    records: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    selections: dict[str, str | list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    selections = selections or {}
    remove_ids: set[str] = set()
    final_selections: dict[str, list[str]] = {}
    for group in groups:
        group_id = group["group_id"]
        valid_ids = set(group["record_ids"])
        requested = selections.get(group_id)
        if requested is None:
            selected_ids = {group["recommended_record_id"]}
        elif isinstance(requested, str):
            selected_ids = {requested}
        else:
            selected_ids = set(requested)
        if not selected_ids:
            raise ValueError(
                f"Choose at least one record to keep from duplicate group {group_id}."
            )
        if not selected_ids.issubset(valid_ids):
            raise ValueError(f"Invalid record selection for duplicate group {group_id}.")
        final_selections[group_id] = [
            record_id
            for record_id in group["record_ids"]
            if record_id in selected_ids
        ]
        remove_ids.update(valid_ids - selected_ids)
    kept = [record for record in records if record["record_id"] not in remove_ids]
    removed = [record for record in records if record["record_id"] in remove_ids]
    return kept, removed, final_selections


ENRICHABLE_FIELDS = (
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
    "abstract",
    "publication_type",
)


def _field_key(field: str, value: Any) -> str:
    if field == "doi":
        return normalize_doi(value)
    if field == "title":
        return normalize_title(str(value or ""))
    if field == "pages":
        return _page_start_key(value)
    return _normalized_metadata(value)


def _source_reference(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": record.get("record_id", ""),
        "source_file_id": record.get("source_file_id", ""),
        "source_filename": record.get("source_filename", ""),
        "source_sheet": record.get("source_sheet"),
        "source_row": record.get("source_row"),
    }


def enrich_retained_records(
    kept: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    selections: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fill safe gaps in retained records and return a field-level audit.

    A populated field is not overwritten except for a clearly compatible,
    longer title. Missing fields are filled only when duplicate members provide
    one unambiguous normalized value. Disagreements remain visible in the audit.
    """

    by_id = {record["record_id"]: record for record in all_records}
    group_by_selected: dict[str, dict[str, Any]] = {}
    for group in groups:
        for selected_id in selections.get(group["group_id"], []):
            group_by_selected[selected_id] = group

    enriched_records: list[dict[str, Any]] = []
    audit_entries: list[dict[str, Any]] = []
    for original in kept:
        record = dict(original)
        group = group_by_selected.get(record["record_id"])
        if not group:
            enriched_records.append(record)
            continue
        members = [by_id[record_id] for record_id in group["record_ids"]]
        record["source_provenance"] = [_source_reference(member) for member in members]
        protocol_ids = set(group.get("potential_protocol_record_ids", []))
        if protocol_ids:
            selected_is_protocol = record["record_id"] in protocol_ids
            enrichment_members = [
                member
                for member in members
                if (member["record_id"] in protocol_ids) == selected_is_protocol
            ]
        else:
            enrichment_members = members
        field_audit: list[dict[str, Any]] = []

        for field in ENRICHABLE_FIELDS:
            current = record.get(field, "")
            candidates = [
                (member, member.get(field, ""))
                for member in enrichment_members
                if member.get(field, "") not in (None, "")
            ]
            if not candidates:
                continue
            keys: dict[str, list[tuple[dict[str, Any], Any]]] = defaultdict(list)
            for member, value in candidates:
                key = _field_key(field, value)
                if key:
                    keys[key].append((member, value))

            if field == "title" and current:
                compatible = [
                    (member, value)
                    for member, value in candidates
                    if _protocol_title_score(str(current), str(value)) >= 96
                    and publication_kind(str(current)) == publication_kind(str(value))
                ]
                if compatible:
                    source, longest = max(compatible, key=lambda item: len(str(item[1])))
                    if len(str(longest)) > len(str(current)):
                        record[field] = longest
                        field_audit.append(
                            {
                                "field": field,
                                "action": "used longer compatible title",
                                "from_record_id": source["record_id"],
                                "previous_value": current,
                                "value": longest,
                            }
                        )
                continue

            if current:
                continue
            if len(keys) == 1:
                source, value = max(next(iter(keys.values())), key=lambda item: len(str(item[1])))
                record[field] = value
                field_audit.append(
                    {
                        "field": field,
                        "action": "filled missing field",
                        "from_record_id": source["record_id"],
                        "value": value,
                    }
                )
            elif len(keys) > 1:
                field_audit.append(
                    {
                        "field": field,
                        "action": "left missing because duplicates disagree",
                        "candidate_values": [
                            values[0][1] for values in keys.values()
                        ],
                    }
                )

        record["enrichment_audit"] = field_audit
        audit_entry = {
            "retained_record_id": record["record_id"],
            "group_id": group["group_id"],
            "source_provenance": record["source_provenance"],
            "fields": field_audit,
        }
        audit_entries.append(audit_entry)
        enriched_records.append(record)
    return enriched_records, audit_entries
