from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


CANONICAL_FIELDS = (
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


@dataclass
class SourceTable:
    """A logical table awaiting canonical field mapping."""

    table_id: str
    file_id: str
    filename: str
    file_type: str
    sheet_name: str | None
    header_index: int
    raw_rows: list[list[str]]
    columns: list[str]
    rows: list[dict[str, str]]
    blank_rows: int = 0
    preamble_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    inferred_mapping: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceTable":
        return cls(**value)


@dataclass
class ArticleRecord:
    """Canonical fields plus lossless source metadata."""

    record_id: str
    source_file_id: str
    source_filename: str
    source_table_id: str
    source_sheet: str | None
    source_row: int
    title: str = ""
    year: str = ""
    first_author: str = ""
    authors: str = ""
    doi: str = ""
    journal: str = ""
    pages: str = ""
    article_number: str = ""
    issn: str = ""
    isbn: str = ""
    abstract: str = ""
    publication_type: str = ""
    raw_fields: dict[str, Any] = field(default_factory=dict)
    parse_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArticleRecord":
        return cls(**value)


@dataclass
class FileReport:
    file_id: str
    filename: str
    file_type: str
    logical_items: int
    parsed_records: int
    blank_rows: int = 0
    preamble_rows: int = 0
    tables: int = 1
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
