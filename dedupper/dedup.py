from __future__ import annotations

import html
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from typing import Any

from rapidfuzz.fuzz import ratio, token_set_ratio


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
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


def normalize_doi(value: str) -> str:
    value = html.unescape(value or "").strip().casefold()
    value = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi\s*:\s*", "", value)
    match = DOI_PATTERN.search(value)
    return match.group(0).rstrip(".,;:)]}") if match else ""


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


def _add_exact_edges(
    records: list[dict[str, Any]],
    key_function: Any,
    label: str,
    union_find: UnionFind,
    edges: list[dict[str, Any]],
) -> None:
    buckets: dict[str, list[str]] = defaultdict(list)
    for record in records:
        key = key_function(record)
        if key:
            buckets[key].append(record["record_id"])
    for key, ids in buckets.items():
        for current in ids[1:]:
            union_find.union(ids[0], current)
            edges.append(
                {
                    "left": ids[0],
                    "right": current,
                    "method": label,
                    "value": key,
                    "score": 100.0,
                }
            )


def _title_evidence_is_compatible(
    left: dict[str, Any], right: dict[str, Any]
) -> bool:
    left_doi = normalize_doi(left.get("doi", ""))
    right_doi = normalize_doi(right.get("doi", ""))
    if left_doi and right_doi and left_doi != right_doi:
        return False
    return publication_kind(left.get("title", "")) == publication_kind(
        right.get("title", "")
    )


def _add_exact_title_edges(
    records: list[dict[str, Any]],
    union_find: UnionFind,
    edges: list[dict[str, Any]],
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
        anchor = bucket[0]
        for current in bucket[1:]:
            if not _title_evidence_is_compatible(anchor, current):
                continue
            if union_find.has_doi_conflict(
                anchor["record_id"], current["record_id"]
            ):
                continue
            union_find.union(anchor["record_id"], current["record_id"])
            edges.append(
                {
                    "left": anchor["record_id"],
                    "right": current["record_id"],
                    "method": "Exact cleaned title",
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

    _add_exact_edges(
        records,
        lambda record: normalize_doi(record.get("doi", "")),
        "Exact DOI",
        union_find,
        edges,
    )
    _add_exact_title_edges(records, union_find, edges)

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
            if not _title_evidence_is_compatible(record, other):
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
            union_find.union(record_id, other_id)
            edges.append(
                {
                    "left": record_id,
                    "right": other_id,
                    "method": "Fuzzy cleaned title",
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


def record_information_score(record: dict[str, Any]) -> tuple[int, int, int, int]:
    canonical = [
        record.get("title", ""),
        record.get("year", ""),
        record.get("first_author", ""),
        record.get("doi", ""),
        record.get("journal", ""),
        record.get("abstract", ""),
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
