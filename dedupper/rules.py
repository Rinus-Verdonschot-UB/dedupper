"""Central catalogue of matching rules and thresholds.

Every automatic decision Dedupper makes carries one of the rule identifiers
below. The same catalogue is rendered on the ``/rules`` page and written into
the audit JSON, so a reviewer can always look up exactly which rule produced a
decision and which thresholds were in force.

Changing a threshold changes scientific-review outcomes. Override defaults only
through :func:`settings_from_env` (environment variables) so the values used
for a run are recorded in that run's audit trail.
"""

from __future__ import annotations

import os
from typing import Any


DEFAULT_SETTINGS: dict[str, float] = {
    # Minimum cleaned-title similarity for a fuzzy duplicate candidate.
    "fuzzy_title_threshold": 92.0,
    # Minimum similarity of marker-stripped titles for a related-publication link.
    "related_title_threshold": 88.0,
    # Related link with conflicting first authors needs this title similarity.
    "related_title_threshold_author_conflict": 92.0,
    # Conference abstracts: near-identical title plus identical first author.
    "conference_title_threshold": 96.0,
    "conference_author_threshold": 100.0,
    # Same DOI: title similarity that confirms the DOI on its own.
    "doi_title_confirm": 88.0,
    # Same DOI: below this title similarity the titles are considered to conflict.
    "doi_title_conflict": 55.0,
    # Maximum allowed publication-year difference inside one duplicate group.
    "max_year_difference": 1.0,
}

ENV_OVERRIDES = {
    "fuzzy_title_threshold": "DEDUPPER_FUZZY_TITLE_THRESHOLD",
    "related_title_threshold": "DEDUPPER_RELATED_TITLE_THRESHOLD",
}


def settings_from_env(environ: dict[str, str] | None = None) -> dict[str, float]:
    environ = dict(os.environ if environ is None else environ)
    settings = dict(DEFAULT_SETTINGS)
    for key, variable in ENV_OVERRIDES.items():
        value = environ.get(variable, "").strip()
        if not value:
            continue
        try:
            number = float(value)
        except ValueError as exc:
            raise ValueError(f"{variable} must be a number, got {value!r}.") from exc
        if not 50 <= number <= 100:
            raise ValueError(f"{variable} must be between 50 and 100.")
        settings[key] = number
    return settings


# outcome: duplicate | review | related | separate
RULES: dict[str, dict[str, Any]] = {
    "D1": {
        "name": "Corroborated DOI",
        "outcome": "duplicate",
        "description": (
            "Identical DOI, confirmed by a similar title (≥ doi_title_confirm), "
            "the full author list, pages/article number, or first author plus "
            "year/journal, with no clear title conflict."
        ),
    },
    "D2": {
        "name": "Corroborated exact title",
        "outcome": "duplicate",
        "description": (
            "Identical cleaned title and at least one independent field "
            "(author, ISSN/ISBN, pages, article number, year within one, or "
            "journal). Short titles (< 28 characters) need stronger support."
        ),
    },
    "D3": {
        "name": "Corroborated fuzzy title",
        "outcome": "duplicate",
        "description": (
            "Title similarity ≥ fuzzy_title_threshold plus author, identifier, "
            "or year-and-journal agreement."
        ),
    },
    "D4": {
        "name": "Conference abstract title + first author",
        "outcome": "duplicate",
        "description": (
            "Two conference abstracts (or an abstract and an untyped copy in the "
            "same venue) with near-identical titles (≥ conference_title_threshold), "
            "the same first-author surname and the same numbers in the title. "
            "A shared proceedings DOI is ignored."
        ),
    },
    "R1": {
        "name": "DOI metadata conflict",
        "outcome": "review",
        "description": (
            "Identical DOI but titles/authors conflict, the years differ by more "
            "than one, or there is too little supporting metadata."
        ),
    },
    "R2": {
        "name": "Uncorroborated title match",
        "outcome": "review",
        "description": "Identical cleaned title with no independent supporting field.",
    },
    "R3": {
        "name": "Group conflict",
        "outcome": "review",
        "description": (
            "The pair matches, but joining it to an existing group would put two "
            "DOIs, two publication versions, two ISBNs or years more than one "
            "apart in the same group."
        ),
    },
    "R4": {
        "name": "Title shared by several DOIs",
        "outcome": "review",
        "description": (
            "A record without a DOI has the same title as records with different "
            "DOIs, so it cannot be assigned to one of them automatically."
        ),
    },
    "V1": {
        "name": "Related publication (different version)",
        "outcome": "related",
        "description": (
            "Titles agree after removing version markers (≥ related_title_threshold) "
            "but the records are different publication versions, e.g. preprint and "
            "journal article, conference abstract and full paper, protocol and "
            "results paper, or a correction notice and the original article. Both "
            "are kept unless you choose otherwise."
        ),
    },
    "V2": {
        "name": "Publication type known for one copy only",
        "outcome": "related",
        "description": (
            "One record is labelled e.g. conference abstract or preprint, the other "
            "has no publication type, and neither the DOI nor the journal/ISSN "
            "confirms they are the same item. Shown as related instead of merged."
        ),
    },
    "S1": {
        "name": "Different DOIs",
        "outcome": "separate",
        "description": "Both records have valid but different DOIs.",
    },
    "S2": {
        "name": "Publication years too far apart",
        "outcome": "separate",
        "description": "Publication years differ by more than max_year_difference.",
    },
    "S3": {
        "name": "Different ISBNs",
        "outcome": "separate",
        "description": "Both records carry different ISBNs.",
    },
    "S4": {
        "name": "Not enough supporting evidence",
        "outcome": "separate",
        "description": (
            "Titles are similar, but no author, identifier or year-and-journal "
            "evidence supports a duplicate."
        ),
    },
    "S6": {
        "name": "Different publication versions, titles not close enough",
        "outcome": "separate",
        "description": (
            "The records are different publication versions and their titles, "
            "after removing version markers, are below related_title_threshold."
        ),
    },
    "S5": {
        "name": "Conference abstracts do not match",
        "outcome": "separate",
        "description": (
            "Conference abstracts (often sharing one proceedings DOI) whose title "
            "or first author is not near-identical."
        ),
    },
}


def rule_name(rule_id: str) -> str:
    return RULES.get(rule_id, {}).get("name", rule_id)
