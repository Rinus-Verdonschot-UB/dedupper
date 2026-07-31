from __future__ import annotations

import html
import re
from urllib.parse import unquote


# This is the broadly used Crossref-style DOI pattern. Decode URL escaping
# before applying it so suffix punctuation such as parentheses is retained.
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def _decode_doi_text(value: str) -> str:
    """Decode HTML and URL escaping without treating literal plus signs as spaces."""

    decoded = html.unescape(value or "").strip()
    # Some exports contain a DOI URL that has been escaped twice. Two passes
    # recover those values while still leaving malformed percent sequences as-is.
    for _ in range(2):
        unquoted = unquote(decoded)
        if unquoted == decoded:
            break
        decoded = unquoted
    return decoded


def _trim_citation_punctuation(candidate: str) -> str:
    candidate = candidate.rstrip(".,;:")

    # A DOI may legitimately contain balanced parentheses, including at the
    # very end. Remove a closing delimiter only when it belongs to surrounding
    # citation prose rather than to the DOI itself.
    while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
        candidate = candidate[:-1].rstrip(".,;:")
    return candidate


def extract_doi(value: str) -> str:
    """Return the first complete DOI in display form, or an empty string."""

    match = DOI_RE.search(_decode_doi_text(value))
    return _trim_citation_punctuation(match.group(0)) if match else ""


def normalize_doi(value: str) -> str:
    """Return a complete, comparison-safe DOI."""

    return extract_doi(value).casefold()
