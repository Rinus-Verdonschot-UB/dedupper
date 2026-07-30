from __future__ import annotations

import itertools
import re
import statistics
import unicodedata
from collections import Counter
from typing import Any

from rapidfuzz.fuzz import ratio

from .models import CANONICAL_FIELDS, SourceTable
from .parsers import DOI_RE, YEAR_RE


ALIASES = {
    "title": {
        "title",
        "article title",
        "document title",
        "record title",
        "publication title article",
        "titel",
        "titel artikel",
        "ti",
        "t1",
    },
    "year": {
        "year",
        "publication year",
        "publication date",
        "published",
        "date published",
        "pub year",
        "jaar",
        "publicatiejaar",
        "datum",
        "py",
        "y1",
    },
    "first_author": {
        "first author",
        "first authors last name",
        "author",
        "authors",
        "author names",
        "creator",
        "creators",
        "auteur",
        "auteurs",
        "eerste auteur",
        "au",
        "a1",
    },
    "doi": {
        "doi",
        "digital object identifier",
        "document object identifier",
        "doi link",
        "doi url",
    },
    "journal": {
        "journal",
        "journal name",
        "journal title",
        "source",
        "source title",
        "publication title",
        "publication name",
        "periodical",
        "container title",
        "tijdschrift",
        "tijdschriftnaam",
        "jo",
        "jf",
        "t2",
    },
    "abstract": {
        "abstract",
        "abstract text",
        "article abstract",
        "summary",
        "description",
        "samenvatting",
        "beschrijving",
        "ab",
        "n2",
    },
}


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _header_score(column: str, field: str) -> float:
    normalized = _normalize(re.sub(r"\s+\[\d+\]$", "", column))
    if normalized in ALIASES[field]:
        return 1.0
    if any(
        len(alias) >= 4
        and (normalized.startswith(alias + " ") or normalized.endswith(" " + alias))
        for alias in ALIASES[field]
    ):
        return 0.9
    best = max(ratio(normalized, alias) for alias in ALIASES[field]) / 100
    return best if best >= 0.72 else 0.0


def _values(table: SourceTable, column: str, limit: int = 400) -> list[str]:
    return [
        row.get(column, "").strip()
        for row in table.rows[:limit]
        if row.get(column, "").strip()
    ]


def _content_score(values: list[str], field: str) -> float:
    if not values:
        return 0.0
    sample = values[:400]
    total = len(sample)
    lengths = [len(value) for value in sample]
    word_counts = [len(re.findall(r"\w+", value, re.UNICODE)) for value in sample]
    unique_ratio = len(set(value.casefold() for value in sample)) / total
    median_len = statistics.median(lengths)
    median_words = statistics.median(word_counts)

    if field == "doi":
        matches = sum(bool(DOI_RE.search(value)) for value in sample) / total
        return matches
    if field == "year":
        matches = sum(
            bool(YEAR_RE.search(value))
            and len(re.sub(r"\D", "", value)) <= 8
            for value in sample
        ) / total
        return matches
    if field == "title":
        long_fraction = sum(len(value) >= 18 for value in sample) / total
        multiword_fraction = sum(words >= 4 for words in word_counts) / total
        doi_penalty = sum(bool(DOI_RE.search(value)) for value in sample) / total
        year_penalty = sum(
            bool(re.fullmatch(r"\D*(?:1[5-9]\d{2}|20\d{2}|21\d{2})\D*", value))
            for value in sample
        ) / total
        length_component = min(median_len / 55, 1.0)
        return max(
            0.0,
            0.25 * long_fraction
            + 0.25 * multiword_fraction
            + 0.25 * unique_ratio
            + 0.25 * length_component
            - 0.6 * doi_penalty
            - 0.6 * year_penalty,
        )
    if field == "first_author":
        plausible_words = sum(1 <= words <= 12 for words in word_counts) / total
        name_punctuation = sum(
            bool(re.search(r"[,;]|\b[A-Z]\.?(?:\s|$)", value)) for value in sample
        ) / total
        not_too_long = sum(len(value) <= 160 for value in sample) / total
        title_penalty = sum(len(value) > 180 for value in sample) / total
        return max(
            0.0,
            0.4 * plausible_words
            + 0.3 * name_punctuation
            + 0.2 * not_too_long
            + 0.1 * unique_ratio
            - 0.5 * title_penalty,
        )
    if field == "journal":
        plausible_words = sum(1 <= words <= 14 for words in word_counts) / total
        plausible_length = sum(3 <= len(value) <= 140 for value in sample) / total
        journal_terms = sum(
            bool(
                re.search(
                    r"\b(journal|review|proceedings|transactions|bulletin|"
                    r"tijdschrift|revue|zeitschrift|archives|annals)\b",
                    value,
                    re.IGNORECASE,
                )
            )
            for value in sample
        ) / total
        repeated = 1 - unique_ratio if total >= 4 else 0
        return min(
            1.0,
            0.35 * plausible_words
            + 0.25 * plausible_length
            + 0.3 * journal_terms
            + 0.1 * repeated,
        )
    if field == "abstract":
        long_fraction = sum(len(value) >= 120 for value in sample) / total
        many_words = sum(words >= 25 for words in word_counts) / total
        very_long_fraction = sum(len(value) >= 300 for value in sample) / total
        length_component = min(median_len / 500, 1.0)
        title_like_penalty = sum(
            len(value) < 160 and words < 20 for value, words in zip(sample, word_counts)
        ) / total
        return max(
            0.0,
            0.32 * long_fraction
            + 0.28 * many_words
            + 0.2 * very_long_fraction
            + 0.2 * length_component
            - 0.35 * title_like_penalty,
        )
    return 0.0


