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


class UnionFind:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        ids = [record["record_id"] for record in records]
        self.parent = {item: item for item in ids}
        self.rank = {item: 0 for item in ids}
        self.dois = {
            record["record_id"]: {
                doi
                for doi in [normalize_doi(record.get("doi", ""))]
                if doi
            }
            for record in records
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

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        self.dois[left_root].update(self.dois[right_root])
        del self.dois[right_root]
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1

    def has_doi_conflict(self, left: str, right: str) -> bool:
        left_dois = self.dois[self.find(left)]
        right_dois = self.dois[self.find(right)]
        return bool(left_dois and right_dois and left_dois != right_dois)


def _add_exact_doi_edges(
    records: list[dict[str, Any]],
    union_find: UnionFind,
    edges: list[dict[str, Any]],
    conference_context: dict[str, Any],
) -> None:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = normalize_doi(record.get("doi", ""))
        if key:
            buckets[key].append(record)

    for doi, bucket in buckets.items():
        if len(bucket) < 2:
            continue
        is_shared_conference_doi = bool(
            doi in conference_context["shared_conference_dois"]
            or any(
                record["record_id"] in conference_context["conference_ids"]
                for record in bucket
            )
        )
        if is_shared_conference_doi:
            author_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for record in bucket:
                author_key = _author_key(record.get("first_author", ""))
                if len(author_key) >= 2:
                    author_buckets[author_key].append(record)
            pairs = (
                (author_bucket[left_index], author_bucket[right_index])
                for author_bucket in author_buckets.values()
                for left_index in range(len(author_bucket))
                for right_index in range(left_index + 1, len(author_bucket))
            )
        else:
            pairs = ((bucket[0], current) for current in bucket[1:])

        for left, right in pairs:
            if is_shared_conference_doi and not _conference_records_match(
                left, right
            ):
                continue
            union_find.union(left["record_id"], right["record_id"])
            edges.append(
                {
                    "left": left["record_id"],
                    "right": right["record_id"],
                    "method": (
                        "Conference abstract title + first author"
                        if is_shared_conference_doi
                        else "Exact DOI"
                    ),
                    "value": doi,
                    "score": 100.0,
                }
            )


def _title_evidence_is_compatible(
    left: dict[str, Any],
    right: dict[str, Any],
    conference_context: dict[str, Any],
) -> bool:
    left_doi = normalize_doi(left.get("doi", ""))
    right_doi = normalize_doi(right.get("doi", ""))
    if left_doi and right_doi and left_doi != right_doi:
        return False
    if _conference_sensitive_pair(left, right, conference_context):
        return _conference_records_match(left, right)
    return publication_kind(left.get("title", "")) == publication_kind(
        right.get("title", "")
    )


def _add_exact_title_edges(
    records: list[dict[str, Any]],
    union_find: UnionFind,
    edges: list[dict[str, Any]],
    conference_context: dict[str, Any],
) -> None:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = normalize_title(record.get("title", ""))
        if key:
            buckets[key].append(record)

    for key, bucket in buckets.items():
        # If the same title is already associated with multiple known DOIs,
        # title-only records cannot safely be assigned to either publication.
        known_dois = {
            normalize_doi(record.get("doi", ""))
            for record in bucket
            if normalize_doi(record.get("doi", ""))
        }
        if len(known_dois) > 1:
            continue
        for left_index, left in enumerate(bucket):
            for current in bucket[left_index + 1 :]:
                if not _title_evidence_is_compatible(
                    left, current, conference_context
                ):
                    continue
                if union_find.has_doi_conflict(
                    left["record_id"], current["record_id"]
                ):
                    continue
                union_find.union(left["record_id"], current["record_id"])
                edges.append(
                    {
                        "left": left["record_id"],
                        "right": current["record_id"],
                        "method": (
                            "Conference abstract title + first author"
                            if _conference_sensitive_pair(
                                left, current, conference_context
                            )
                            else "Exact cleaned title"
                        ),
                        "value": key,
                        "score": 100.0,
                    }
                )


def find_duplicate_groups(
    records: list[dict[str, Any]], fuzzy_threshold: float = 92.0
) -> list[dict[str, Any]]:
    if len(records) < 2:
        return []
    ids = [record["record_id"] for record in records]
    by_id = {record["record_id"]: record for record in records}
    union_find = UnionFind(records)
    edges: list[dict[str, Any]] = []
    conference_context = _conference_context(records)

    _add_exact_doi_edges(records, union_find, edges, conference_context)
    _add_exact_title_edges(
        records, union_find, edges, conference_context
    )

    title_by_id = {
        record["record_id"]: normalize_title(record.get("title", ""))
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
    document_frequency = Counter(
        token for tokens in token_sets.values() for token in tokens
    )
    token_index: dict[str, list[str]] = defaultdict(list)
    for record_id, tokens in token_sets.items():
        for token in tokens:
            token_index[token].append(record_id)

    seen_pairs: set[tuple[str, str]] = set()
    maximum_block_size = max(40, int(len(records) * 0.12))
    for record in records:
        record_id = record["record_id"]
        title = title_by_id[record_id]
        if len(title) < 12:
            continue
        rare_tokens = sorted(
            (
                token
                for token in token_sets[record_id]
                if document_frequency[token] <= maximum_block_size
            ),
            key=lambda token: (document_frequency[token], token),
        )[:6]
        candidates: set[str] = set()
        for token in rare_tokens:
            candidates.update(token_index[token])
        for other_id in candidates:
            if other_id == record_id:
                continue
            pair = tuple(sorted((record_id, other_id)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            other = by_id[other_id]
            other_title = title_by_id[other_id]
            if title == other_title:
                continue
            if not _title_evidence_is_compatible(
                record, other, conference_context
            ):
                continue
            year, other_year = record.get("year", ""), other.get("year", "")
            if year and other_year and year != other_year:
                continue
            length_ratio = min(len(title), len(other_title)) / max(
                len(title), len(other_title)
            )
            if length_ratio < 0.72:
                continue
            plain_score = ratio(title, other_title)
            token_score = token_set_ratio(title, other_title)
            score = max(plain_score, 0.55 * plain_score + 0.45 * token_score)
            if score < fuzzy_threshold:
                continue
            # Short, generic titles need one additional agreeing identifier.
            if min(len(title), len(other_title)) < 28:
                author_agrees = bool(
                    _author_key(record.get("first_author", ""))
                    and _author_key(record.get("first_author", ""))
                    == _author_key(other.get("first_author", ""))
                )
                year_agrees = bool(year and other_year and year == other_year)
                if not (author_agrees or year_agrees):
                    continue
            if union_find.has_doi_conflict(record_id, other_id):
                continue
            conference_match = _conference_sensitive_pair(
                record, other, conference_context
            )
            union_find.union(record_id, other_id)
            edges.append(
                {
                    "left": record_id,
                    "right": other_id,
                    "method": (
                        "Conference abstract title + first author"
                        if conference_match
                        else "Fuzzy cleaned title"
                    ),
                    "value": "",
                    "score": round(score, 1),
                }
            )

    components: dict[str, list[str]] = defaultdict(list)
    for record_id in ids:
        components[union_find.find(record_id)].append(record_id)

    groups: list[dict[str, Any]] = []
    for member_ids in components.values():
        if len(member_ids) < 2:
            continue
        member_set = set(member_ids)
        group_edges = [
            edge
            for edge in edges
            if edge["left"] in member_set and edge["right"] in member_set
        ]
        member_records = [by_id[item] for item in member_ids]
        conference_abstract_ids = [
            record_id
            for record_id in member_ids
            if record_id in conference_context["conference_ids"]
        ]
        potential_protocol_ids = _potential_protocol_record_ids(
            member_records,
            fuzzy_threshold,
        )
        recommendation_candidates = [
            record
            for record in member_records
            if record["record_id"] not in potential_protocol_ids
        ]
        recommendation = choose_best_record(
            recommendation_candidates or member_records
        )
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
            normalize_title(by_id[group["record_ids"][0]].get("title", "")),
        )
    )
    return groups


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
        record.get("doi", ""),
        record.get("journal", ""),
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
