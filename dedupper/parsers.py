from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from charset_normalizer import from_bytes

from .doi import DOI_RE, extract_doi
from .models import ArticleRecord, SourceTable


SUPPORTED_EXTENSIONS = {".ris", ".txt", ".csv", ".tsv", ".xlsx", ".xls"}
RIS_TAG_RE = re.compile(r"^\s*([A-Za-z0-9]{2,4})\s*-\s?(.*)$")
YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2}|21\d{2})(?!\d)")
PUBMED_RECORD_START_RE = re.compile(
    r"(?m)^[ \t]*(?P<number>\d+)\.[ \t]+(?P<citation>\S.*)$"
)
PUBMED_CITATION_YEAR_RE = re.compile(
    r"\.\s*(?:1[5-9]\d{2}|20\d{2}|21\d{2})(?:\b|;)"
)
PMID_RE = re.compile(r"(?im)^[ \t]*PMID:\s*(\d+)")
ISSN_RE = re.compile(r"(?<!\d)(\d{4})[-\s]?(\d{3}[\dXx])(?!\d)")
ISBN_RE = re.compile(
    r"(?<!\d)(?:(97[89])[-\s]?)?\d[-\s\d]{7,15}[\dXx](?!\d)"
)
PUBMED_METADATA_HEADING_RE = re.compile(
    r"^(?:doi|pmid|pmcid|issn|isbn|publication types?|mesh terms?|substances?|"
    r"grant support|conflict of interest statement|clinical trial registration|"
    r"associated data)\s*:",
    re.IGNORECASE,
)


