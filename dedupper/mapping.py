from __future__ import annotations

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
        "eerste auteur",
        "first author surname",
        "first author last name",
    },
    "authors": {
        "author",
        "authors",
        "author names",
        "all authors",
        "creator",
        "creators",
        "auteur",
        "auteurs",
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
    "pages": {
        "pages",
        "page",
        "page range",
        "start page",
        "pagination",
        "pagina",
        "paginas",
        "sp",
        "ep",
        "pg",
    },
    "article_number": {
        "article number",
        "article no",
        "electronic article number",
        "elocation id",
        "elocationid",
        "artikelnummer",
        "c7",
        "ar",
    },
    "issn": {
        "issn",
        "eissn",
        "electronic issn",
        "print issn",
        "sn",
    },
    "isbn": {
        "isbn",
        "eisbn",
        "electronic isbn",
        "book isbn",
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
    "publication_type": {
        "publication type",
        "publication types",
        "document type",
        "document types",
        "article type",
        "article types",
        "record type",
        "source type",
        "publication category",
        "document category",
        "publicatietype",
        "publicatie type",
        "documenttype",
        "document type nederlands",
        "pt",
        "dt",
        "m3",
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
    if field in {"first_author", "authors"}:
        plausible_words = sum(1 <= words <= 12 for words in word_counts) / total
        name_punctuation = sum(
            bool(re.search(r"[,;]|\b[A-Z]\.?(?:\s|$)", value)) for value in sample
        ) / total
        max_length = 160 if field == "first_author" else 1200
        not_too_long = sum(len(value) <= max_length for value in sample) / total
        penalty_length = 180 if field == "first_author" else 1200
        title_penalty = sum(len(value) > penalty_length for value in sample) / total
        multi_author_bonus = (
            sum(bool(re.search(r";|\band\b|,[^,]+,", value, re.I)) for value in sample)
            / total
            if field == "authors"
            else 0
        )
        return max(
            0.0,
            0.4 * plausible_words
            + 0.3 * name_punctuation
            + 0.2 * not_too_long
            + 0.1 * unique_ratio
            + 0.2 * multi_author_bonus
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
    if field == "publication_type":
        type_terms = sum(
            bool(
                re.search(
                    r"\b(?:conference abstract|meeting abstract|journal article|"
                    r"research article|review|systematic review|meta-analysis|"
                    r"editorial|letter|conference paper|proceedings paper|"
                    r"book chapter|dissertation|thesis|preprint)\b",
                    value,
                    re.IGNORECASE,
                )
            )
            for value in sample
        ) / total
        short_values = sum(len(value) <= 100 for value in sample) / total
        modest_words = sum(1 <= words <= 12 for words in word_counts) / total
        return min(1.0, 0.72 * type_terms + 0.14 * short_values + 0.14 * modest_words)
    if field == "pages":
        matches = sum(
            bool(re.fullmatch(r"\s*[A-Za-z]?\d+[A-Za-z]?(?:\s*[-–—]\s*[A-Za-z]?\d+[A-Za-z]?)?\s*", value))
            for value in sample
        ) / total
        return matches
    if field == "article_number":
        matches = sum(
            bool(re.fullmatch(r"\s*(?:e\d{3,}|[A-Za-z]\d{5,}|\d{5,})\s*", value, re.I))
            for value in sample
        ) / total
        return matches
    if field == "issn":
        return sum(
            bool(re.fullmatch(r"\s*\d{4}[- ]?\d{3}[\dXx]\s*", value))
            for value in sample
        ) / total
    if field == "isbn":
        return sum(
            len(re.sub(r"[-\s]", "", value)) in {10, 13}
            and bool(re.fullmatch(r"[\dXx\-\s]+", value))
            for value in sample
        ) / total
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

    # A deterministic greedy assignment scales safely as canonical metadata is
    # expanded. High-confidence header matches claim a column first; weaker
    # content-only suggestions are then assigned from the remaining columns.
    best_assignment: dict[str, str | None] = {field: None for field in CANONICAL_FIELDS}
    available = set(table.columns)
    proposals = sorted(
        (
            candidate["score"],
            candidate["header_score"],
            field,
            candidate["column"],
        )
        for field, candidates in ranked.items()
        for candidate in candidates[:4]
        if candidate["score"] >= 0.38
    )
    for score, header_score, field, column in reversed(proposals):
        if best_assignment[field] is not None or column not in available:
            continue
        best_assignment[field] = column
        available.remove(column)

    # A complete-author column is also a valid source for the derived first
    # author. This is the one intentional shared mapping in the review form.
    if best_assignment["authors"] and best_assignment["first_author"] is None:
        best_assignment["first_author"] = best_assignment["authors"]

    score_lookup: dict[tuple[str, str | None], float] = {
        (field, None): 0.0 for field in CANONICAL_FIELDS
    }
    for field, candidates in ranked.items():
        for candidate in candidates:
            score_lookup[(field, candidate["column"])] = candidate["score"]

    result: dict[str, dict[str, Any]] = {}
    for field in CANONICAL_FIELDS:
        selected = best_assignment[field]
        candidates = ranked[field]
        selected_score = score_lookup.get((field, selected), 0.0)
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
