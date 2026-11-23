"""Publication-version detection.

A preprint, a conference abstract, a protocol, a correction notice and the
final journal article can all describe the same study, yet they are different
publications. Systematic-review teams must see them side by side rather than
having one silently removed as a "duplicate".

:func:`classify_version` labels each record and returns the evidence that led
to the label, so the label itself is transparent.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

from rapidfuzz.fuzz import ratio, token_set_ratio

from .doi import normalize_doi
from .normalize import normalize_title

# ---------------------------------------------------------------------------
# Title-level related-publication markers (unchanged from v0.7.0)
# ---------------------------------------------------------------------------

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
CONFERENCE_ABSTRACT_PATTERN = re.compile(
    r"(?:\b(?:conference|congress|meeting|symposium)\s+abstracts?\b|"
    r"\babstracts?\s+(?:from|of|for)\s+(?:an?\s+)?"
    r"(?:conference|congress|meeting|symposium)\b)",
    re.IGNORECASE,
)
CONFERENCE_ABSTRACT_TITLE = re.compile(
    r"(?:[:\-–—]\s*|[\[(]\s*)conference\s+abstract\s*[\])]?[\s.]*$",
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

# ---------------------------------------------------------------------------
# Version vocabularies
# ---------------------------------------------------------------------------

VERSION_LABELS = {
    "article": "Journal article",
    "unspecified": "Type not stated",
    "preprint": "Preprint",
    "conference_abstract": "Conference abstract",
    "conference_paper": "Conference paper / proceedings",
    "protocol": "Protocol",
    "registry": "Trial registry record",
    "thesis": "Thesis / dissertation",
    "correction": "Correction / erratum notice",
    "retraction": "Retraction notice",
    "commentary": "Commentary",
    "response": "Reply / response",
    "expression of concern": "Expression of concern",
}
# Kinds that never form a "standard publication" family with each other.
STANDARD_KINDS = {"article", "unspecified"}
NOTICE_KINDS = {"correction", "retraction", "commentary", "response", "expression of concern"}

# DOI prefixes that identify preprint servers. 10.1101 is shared by bioRxiv /
# medRxiv and Cold Spring Harbor Laboratory Press journals, so only the
# server-style numeric/date suffixes count.
PREPRINT_DOI_PATTERNS = (
    (re.compile(r"^10\.1101/(?:\d{4}\.\d{2}\.\d{2}\.)?\d{5,}(?:v\d+)?$"), "bioRxiv / medRxiv"),
    (re.compile(r"^10\.48550/arxiv\."), "arXiv"),
    (re.compile(r"^10\.2139/ssrn\."), "SSRN"),
    (re.compile(r"^10\.21203/rs\."), "Research Square"),
    (re.compile(r"^10\.20944/preprints"), "Preprints.org"),
    (re.compile(r"^10\.(?:31234|31219|31235|31222|31236|35542)/osf\.io/"), "OSF Preprints (PsyArXiv, SocArXiv, EdArXiv …)"),
    (re.compile(r"^10\.26434/chemrxiv"), "ChemRxiv"),
    (re.compile(r"^10\.22541/au\."), "Authorea"),
    (re.compile(r"^10\.1590/scielopreprints"), "SciELO Preprints"),
    (re.compile(r"^10\.32388/"), "Qeios"),
    (re.compile(r"^10\.36227/techrxiv"), "TechRxiv"),
    (re.compile(r"^10\.31223/"), "EarthArXiv"),
    (re.compile(r"^10\.2196/preprints\."), "JMIR Preprints"),
)
PREPRINT_VENUE = re.compile(
    r"\b(?:bio|med|chem|tech|engr|agri|sport)rxiv\b|\b(?:psy|soc|ed|earth)?arxiv\b"
    r"|\bssrn\b|\bresearch\s*square\b|\bpreprints?\b|\bauthorea\b|\bqeios\b"
    r"|\bosf\s+preprints\b",
    re.IGNORECASE,
)
PREPRINT_TITLE = re.compile(r"[\[(]\s*pre-?print\s*[\])]", re.IGNORECASE)
REGISTRY_VENUE = re.compile(
    r"clinicaltrials\.gov|\bictrp\b|trialsearch\.who|clinical\s*trials?\s+regist(?:er|ry)"
    r"|\beuctr\b|clinicaltrialsregister\.eu|\bisrctn\b|\banzctr\b|\bchictr\b|\bdrks\b"
    r"|\bctri\b|\bjprn\b|\bumin-?ctr\b|\birct\b|\bpactr\b|trialregister\.nl"
    r"|netherlands\s+trial\s+register|\bctis\b",
    re.IGNORECASE,
)
REGISTRY_URL = re.compile(
    r"clinicaltrials\.gov/(?:show|ct2|study)|trialsearch\.who\.int"
    r"|clinicaltrialsregister\.eu|isrctn\.com|anzctr\.org\.au|chictr\.org\.cn"
    r"|trialregister\.nl|drks\.de",
    re.IGNORECASE,
)
URL_KEYS = {"ur", "l1", "l2", "l3", "url", "link", "links", "source url"}


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


def publication_type_values(record: dict[str, Any]) -> list[str]:
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
    seen: list[str] = []
    for value in values:
        for part in re.split(r"\s*[;|\n]\s*", value):
            if part and part not in seen:
                seen.append(part)
    return seen


def is_conference_abstract(record: dict[str, Any]) -> bool:
    """Recognize an explicit conference-abstract publication type."""

    if any(
        CONFERENCE_ABSTRACT_PATTERN.search(value)
        for value in publication_type_values(record)
    ):
        return True
    title = html.unescape(str(record.get("title", "")))
    return bool(CONFERENCE_ABSTRACT_TITLE.search(title))


def has_protocol_marker(value: str) -> bool:
    """Recognize a likely publication-type marker, not every use of protocol."""

    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKC", value).strip()
    if not PROTOCOL_WORD.search(value):
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


def has_strict_protocol_marker(value: str) -> bool:
    value = unicodedata.normalize("NFKC", html.unescape(value or "")).strip()
    return bool(CONVENTIONAL_PROTOCOL_MARKER.search(value))


def _raw_values(record: dict[str, Any], keys: set[str]) -> list[str]:
    raw_fields = record.get("raw_fields", {})
    values: list[str] = []
    if not isinstance(raw_fields, dict):
        return values
    for key, value in raw_fields.items():
        if normalize_title(str(key)) not in keys:
            continue
        if isinstance(value, list):
            values.extend(str(item) for item in value if str(item).strip())
        elif str(value).strip():
            values.append(str(value))
    return values


def preprint_doi_server(doi: str) -> str:
    normalized = normalize_doi(doi)
    for pattern, server in PREPRINT_DOI_PATTERNS:
        if normalized and pattern.search(normalized):
            return server
    return ""


_TYPE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("correction", re.compile(r"\b(?:published\s+)?erratum\b|\bcorrigendum\b|\bcorrection(?:\s+notice)?\b", re.I)),
    ("retraction", re.compile(r"\bretraction(?:\s+of\s+publication|\s+notice)?\b", re.I)),
    ("expression of concern", re.compile(r"\bexpression\s+of\s+concern\b", re.I)),
    ("commentary", re.compile(r"^\s*comment(?:ary)?\s*$", re.I)),
    ("preprint", re.compile(r"\bpre-?prints?\b|\bposted\s+content\b", re.I)),
    ("registry", re.compile(r"\btrial\s+regist(?:ry|ration)\b|\bregistry\s+record\b", re.I)),
    ("protocol", re.compile(r"\bprotocol\b", re.I)),
    ("conference_abstract", CONFERENCE_ABSTRACT_PATTERN),
    ("conference_paper", re.compile(r"\bconference\s+(?:paper|proceedings?|review)\b|\bproceedings?\s+paper\b|\bproceedings\b", re.I)),
    ("thesis", re.compile(r"\bthesis\b|\bdissertation\b", re.I)),
    ("article", re.compile(
        r"\bjournal\s+article\b|^\s*article\s*$|\bresearch\s+article\b|\boriginal\s+(?:article|research)\b"
        r"|^\s*review(?:\s+article)?\s*$|\bsystematic\s+review\b|\bmeta-analysis\b"
        r"|\brandomi[sz]ed\s+controlled\s+trial\b|^\s*clinical\s+trial\b|\bobservational\s+study\b"
        r"|\bcase\s+reports?\b|\barticle\s+in\s+press\b",
        re.I,
    )),
)
_KIND_PRIORITY = [
    "correction",
    "retraction",
    "expression of concern",
    "commentary",
    "response",
    "preprint",
    "registry",
    "protocol",
    "conference_abstract",
    "conference_paper",
    "thesis",
    "article",
]


def classify_version(record: dict[str, Any]) -> dict[str, Any]:
    """Label the publication version of one record and explain why.

    Returns ``{"kind", "label", "source", "evidence": [...]}`` where *source*
    is the strongest kind of evidence: ``title``, ``publication_type``,
    ``doi``, ``venue``, ``url``, ``ris_type`` or ``none``.
    """

    title = str(record.get("title", ""))
    evidence: dict[str, list[tuple[str, str]]] = {}

    def add(kind: str, source: str, text: str) -> None:
        evidence.setdefault(kind, []).append((source, text))

    title_kind = publication_kind(title)
    if title_kind != "article":
        add(title_kind, "title", f"title carries a '{title_kind}' marker")

    type_values = publication_type_values(record)
    for value in type_values:
        for kind, pattern in _TYPE_RULES:
            if pattern.search(value):
                add(kind, "publication_type", f"publication type '{value}'")
                break

    server = preprint_doi_server(str(record.get("doi", "")))
    if server:
        add("preprint", "doi", f"DOI prefix belongs to {server}")
    journal = str(record.get("journal", ""))
    if journal and PREPRINT_VENUE.search(journal):
        add("preprint", "venue", f"source/journal '{journal}' is a preprint server")
    if PREPRINT_TITLE.search(title):
        add("preprint", "title", "title is tagged [preprint]")

    if journal and REGISTRY_VENUE.search(journal):
        add("registry", "venue", f"source '{journal}' is a trial registry")
    for url in _raw_values(record, URL_KEYS):
        if REGISTRY_URL.search(url):
            add("registry", "url", "link points to a trial registry")
            break

    if has_strict_protocol_marker(title):
        add("protocol", "title", "title contains a protocol marker")
    if CONFERENCE_ABSTRACT_TITLE.search(html.unescape(title)):
        add("conference_abstract", "title", "title ends with a conference-abstract marker")

    for ris_type in _raw_values(record, {"ty"}):
        ris_type = ris_type.strip().upper()
        if ris_type in {"CONF", "CPAPER"}:
            add("conference_paper", "ris_type", f"RIS type {ris_type}")
        elif ris_type == "THES":
            add("thesis", "ris_type", "RIS type THES")

    if not evidence:
        return {
            "kind": "unspecified",
            "label": VERSION_LABELS["unspecified"],
            "source": "none",
            "evidence": [],
            "has_type_metadata": bool(type_values),
        }

    # Notices (from the title) outrank everything, then the fixed priority. A
    # conference abstract outranks a generic RIS CONF type.
    kind = min(evidence, key=lambda item: _KIND_PRIORITY.index(item))
    if (
        kind == "conference_paper"
        and "conference_abstract" in evidence
    ):
        kind = "conference_abstract"
    sources = [source for source, _ in evidence[kind]]
    return {
        "kind": kind,
        "label": VERSION_LABELS[kind],
        "source": sources[0],
        "evidence": [text for _, text in evidence[kind]],
        "has_type_metadata": bool(type_values),
    }


def version_compatibility(left: dict[str, Any], right: dict[str, Any]) -> str:
    """Compare two classify_version results.

    ``same``: the same version, or both ordinary/unstated articles.
    ``type_known_once``: one copy is a special version known only from its
    publication-type metadata and the other copy states no type at all.
    ``distinct``: different publication versions.
    """

    left_kind, right_kind = left["kind"], right["kind"]
    if left_kind == right_kind:
        return "same"
    if left_kind in STANDARD_KINDS and right_kind in STANDARD_KINDS:
        return "same"
    if "unspecified" in (left_kind, right_kind):
        special = right if left_kind == "unspecified" else left
        if special["source"] in {"publication_type", "ris_type"}:
            return "type_known_once"
    return "distinct"


_MARKER_PHRASES = re.compile(
    r"\b(?:"
    r"(?:a\s+|an\s+)?(?:study|trial|research)?\s*protocol(?:\s+(?:for|of))?(?:\s+an?)?"
    r"|(?:correction|erratum|corrigendum|retraction(?:\s+note)?)(?:\s+(?:to|for))?"
    r"|comment(?:ary)?(?:\s+(?:on|regarding|concerning))?"
    r"|(?:reply|response)(?:\s+to)?"
    r"|expression\s+of\s+concern(?:\s+for)?"
    r"|pre\s?print"
    r"|(?:conference|meeting|congress)\s+abstracts?"
    r")\b",
)


def strip_version_markers(title: str) -> str:
    text = normalize_title(title)
    text = _MARKER_PHRASES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def related_title_score(left_title: str, right_title: str) -> float:
    left = strip_version_markers(left_title)
    right = strip_version_markers(right_title)
    if not left or not right:
        return 0.0
    plain = ratio(left, right)
    token = token_set_ratio(left, right)
    # Versions often add or drop a descriptive phrase ("a randomised controlled
    # trial"), so word overlap weighs slightly more than character order.
    return max(plain, 0.45 * plain + 0.55 * token)


RELATIONSHIP_LABELS = {
    frozenset({"preprint", "article"}): "Preprint and journal version",
    frozenset({"preprint", "unspecified"}): "Preprint and another version",
    frozenset({"conference_abstract", "article"}): "Conference abstract and full publication",
    frozenset({"conference_abstract", "unspecified"}): "Conference abstract and another version",
    frozenset({"conference_paper", "article"}): "Conference paper and journal version",
    frozenset({"conference_paper", "unspecified"}): "Conference paper and another version",
    frozenset({"conference_abstract", "conference_paper"}): "Conference abstract and conference paper",
    frozenset({"conference_abstract", "preprint"}): "Conference abstract and preprint",
    frozenset({"protocol", "article"}): "Protocol and results publication",
    frozenset({"protocol", "unspecified"}): "Protocol and results publication",
    frozenset({"registry", "article"}): "Trial registration and publication",
    frozenset({"registry", "unspecified"}): "Trial registration and publication",
    frozenset({"registry", "protocol"}): "Trial registration and protocol",
    frozenset({"thesis", "article"}): "Thesis and journal version",
    frozenset({"thesis", "unspecified"}): "Thesis and another version",
    frozenset({"preprint", "thesis"}): "Preprint and thesis",
}


def relationship_label(left_kind: str, right_kind: str) -> str:
    pair = frozenset({left_kind, right_kind})
    if pair in RELATIONSHIP_LABELS:
        return RELATIONSHIP_LABELS[pair]
    for kind in (left_kind, right_kind):
        if kind in NOTICE_KINDS:
            other = right_kind if kind == left_kind else left_kind
            if other in NOTICE_KINDS and other != kind:
                return f"{VERSION_LABELS[kind]} and {VERSION_LABELS[other].lower()}"
            return f"{VERSION_LABELS[kind]} linked to the publication it concerns"
    return f"{VERSION_LABELS.get(left_kind, left_kind)} and {VERSION_LABELS.get(right_kind, right_kind).lower()}"