class ParseError(ValueError):
    pass


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode common export encodings without silently dropping characters."""

    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        match = from_bytes(data).best()
        if match is not None:
            return str(match), match.encoding or "detected"
        for encoding in ("cp1252", "latin-1"):
            try:
                return data.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
    raise ParseError("The text encoding could not be detected without data loss.")


def parse_uploaded_file(path: Path, file_id: str) -> dict[str, Any]:
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ParseError(
            f"Unsupported file type {extension or '(none)'}. "
            f"Use {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )
    if extension == ".ris":
        records, diagnostics = parse_ris(path.read_bytes(), file_id, path.name)
        return {
            "kind": "records",
            "records": [record.to_dict() for record in records],
            "diagnostics": diagnostics,
        }
    if extension == ".txt":
        records, diagnostics = parse_pubmed_txt(
            path.read_bytes(), file_id, path.name
        )
        return {
            "kind": "records",
            "records": [record.to_dict() for record in records],
            "diagnostics": diagnostics,
        }
    if extension in {".csv", ".tsv"}:
        tables = parse_delimited(path.read_bytes(), file_id, path.name, extension)
    else:
        tables = parse_workbook(path, file_id, path.name)
    return {
        "kind": "tables",
        "tables": [table.to_dict() for table in tables],
    }


def parse_ris(
    data: bytes, file_id: str, filename: str
) -> tuple[list[ArticleRecord], dict[str, Any]]:
    text, encoding = decode_text(data)
    raw_records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    last_tag: str | None = None
    warnings: list[str] = []
    orphan_lines = 0

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\r\n")
        match = RIS_TAG_RE.match(line)
        if match:
            tag, value = match.group(1).upper(), match.group(2).strip()
            if tag == "ER" and current is None:
                orphan_lines += 1
                continue
            if tag == "TY":
                if current and any(current.values()):
                    raw_records.append(current)
                    warnings.append(
                        f"Line {line_number}: a new TY tag started before ER; "
                        "the previous record was retained."
                    )
                current = defaultdict(list)
            elif current is None:
                current = defaultdict(list)
                warnings.append(
                    f"Line {line_number}: record began without a TY tag; it was retained."
                )
            assert current is not None
            current[tag].append(value)
            last_tag = tag
            if tag == "ER":
                raw_records.append(dict(current))
                current = None
                last_tag = None
            continue

        if not line.strip():
            continue
        if current is not None and last_tag and current.get(last_tag):
            current[last_tag][-1] = f"{current[last_tag][-1]}\n{line.strip()}"
        else:
            orphan_lines += 1

    if current and any(current.values()):
        raw_records.append(dict(current))
        warnings.append("The last RIS record had no ER tag; it was retained at EOF.")
    if orphan_lines:
        warnings.append(
            f"{orphan_lines} line(s) outside a RIS record could not be assigned to an item."
        )
    if not raw_records:
        raise ParseError("The RIS file contains no records.")

    records: list[ArticleRecord] = []
    for index, raw_fields in enumerate(raw_records, start=1):
        records.append(_ris_to_record(raw_fields, file_id, filename, index))

    return records, {
        "encoding": encoding,
        "logical_items": len(raw_records),
        "parsed_records": len(records),
        "blank_rows": 0,
        "preamble_rows": 0,
        "warnings": warnings,
    }


def _first_value(fields: dict[str, list[str]], tags: Iterable[str]) -> str:
    for tag in tags:
        for value in fields.get(tag, []):
            if value.strip():
                return value.strip()
    return ""


def _joined_values(fields: dict[str, list[str]], tags: Iterable[str]) -> str:
    for tag in tags:
        values = [value.strip() for value in fields.get(tag, []) if value.strip()]
        if values:
            return "\n\n".join(values)
    return ""


def _extract_doi(value: str) -> str:
    return extract_doi(value)


def _extract_year(value: str) -> str:
    match = YEAR_RE.search(value or "")
    return match.group(1) if match else ""


def _first_author_last_name(author: str) -> str:
    author = re.sub(r"\s+", " ", author or "").strip(" ;")
    if not author:
        return ""
    # Tabular exports often place all authors in one cell. Work only with the
    # first listed author before deriving the surname.
    author = re.split(r"\s*;\s*|\s*\|\s*|\s+and\s+", author, maxsplit=1)[0]
    author = re.sub(r"\(\d[\d,\- ]*\)", "", author).strip()
    if "," in author:
        return author.split(",", 1)[0].strip()
    parts = author.split()
    if len(parts) == 1:
        return parts[0]
    suffixes = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
    if parts[-1].casefold() in suffixes and len(parts) > 2:
        return parts[-2]
    initials = re.compile(r"(?:[A-ZÀ-Þ]\.?){1,8}$")
    if initials.fullmatch(parts[-1]) and len(parts) > 1:
        return parts[-2]
    if initials.fullmatch(parts[0]) and len(parts) > 1:
        return parts[-1]
    return parts[-1]


def _joined_authors(fields: dict[str, list[str]]) -> str:
    for tag in ("AU", "A1", "A2", "ED"):
        values = [
            re.sub(r"\s+", " ", value).strip()
            for value in fields.get(tag, [])
            if value.strip()
        ]
        if values:
            return "; ".join(values)
    return ""


def _extract_pages(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" .;,")
    if not value:
        return ""
    match = re.search(
        r"(?<!\w)(?:pp?\.?\s*)?"
        r"(?P<pages>[A-Za-z]?\d+[A-Za-z]?(?:\s*[-–—]\s*[A-Za-z]?\d+[A-Za-z]?)?)"
        r"(?:\s*;\s*discussion\s+[A-Za-z]?\d+(?:\s*[-–—]\s*\d+)?)?",
        value,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return re.sub(r"\s*[-–—]\s*", "-", match.group("pages"))


def _extract_identifiers(value: str) -> tuple[str, str]:
    text = value or ""
    issn_match = ISSN_RE.search(text)
    issn = (
        f"{issn_match.group(1)}-{issn_match.group(2).upper()}"
        if issn_match
        else ""
    )
    isbn = ""
    for match in ISBN_RE.finditer(text):
        compact = re.sub(r"[-\s]", "", match.group(0)).upper()
        if len(compact) in {10, 13} and (not issn or compact != issn.replace("-", "")):
            isbn = compact
            break
    return issn, isbn


def _ris_pages(fields: dict[str, list[str]]) -> str:
    start = _first_value(fields, ("SP",))
    end = _first_value(fields, ("EP",))
    if start and end:
        return f"{start.strip()}-{end.strip()}"
    return start or _first_value(fields, ("PG", "SE"))


def _ris_to_record(
    fields: dict[str, list[str]], file_id: str, filename: str, row_number: int
) -> ArticleRecord:
    title = _first_value(fields, ("TI", "T1", "CT", "BT"))
    year_source = _first_value(fields, ("PY", "Y1", "DA"))
    authors = _joined_authors(fields)
    author = _first_value(fields, ("AU", "A1", "A2", "ED"))
    journal = _first_value(fields, ("JO", "JF", "T2", "JA"))
    pages = _ris_pages(fields)
    article_number = _first_value(fields, ("C7", "AR"))
    serial_number = _joined_values(fields, ("SN",))
    issn, isbn = _extract_identifiers(serial_number)
    abstract = _joined_values(fields, ("AB", "N2"))
    publication_type = _joined_values(fields, ("PT", "DT", "M3"))
    doi = _extract_doi(_first_value(fields, ("DO", "L3", "UR")))
    if not doi:
        for values in fields.values():
            doi = _extract_doi(" ".join(values))
            if doi:
                break

    parse_warnings: list[str] = []
    if not title:
        parse_warnings.append("No title tag (TI/T1/CT/BT) was found.")
    if year_source and not _extract_year(year_source):
        parse_warnings.append(f"Year value could not be normalized: {year_source!r}.")

    return ArticleRecord(
        record_id=uuid.uuid4().hex,
        source_file_id=file_id,
        source_filename=filename,
        source_table_id=f"{file_id}:ris",
        source_sheet=None,
        source_row=row_number,
        title=title,
        year=_extract_year(year_source),
        first_author=_first_author_last_name(author),
        authors=authors,
        doi=doi,
        journal=journal,
        pages=pages,
        article_number=article_number,
        issn=issn,
        isbn=isbn,
        abstract=abstract,
        publication_type=publication_type,
        raw_fields={key: list(values) for key, values in fields.items()},
        parse_warnings=parse_warnings,
    )


def parse_pubmed_txt(
    data: bytes, file_id: str, filename: str
) -> tuple[list[ArticleRecord], dict[str, Any]]:
    """Parse PubMed's numbered plain-text abstract export.

    PubMed wraps citation, title, affiliation and abstract text across physical
    lines. Records are recognized by their numbered citation header rather than
    by blank-line counts, so blank lines inside a record do not create phantom
    articles.
    """

    decoded, encoding = decode_text(data)
    text = decoded.replace("\r\n", "\n").replace("\r", "\n")
    starts = [
        match
        for match in PUBMED_RECORD_START_RE.finditer(text)
        if PUBMED_CITATION_YEAR_RE.search(_pubmed_citation(text, match))
    ]
    if not starts:
        raise ParseError(
            "The TXT file does not look like a numbered PubMed abstract export."
        )

    warnings: list[str] = []
    prefix = text[: starts[0].start()].strip()
    if prefix:
        warnings.append(
            "Text before the first numbered PubMed citation was not assigned "
            "to an article record."
        )

    records: list[ArticleRecord] = []
    for index, match in enumerate(starts, start=1):
        end = starts[index].start() if index < len(starts) else len(text)
        block = text[match.start() : end].strip()
        records.append(
            _pubmed_block_to_record(block, file_id, filename, index)
        )

    return records, {
        "encoding": encoding,
        "logical_items": len(starts),
        "parsed_records": len(records),
        "blank_rows": 0,
        "preamble_rows": 0,
        "warnings": warnings,
    }


def _pubmed_citation(text: str, match: re.Match[str]) -> str:
    line_end = text.find("\n", match.end())
    if line_end == -1:
        return match.group("citation").strip()
    citation_lines = [match.group("citation").strip()]
    cursor = line_end + 1
    while cursor < len(text):
        next_end = text.find("\n", cursor)
        if next_end == -1:
            next_end = len(text)
        line = text[cursor:next_end].strip()
        if not line:
            break
        citation_lines.append(line)
        cursor = next_end + 1
    return " ".join(citation_lines)


def _paragraphs(lines: list[str]) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            current.append(stripped)
        elif current:
            paragraphs.append(re.sub(r"\s+", " ", " ".join(current)).strip())
            current = []
    if current:
        paragraphs.append(re.sub(r"\s+", " ", " ".join(current)).strip())
    return paragraphs


def _looks_like_pubmed_authors(value: str) -> bool:
    if not value or len(value) > 800 or ":" in value:
        return False
    return bool(
        re.search(
            r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+"
            r"(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ'’\-]+)*\s+[A-Z]{1,8}"
            r"(?:\(\d[\d,\- ]*\))?(?:,|\.|$)",
            value,
        )
    )


def _pubmed_first_author_last_name(authors: str) -> str:
    first = (authors or "").split(",", 1)[0].strip(" .;")
    first = re.sub(r"\(\d[\d,\- ]*\)", "", first).strip()
    if not first:
        return ""
    tokens = first.split()
    suffixes = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
    if tokens and tokens[-1].casefold() in suffixes:
        tokens.pop()
    if len(tokens) > 1 and re.fullmatch(r"(?:[A-Z]\.?){1,8}", tokens[-1]):
        tokens.pop()
    return " ".join(tokens).strip() or first


def _pubmed_pages_and_article_number(citation: str) -> tuple[str, str]:
    page_match = re.search(
        r"(?:\)|\d)\s*:\s*([^.;]+)(?:[.;]|$)", citation or ""
    )
    if not page_match:
        return "", ""
    value = page_match.group(1).strip()
    pages = _extract_pages(value)
    article_number = ""
    if not pages:
        match = re.search(r"\b(e\d{3,}|[A-Za-z]\d{5,})\b", value, re.IGNORECASE)
        article_number = match.group(1) if match else ""
    return pages, article_number


def _looks_like_affiliation(value: str) -> bool:
    normalized = value.casefold()
    return bool(
        re.match(r"^\(\d[\d,\- ]*\)", value)
        or "@" in value
        or re.search(
            r"\b(department|university|universit[aäeé]|hospital|institute|"
            r"school of|faculty|clinic|centre|center|laboratory|address)\b",
            normalized,
        )
    )


def _is_pubmed_tail_metadata(value: str) -> bool:
    normalized = value.casefold().lstrip()
    prefixes = (
        "copyright ",
        "copyright(",
        "copyright (",
        "©",
        "doi:",
        "pmid:",
        "pmcid:",
        "issn:",
        "isbn:",
        "publication types:",
        "mesh terms:",
        "substances:",
        "grant support:",
        "conflict of interest statement",
        "clinical trial registration",
        "associated data",
    )
    return normalized.startswith(prefixes)


def _pubmed_metadata_value(paragraphs: list[str], heading: str) -> str:
    """Return a named PubMed footer field, including wrapped/list values."""

    normalized_heading = heading.casefold().rstrip(":")
    for index, paragraph in enumerate(paragraphs):
        label, separator, inline = paragraph.partition(":")
        if not separator or label.strip().casefold() != normalized_heading:
            continue
        values = [inline.strip()] if inline.strip() else []
        cursor = index + 1
        while cursor < len(paragraphs):
            value = paragraphs[cursor].strip()
            if PUBMED_METADATA_HEADING_RE.match(value):
                break
            values.append(value)
            cursor += 1
        return "; ".join(value for value in values if value)
    return ""


def _pubmed_block_to_record(
    block: str, file_id: str, filename: str, row_number: int
) -> ArticleRecord:
    lines = block.splitlines()
    first_match = PUBMED_RECORD_START_RE.match(lines[0]) if lines else None
    if first_match is None:
        raise ParseError(f"PubMed record {row_number} has no numbered citation.")

    citation_lines = [first_match.group("citation").strip()]
    cursor = 1
    while cursor < len(lines) and lines[cursor].strip():
        citation_lines.append(lines[cursor].strip())
        cursor += 1
    citation = re.sub(r"\s+", " ", " ".join(citation_lines)).strip()
    paragraphs = _paragraphs(lines[cursor:])

    title = paragraphs[0] if paragraphs else ""
    author_info_index = next(
        (
            index
            for index, paragraph in enumerate(paragraphs)
            if paragraph.casefold().startswith("author information:")
        ),
        None,
    )
    authors_index: int | None = None
    if author_info_index is not None and author_info_index > 0:
        authors_index = author_info_index - 1
    else:
        authors_index = next(
            (
                index
                for index, paragraph in enumerate(paragraphs[1:5], start=1)
                if _looks_like_pubmed_authors(paragraph)
            ),
            None,
        )
    authors = paragraphs[authors_index] if authors_index is not None else ""

    content_index = (authors_index + 1) if authors_index is not None else 1
    affiliations: list[str] = []
    if author_info_index is not None:
        inline_affiliation = paragraphs[author_info_index].split(":", 1)[1].strip()
        if inline_affiliation:
            affiliations.append(inline_affiliation)
        content_index = author_info_index + 1
        while (
            content_index < len(paragraphs)
            and _looks_like_affiliation(paragraphs[content_index])
        ):
            affiliations.append(paragraphs[content_index])
            content_index += 1

    abstract_parts: list[str] = []
    for paragraph in paragraphs[content_index:]:
        if _is_pubmed_tail_metadata(paragraph):
            break
        if paragraph.casefold() == "abstract":
            continue
        if paragraph.casefold().startswith("abstract:"):
            paragraph = paragraph.split(":", 1)[1].strip()
        if paragraph.casefold() == "[no abstract available]":
            break
        abstract_parts.append(paragraph)
    abstract = "\n\n".join(abstract_parts).strip()

    year = _extract_year(citation)
    year_match = YEAR_RE.search(citation)
    journal = citation[: year_match.start()].rstrip(" .;") if year_match else ""
    doi = _extract_doi(block)
    pmid_match = PMID_RE.search(block)
    pmid = pmid_match.group(1) if pmid_match else ""
    publication_type = _pubmed_metadata_value(paragraphs, "Publication Types")
    if not publication_type:
        publication_type = _pubmed_metadata_value(paragraphs, "Publication Type")
    record_number = first_match.group("number")
    pages, article_number = _pubmed_pages_and_article_number(citation)
    serial_text = "; ".join(
        value
        for value in (
            _pubmed_metadata_value(paragraphs, "ISSN"),
            _pubmed_metadata_value(paragraphs, "ISBN"),
        )
        if value
    )
    issn, isbn = _extract_identifiers(serial_text)

    parse_warnings: list[str] = []
    if not title:
        parse_warnings.append("No article title was found in the PubMed record.")
    if not year:
        parse_warnings.append("No publication year was found in the citation.")
    if not authors:
        parse_warnings.append("No author line was recognized in the PubMed record.")

    raw_fields: dict[str, Any] = {
        "record_number": record_number,
        "citation": citation,
        "title": title,
        "authors": authors,
        "author_information": affiliations,
        "abstract": abstract,
        "publication_type": publication_type,
        "pages": pages,
        "article_number": article_number,
        "issn": issn,
        "isbn": isbn,
        "doi": doi,
        "pmid": pmid,
        "raw_text": block,
    }
    return ArticleRecord(
        record_id=uuid.uuid4().hex,
        source_file_id=file_id,
        source_filename=filename,
        source_table_id=f"{file_id}:pubmed_txt",
        source_sheet=None,
        source_row=row_number,
        title=title,
        year=year,
        first_author=_pubmed_first_author_last_name(authors),
        authors=authors,
        doi=doi,
        journal=journal,
        pages=pages,
        article_number=article_number,
        issn=issn,
        isbn=isbn,
        abstract=abstract,
        publication_type=publication_type,
        raw_fields=raw_fields,
        parse_warnings=parse_warnings,
    )


def parse_delimited(
    data: bytes, file_id: str, filename: str, extension: str
) -> list[SourceTable]:
    text, encoding = decode_text(data)
    delimiter = "\t" if extension == ".tsv" else _detect_delimiter(text)
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as exc:
        raise ParseError(f"Could not parse the delimited file: {exc}") from exc
    rows = [[cell.strip() for cell in row] for row in rows]
    if not any(any(cell for cell in row) for row in rows):
        raise ParseError("The file contains no non-empty rows.")
    header_index = detect_header_row(rows)
    table = build_source_table(
        raw_rows=rows,
        table_id=f"{file_id}:table",
        file_id=file_id,
        filename=filename,
        file_type=extension.lstrip("."),
        sheet_name=None,
        header_index=header_index,
    )
    table.warnings.insert(
        0, f"Detected {encoding} encoding and {delimiter!r} delimiter."
    )
    return [table]


def _detect_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:30])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        counts = {candidate: sample.count(candidate) for candidate in ",;\t|"}
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        return delimiter if count else ","


def parse_workbook(path: Path, file_id: str, filename: str) -> list[SourceTable]:
    engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
    try:
        sheets = pd.read_excel(
            path,
            sheet_name=None,
            header=None,
            dtype=object,
            keep_default_na=False,
            engine=engine,
        )
    except Exception as exc:
        raise ParseError(f"Could not read the workbook: {exc}") from exc
    tables: list[SourceTable] = []
    for sheet_index, (sheet_name, frame) in enumerate(sheets.items(), start=1):
        raw_rows = [
            [_stringify(value) for value in row]
            for row in frame.itertuples(index=False, name=None)
        ]
        if not any(any(cell for cell in row) for row in raw_rows):
            continue
        header_index = detect_header_row(raw_rows)
        tables.append(
            build_source_table(
                raw_rows=raw_rows,
                table_id=f"{file_id}:sheet:{sheet_index}",
                file_id=file_id,
                filename=filename,
                file_type=path.suffix.lower().lstrip("."),
                sheet_name=str(sheet_name),
                header_index=header_index,
            )
        )
    if not tables:
        raise ParseError("The workbook contains no non-empty worksheets.")
    return tables


def detect_header_row(rows: list[list[str]], scan_limit: int = 15) -> int:
    nonempty_indices = [
        index for index, row in enumerate(rows) if any(cell.strip() for cell in row)
    ]
    if not nonempty_indices:
        return 0
    candidates = nonempty_indices[:scan_limit]
    best_index = candidates[0]
    best_score = float("-inf")
    known_headers = {
        "title",
        "titel",
        "article title",
        "year",
        "jaar",
        "doi",
        "author",
        "authors",
        "auteur",
        "journal",
        "abstract",
        "abstract text",
        "publication type",
        "publication types",
        "document type",
        "article type",
        "publicatietype",
        "summary",
        "samenvatting",
        "source",
        "publication",
    }
    for index in candidates:
        cells = [cell.strip() for cell in rows[index] if cell.strip()]
        if not cells:
            continue
        normalized = [_normalize_header(cell) for cell in cells]
        uniqueness = len(set(normalized)) / len(normalized)
        text_fraction = sum(not _looks_like_scalar(cell) for cell in cells) / len(cells)
        known = sum(value in known_headers for value in normalized)
        following = [
            row
            for row in rows[index + 1 : index + 6]
            if any(cell.strip() for cell in row)
        ]
        width = len(rows[index])
        width_match = (
            sum(abs(len(row) - width) <= 1 for row in following) / len(following)
            if following
            else 0
        )
        score = (
            known * 4
            + uniqueness * 1.5
            + text_fraction
            + width_match
            - index * 0.03
        )
        if score > best_score:
            best_score, best_index = score, index
    return best_index


def _normalize_header(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _looks_like_scalar(value: str) -> bool:
    value = value.strip()
    return bool(
        re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", value)
        or DOI_RE.fullmatch(value)
        or re.fullmatch(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", value)
    )


def _unique_columns(headers: list[str], width: int) -> list[str]:
    values = list(headers) + [""] * max(0, width - len(headers))
    counts: Counter[str] = Counter()
    result: list[str] = []
    for index, original in enumerate(values[:width], start=1):
        base = original.strip() or f"Column {index}"
        counts[base] += 1
        result.append(base if counts[base] == 1 else f"{base} [{counts[base]}]")
    return result


def build_source_table(
    *,
    raw_rows: list[list[str]],
    table_id: str,
    file_id: str,
    filename: str,
    file_type: str,
    sheet_name: str | None,
    header_index: int,
) -> SourceTable:
    if header_index < 0 or header_index >= len(raw_rows):
        raise ParseError("The selected header row is outside the source data.")
    data_rows = raw_rows[header_index + 1 :]
    width = max(
        [len(raw_rows[header_index]), *(len(row) for row in data_rows)] or [0]
    )
    columns = _unique_columns(raw_rows[header_index], width)
    parsed_rows: list[dict[str, str]] = []
    blank_rows = 0
    for source_index, values in enumerate(data_rows, start=header_index + 2):
        padded = values + [""] * (width - len(values))
        row = dict(zip(columns, padded[:width]))
        if not any(value.strip() for value in row.values()):
            blank_rows += 1
            continue
        row["__source_row__"] = str(source_index)
        parsed_rows.append(row)
    preamble_rows = sum(
        any(cell.strip() for cell in row) for row in raw_rows[:header_index]
    )
    warnings: list[str] = []
    if preamble_rows:
        warnings.append(
            f"{preamble_rows} non-empty row(s) before the detected header are "
            "reported as preamble, not article records."
        )
    return SourceTable(
        table_id=table_id,
        file_id=file_id,
        filename=filename,
        file_type=file_type,
        sheet_name=sheet_name,
        header_index=header_index,
        raw_rows=raw_rows,
        columns=columns,
        rows=parsed_rows,
        blank_rows=blank_rows,
        preamble_rows=preamble_rows,
        warnings=warnings,
    )


def rebuild_source_table(table: SourceTable, header_index: int) -> SourceTable:
    rebuilt = build_source_table(
        raw_rows=table.raw_rows,
        table_id=table.table_id,
        file_id=table.file_id,
        filename=table.filename,
        file_type=table.file_type,
        sheet_name=table.sheet_name,
        header_index=header_index,
    )
    retained = [
        warning
        for warning in table.warnings
        if warning.startswith("Detected ") and "delimiter" in warning
    ]
    rebuilt.warnings = retained + rebuilt.warnings
    return rebuilt


def table_to_records(
    table: SourceTable, mapping: dict[str, str | None]
) -> list[ArticleRecord]:
    records: list[ArticleRecord] = []
    for row in table.rows:
        raw_fields = {
            key: value for key, value in row.items() if key != "__source_row__"
        }

        def mapped(field: str) -> str:
            column = mapping.get(field)
            return raw_fields.get(column, "").strip() if column else ""

        year_value = mapped("year")
        doi_value = mapped("doi")
        authors_value = mapped("authors")
        author_value = mapped("first_author") or authors_value
        issn, _ = _extract_identifiers(mapped("issn"))
        _, isbn = _extract_identifiers(mapped("isbn"))
        parse_warnings: list[str] = []
        if year_value and not _extract_year(year_value):
            parse_warnings.append(f"Year could not be normalized: {year_value!r}.")
        if doi_value and not _extract_doi(doi_value):
            parse_warnings.append(f"DOI could not be normalized: {doi_value!r}.")
        records.append(
            ArticleRecord(
                record_id=uuid.uuid4().hex,
                source_file_id=table.file_id,
                source_filename=table.filename,
                source_table_id=table.table_id,
                source_sheet=table.sheet_name,
                source_row=int(row["__source_row__"]),
                title=mapped("title"),
                year=_extract_year(year_value),
                first_author=_first_author_last_name(author_value),
                authors=authors_value or mapped("first_author"),
                doi=_extract_doi(doi_value),
                journal=mapped("journal"),
                pages=mapped("pages"),
                article_number=mapped("article_number"),
                issn=issn,
                isbn=isbn,
                abstract=mapped("abstract"),
                publication_type=mapped("publication_type"),
                raw_fields=raw_fields,
                parse_warnings=parse_warnings,
            )
        )
    return records


def serialize_for_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