def _score_column(table: SourceTable, column: str, field: str) -> dict[str, Any]:
    values = _values(table, column)
    header = _header_score(column, field)
    content = _content_score(values, field)
    if header >= 0.9:
        score = 0.78 * header + 0.22 * content
    elif header:
        score = 0.58 * header + 0.42 * content
    else:
        # A gibberish header can still be proposed from the values, but it
        # remains explicitly lower-confidence until the user confirms it.
        score = 0.78 * content
    samples = values[:3]
    return {
        "column": column,
        "score": round(score, 4),
        "header_score": round(header, 4),
        "content_score": round(content, 4),
        "samples": samples,
    }


def infer_mapping(table: SourceTable) -> dict[str, dict[str, Any]]:
    ranked: dict[str, list[dict[str, Any]]] = {}
    for field in CANONICAL_FIELDS:
        candidates = sorted(
            (_score_column(table, column, field) for column in table.columns),
            key=lambda item: item["score"],
            reverse=True,
        )
        ranked[field] = candidates

    shortlists: dict[str, list[str | None]] = {}
    score_lookup: dict[tuple[str, str | None], float] = {}
    for field, candidates in ranked.items():
        selected = [item["column"] for item in candidates[:6]]
        shortlists[field] = [None, *selected]
        score_lookup[(field, None)] = 0.12
        for item in candidates:
            score_lookup[(field, item["column"])] = item["score"]

    best_assignment: dict[str, str | None] = {
        field: None for field in CANONICAL_FIELDS
    }
    best_total = -1.0
    for choices in itertools.product(
        *(shortlists[field] for field in CANONICAL_FIELDS)
    ):
        used = [choice for choice in choices if choice is not None]
        if len(used) != len(set(used)):
            continue
        total = sum(
            score_lookup[(field, choice)]
            for field, choice in zip(CANONICAL_FIELDS, choices)
        )
        if total > best_total:
            best_total = total
            best_assignment = dict(zip(CANONICAL_FIELDS, choices))

    result: dict[str, dict[str, Any]] = {}
    for field in CANONICAL_FIELDS:
        selected = best_assignment[field]
        candidates = ranked[field]
        selected_score = score_lookup[(field, selected)]
        alternatives = [
            candidate["score"]
            for candidate in candidates
            if candidate["column"] != selected
        ]
        gap = selected_score - (alternatives[0] if alternatives else 0)
        if selected is None or selected_score < 0.38:
            confidence = "low"
            selected = None
        elif selected_score >= 0.82 and gap >= 0.1:
            confidence = "high"
        elif selected_score >= 0.58:
            confidence = "medium"
        else:
            confidence = "low"

        selected_detail = next(
            (item for item in candidates if item["column"] == selected), None
        )
        if selected_detail and selected_detail["header_score"] >= 0.9:
            reason = "Header and values"
        elif selected_detail and selected_detail["header_score"]:
            reason = "Similar header and values"
        elif selected_detail:
            reason = "Values only—please verify"
        else:
            reason = "No reliable candidate"
        result[field] = {
            "selected": selected,
            "score": round(selected_score, 3) if selected else 0,
            "confidence": confidence,
            "reason": reason,
            "candidates": candidates,
        }
    return result


def column_profiles(table: SourceTable) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for column in table.columns:
        values = _values(table, column)
        result[column] = {
            "nonempty": len(values),
            "unique": len(set(value.casefold() for value in values)),
            "samples": values[:3],
        }
    return result
