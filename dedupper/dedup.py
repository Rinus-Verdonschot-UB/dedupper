from __future__ import annotations

import html
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from typing import Any

from rapidfuzz.fuzz import ratio, token_set_ratio

from .doi import normalize_doi

RELATED_PUBLICATION_PREFIXES = (
    (
        "correction",
        re.compile(
            r"^\s*[\"'(\[]?(?:correction|erratum|corrigendum)"
            r"(?:\s+(?:to|for)\b|[\s]*[:\-–—])",
            re.IGNORECASE,
        ),
    ),
    (
        "commentary",
        re.compile(
            r"^\s*[\"'(\[]?(?:commentary(?:\s+(?:on|regarding|concerning)\b"
            r"|[\s]*[:\-–—])|comment\s+(?:on|regarding|concerning)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "response",
        re.compile(
            r"^\s*[\"'(\[]?(?:reply|response)(?:\s+to\b|[\s]*[:\-–—])",
            re.IGNORECASE,
        ),
    ),
    (
        "retraction",
        re.compile(
            r"^\s*[\"'(\[]?(?:retraction(?:\s+note)?)(?:\s+to\b|[\s]*[:\-–—])",
            re.IGNORECASE,
        ),
    ),
    (
        "expression of concern",
        re.compile(
            r"^\s*[\"'(\[]?expression\s+of\s+concern(?:\s*[:\-–—]|\s+for\b)",
            re.IGNORECASE,
        ),
    ),
)
RELATED_PUBLICATION_SUFFIX = re.compile(
    r"(?:[:.\-–—]\s*|[\[(]\s*)"
    r"(?P<kind>correction|erratum|corrigendum|commentary|"
    r"retraction(?:\s+note)?|expression\s+of\s+concern)"
    r"\s*[\])]?[\s\"']*$",
    re.IGNORECASE,
)
PROTOCOL_WORD = re.compile(r"\bprotocol\b", re.IGNORECASE)
CONFERENCE_ABSTRACT_PATTERN = re.compile(
    r"(?:\b(?:conference|congress|meeting|symposium)\s+abstracts?\b|"
    r"\babstracts?\s+(?:from|of|for)\s+(?:an?\s+)?"
    r"(?:conference|congress|meeting|symposium)\b)",
    re.IGNORECASE,
)
PUBLICATION_TYPE_KEYS = {
    "pt",
    "dt",
    "m3",
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
}
CONFERENCE_TITLE_THRESHOLD = 96.0
CONFERENCE_AUTHOR_THRESHOLD = 100.0
CONVENTIONAL_PROTOCOL_MARKER = re.compile(
    r"(?:"
    r"(?:[:.\-–—]\s*|[\[(]\s*)(?:a\s+|an\s+)?"
    r"(?:study\s+|trial\s+|research\s+)?protocol\b"
    r"|^\s*(?:a\s+|an\s+)?(?:study\s+|trial\s+|research\s+)?"
    r"protocol(?:\s+(?:for|of)\b|\s*[:\-–—])"
    r"|\b(?:study|trial|research)\s+protocol\b"
    r"|\bprotocol\s+(?:for|of)\b"
    r")",
    re.IGNORECASE,
)
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

def normalize_title(value: str) -> str:
    value = value or ""
    for _ in range(2):
        value = html.unescape(value)
    value = re.sub(r"&amp;?", " and ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    value = value.replace("’", "'").replace("‘", "'").replace("`", "'")
    value = re.sub(r"['\"]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def publication_kind(value: str) -> str:
    """Return a conservative title-level publication type.

    Related publications often repeat the complete title of the work they
    discuss or amend. Only markers in conventional prefix/suffix positions are
    interpreted, so an ordinary title containing words such as "correction" in
    its main text is not reclassified.
    """

    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKC", value).strip()
    for kind, pattern in RELATED_PUBLICATION_PREFIXES:
        if pattern.search(value):
            return kind
    suffix = RELATED_PUBLICATION_SUFFIX.search(value)
    if not suffix:
        return "article"
    kind = suffix.group("kind").casefold()
    if kind in {"erratum", "corrigendum"}:
        return "correction"
    if kind.startswith("retraction"):
        return "retraction"
    return re.sub(r"\s+", " ", kind)


def _author_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", value)


def _publication_type_values(record: dict[str, Any]) -> list[str]:
    values = [str(record.get("publication_type", "")).strip()]
    raw_fields = record.get("raw_fields", {})
    if isinstance(raw_fields, dict):
        for key, value in raw_fields.items():
            normalized_key = normalize_title(str(key))
            if normalized_key not in PUBLICATION_TYPE_KEYS:
                continue
            if isinstance(value, list):
                values.extend(str(item).strip() for item in value)
            else:
                values.append(str(value).strip())
    return [value for value in values if value]


def is_conference_abstract(record: dict[str, Any]) -> bool:
    """Recognize an explicit conference-abstract publication type.

    The title itself is only consulted for an explicit bracketed/suffixed type
    marker. Ordinary articles that merely discuss conference abstracts are not
    reclassified.
    """

    if any(
        CONFERENCE_ABSTRACT_PATTERN.search(value)
        for value in _publication_type_values(record)
    ):
        return True
    title = html.unescape(str(record.get("title", "")))
    return bool(
        re.search(
            r"(?:[:\-–—]\s*|[\[(]\s*)conference\s+abstract\s*[\])]?[\s.]*$",
            title,
            re.IGNORECASE,
        )
    )


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


def _has_protocol_marker(value: str) -> bool:
    """Recognize a likely publication-type marker, not every use of protocol."""

    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKC", value).strip()
    match = PROTOCOL_WORD.search(value)
    if not match:
        return False
    if CONVENTIONAL_PROTOCOL_MARKER.search(value):
        return True

    # Some exports omit the punctuation before an appended publication type.
    # Treat a late standalone occurrence as a marker, while avoiding titles
    # such as "Protocol adherence and treatment outcomes".
    words = list(re.finditer(r"\b[\w'-]+\b", value, re.UNICODE))
    protocol_index = next(
        (
            index
            for index, word in enumerate(words)
            if word.group(0).casefold() == "protocol"
        ),
        -1,
    )
    return protocol_index >= max(5, int(len(words) * 0.45))


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


def _normalized_metadata(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


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


def _evaluate_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    conference_context: dict[str, Any],
    fuzzy_threshold: float,
) -> dict[str, Any]:
    evidence = _pair_evidence(left, right)
    result = {**evidence, "decision": "none", "method": "", "reason": ""}
    if evidence["different_doi"]:
        return result
    if publication_kind(str(left.get("title", ""))) != publication_kind(
        str(right.get("title", ""))
    ):
        return result
    if _conference_sensitive_pair(left, right, conference_context):
        if _conference_records_match(left, right):
            result.update(
                decision="duplicate",
                method="Conference abstract title + first author",
                reason="The shared proceedings DOI is supported by a near-identical title and first author.",
            )
        return result

    year_distance = evidence["year_distance"]
    if year_distance is not None and year_distance > 1:
        if evidence["same_doi"]:
            result.update(
                decision="review",
                reason="The DOI is identical, but the publication years differ by more than one year.",
            )
        return result

    left_isbn = _normalized_metadata(left.get("isbn", ""))
    right_isbn = _normalized_metadata(right.get("isbn", ""))
    if left_isbn and right_isbn and left_isbn != right_isbn:
        return result

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
        if title_score and title_score < 55 and clear_author_conflict:
            result.update(
                decision="review",
                reason="The DOI is identical, but both title and first-author metadata conflict.",
            )
            return result
        both_titles_present = bool(
            normalize_title(str(left.get("title", "")))
            and normalize_title(str(right.get("title", "")))
        )
        clear_title_conflict = bool(
            both_titles_present and title_score < 55
        )
        identifier_support = any(
            (
                evidence["pages_match"],
                evidence["article_number_match"],
            )
        )
        if (
            title_score >= 88
            or evidence["full_authors_match"]
            or identifier_support
            or (
                evidence["first_author_match"]
                and moderate_support >= 1
                and not clear_title_conflict
            )
            or (title_score >= 75 and moderate_support >= 2)
        ):
            if not (clear_title_conflict and not (evidence["full_authors_match"] or identifier_support)):
                result.update(
                    decision="duplicate",
                    method="Corroborated DOI",
                    reason="The DOI is supported by compatible bibliographic metadata.",
                )
                return result
        result.update(
            decision="review",
            reason="The DOI is identical, but there is not enough supporting metadata for automatic removal.",
        )
        return result

    left_title = normalize_title(str(left.get("title", "")))
    right_title = normalize_title(str(right.get("title", "")))
    exact_title = bool(left_title and left_title == right_title)
    if exact_title:
        exact_title_support = sum(
            bool(value)
            for value in (
                year_distance is not None and year_distance <= 1,
                evidence["journal_match"],
            )
        )
        if strong_support or exact_title_support >= 1:
            if min(len(left_title), len(right_title)) >= 28 or strong_support or exact_title_support >= 2:
                result.update(
                    decision="duplicate",
                    method="Corroborated exact title",
                    reason="The cleaned title is identical and at least one independent field agrees.",
                )
                return result
        result.update(
            decision="review",
            reason="The cleaned title is identical, but no independent metadata corroborates it.",
        )
        return result

    if title_score < fuzzy_threshold:
        return result
    protocol_pair = _has_protocol_marker(str(left.get("title", ""))) != _has_protocol_marker(
        str(right.get("title", ""))
    )
    fuzzy_supported = bool(
        strong_support
        or (year_distance is not None and year_distance <= 1 and evidence["journal_match"])
        or (protocol_pair and (evidence["first_author_match"] or year_distance is not None))
    )
    if fuzzy_supported:
        result.update(
            decision="duplicate",
            method="Corroborated fuzzy title",
            reason="The title similarity is supported by compatible bibliographic metadata.",
        )
    return result


class UnionFind:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        ids = [record["record_id"] for record in records]
        self.parent = {item: item for item in ids}
        self.rank = {item: 0 for item in ids}
        self.metadata: dict[str, dict[str, set[Any]]] = {}
        for record in records:
            record_id = record["record_id"]
            year = str(record.get("year", ""))
            self.metadata[record_id] = {
                "dois": {value for value in (normalize_doi(record.get("doi", "")),) if value},
                "years": {int(year)} if year.isdigit() else set(),
                "kinds": {publication_kind(str(record.get("title", "")))},
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

    def has_component_conflict(self, left: str, right: str) -> bool:
        left_meta = self.metadata[self.find(left)]
        right_meta = self.metadata[self.find(right)]
        dois = left_meta["dois"] | right_meta["dois"]
        years = left_meta["years"] | right_meta["years"]
        kinds = left_meta["kinds"] | right_meta["kinds"]
        isbns = left_meta["isbns"] | right_meta["isbns"]
        return bool(
            len(dois) > 1
            or len(kinds) > 1
            or len(isbns) > 1
            or (years and max(years) - min(years) > 1)
        )

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


def _review_candidate(
    left: dict[str, Any], right: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    same_doi = evidence.get("same_doi", False)
    return {
        "candidate_id": uuid.uuid4().hex,
        "record_ids": [left["record_id"], right["record_id"]],
        "label": "DOI metadata conflict" if same_doi else "Uncorroborated title match",
        "reason": evidence.get("reason", "The metadata needs manual review."),
        "signals": evidence.get("signals", []),
        "doi": evidence.get("doi", ""),
    }


def analyze_duplicates(
    records: list[dict[str, Any]], fuzzy_threshold: float = 92.0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(records) < 2:
        return [], []
    ids = [record["record_id"] for record in records]
    by_id = {record["record_id"]: record for record in records}
    union_find = UnionFind(records)
    edges: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    conference_context = _conference_context(records)
    seen_pairs: set[tuple[str, str]] = set()

    def consider(left: dict[str, Any], right: dict[str, Any]) -> None:
        pair = tuple(sorted((left["record_id"], right["record_id"])))
        if pair in seen_pairs:
            return
        seen_pairs.add(pair)
        evidence = _evaluate_pair(left, right, conference_context, fuzzy_threshold)
        if evidence["decision"] == "review":
            reviews.append(_review_candidate(left, right, evidence))
            return
        if evidence["decision"] != "duplicate":
            return
        if union_find.has_component_conflict(left["record_id"], right["record_id"]):
            evidence["same_doi"] = bool(evidence.get("same_doi"))
            evidence["reason"] = (
                "This pair looks similar, but joining its existing groups would create "
                "a DOI, year, publication-type or ISBN conflict."
            )
            reviews.append(_review_candidate(left, right, evidence))
            return
        union_find.union(left["record_id"], right["record_id"])
        edges.append(
            {
                "left": left["record_id"],
                "right": right["record_id"],
                "method": evidence["method"],
                "value": evidence.get("doi", ""),
                "score": round(evidence["title_score"], 1),
                "signals": evidence["signals"],
                "reason": evidence["reason"],
            }
        )

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

    title_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        title = normalize_title(str(record.get("title", "")))
        if title:
            title_buckets[title].append(record)
    for bucket in title_buckets.values():
        known_dois = {
            normalize_doi(record.get("doi", ""))
            for record in bucket
            if normalize_doi(record.get("doi", ""))
        }
        # A DOI-less copy cannot be assigned safely when this exact title is
        # already attached to more than one known publication.
        if len(known_dois) > 1:
            continue
        for left_index, left in enumerate(bucket):
            for right in bucket[left_index + 1 :]:
                consider(left, right)

    title_by_id = {
        record["record_id"]: normalize_title(str(record.get("title", "")))
        for record in records
    }
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
        for other_id in candidates:
            if other_id == record_id:
                continue
            pair = tuple(sorted((record_id, other_id)))
            if pair in seen_pairs:
                continue
            other_title = title_by_id[other_id]
            if not other_title:
                continue
            if title == other_title:
                continue
            length_ratio = min(len(title), len(other_title)) / max(len(title), len(other_title))
            if length_ratio < 0.72:
                continue
            if _title_score(record, by_id[other_id]) < fuzzy_threshold:
                continue
            consider(record, by_id[other_id])

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
        potential_protocol_ids = _potential_protocol_record_ids(member_records, fuzzy_threshold)
        recommendation_candidates = [
            record for record in member_records if record["record_id"] not in potential_protocol_ids
        ]
        recommendation = choose_best_record(recommendation_candidates or member_records)
        groups.append(
            {
                "group_id": uuid.uuid4().hex,
                "record_ids": member_ids,
                "recommended_record_id": recommendation["record_id"],
                "potential_protocol_record_ids": potential_protocol_ids,
                "conference_abstract_record_ids": conference_abstract_ids,
                "edges": group_edges,
                "methods": sorted({edge["method"] for edge in group_edges}),
            }
        )
    groups.sort(
        key=lambda group: (
            -len(group["record_ids"]),
            normalize_title(str(by_id[group["record_ids"][0]].get("title", ""))),
        )
    )
    reviews.sort(key=lambda candidate: (candidate["label"], candidate["record_ids"]))
    return groups, reviews


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
