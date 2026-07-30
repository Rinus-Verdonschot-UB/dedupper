from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from dedupper.parsers import (
    ParseError,
    parse_delimited,
    parse_pubmed_txt,
    parse_ris,
    parse_workbook,
    rebuild_source_table,
    table_to_records,
)
import pytest


def test_ris_retains_repeated_tags_continuations_and_missing_final_er() -> None:
    payload = """TY  - JOUR
TI  - First title:
      with a continuation
AU  - Smith, Jane
AU  - Jones, Peter
PY  - 2024/02/10
JO  - Journal of Tests
DO  - https://doi.org/10.1234/ABC.7
ER  -

TY  - JOUR
T1  - Second title
A1  - María van Dijk
Y1  - 2023
JF  - Review & Research
ER  -

TY  - JOUR
TI  - Retained at end of file
AU  - Brown J
"""
    records, diagnostics = parse_ris(payload.encode(), "file1", "sample.ris")

    assert diagnostics["logical_items"] == 3
    assert diagnostics["parsed_records"] == 3
    assert records[0].title == "First title:\nwith a continuation"
    assert records[0].first_author == "Smith"
    assert records[0].doi == "10.1234/ABC.7"
    assert records[0].raw_fields["AU"] == ["Smith, Jane", "Jones, Peter"]
    assert records[1].first_author == "Dijk"
    assert records[2].title == "Retained at end of file"
    assert any("no ER tag" in warning for warning in diagnostics["warnings"])


def test_empty_ris_is_rejected_instead_of_reporting_zero_success() -> None:
    with pytest.raises(ParseError, match="contains no records"):
        parse_ris(b"\n\n", "file1", "empty.ris")


def test_ris_abstract_is_promoted_to_the_canonical_record() -> None:
    payload = (
        b"TY  - JOUR\nTI  - Article with an abstract\n"
        b"AB  - This abstract starts here\n"
        b"      and continues on the next physical line.\nER  -\n"
    )

    records, _ = parse_ris(payload, "file1", "abstract.ris")

    assert records[0].abstract == (
        "This abstract starts here\nand continues on the next physical line."
    )


def test_pubmed_txt_parses_numbered_records_and_complete_abstracts() -> None:
    payload = """1. ORL J Otorhinolaryngol Relat Spec. 2006;68(1):23-9; discussion 29-30. doi:
10.1159/000090487. Epub 2006 Mar 3.

Tinnitus retraining therapy: a different view on tinnitus.

Jastreboff PJ(1), Jastreboff MM.

Author information:
(1)Tinnitus and Hyperacusis Center, Department of Otolaryngology Head and Neck
Surgery, Emory University School of Medicine, Atlanta, GA 30322, USA.
pjastre@emory.edu

Tinnitus retraining therapy (TRT) is a method for treating tinnitus and
decreased sound tolerance, based on the neurophysiological model of tinnitus.
The second paragraph of the abstract must also be retained.

Copyright (c) 2006 S. Karger AG, Basel.

DOI: 10.1159/000090487
PMID: 16514259 [Indexed for MEDLINE]


2. Wien Med Wochenschr. 1992;142(11-12):259-62.

[Tinnitus: causes, diagnosis, therapy].

[Article in German]

Denk DM(1), Ehrenberger K.

Author information:
(1)I. Universitäts-HNO-Klinik, Universität Wien.

The symptom tinnitus may be due to a number of causes. Pathological processes
within the sense-organ "ear" as well as non-otologic diseases may result in
tinnitus.

PMID: 1529618 [Indexed for MEDLINE]
""".encode()

    records, diagnostics = parse_pubmed_txt(payload, "file1", "pubmed.txt")

    assert diagnostics["logical_items"] == 2
    assert diagnostics["parsed_records"] == 2
    assert records[0].title == (
        "Tinnitus retraining therapy: a different view on tinnitus."
    )
    assert records[0].journal == "ORL J Otorhinolaryngol Relat Spec"
    assert records[0].year == "2006"
    assert records[0].first_author == "Jastreboff"
    assert records[0].doi == "10.1159/000090487"
    assert "The second paragraph of the abstract" in records[0].abstract
    assert "Copyright" not in records[0].abstract
    assert records[0].raw_fields["pmid"] == "16514259"
    assert records[1].title == "[Tinnitus: causes, diagnosis, therapy]."
    assert records[1].first_author == "Denk"
    assert records[1].year == "1992"
    assert records[1].doi == ""
    assert "non-otologic diseases" in records[1].abstract


def test_non_pubmed_txt_is_rejected_with_a_clear_error() -> None:
    with pytest.raises(ParseError, match="numbered PubMed abstract export"):
        parse_pubmed_txt(b"ordinary unstructured notes", "file1", "notes.txt")


def test_semicolon_csv_detects_header_after_preamble_and_keeps_all_data_rows() -> None:
    payload = (
        "Export created by database\r\n"
        "Titel;Jaar;Auteur;DOI;Tijdschrift;Notes\r\n"
        "A first article;2020;Smith, J.;10.1000/one;Journal A;alpha\r\n"
        "A second article;2021;Jones P;;Journal B;beta\r\n"
        ";;;;;\r\n"
    ).encode("cp1252")
    table = parse_delimited(payload, "file1", "export.csv", ".csv")[0]

    assert table.header_index == 1
    assert table.preamble_rows == 1
    assert table.blank_rows == 1
    assert len(table.rows) == 2
    assert table.rows[1]["Titel"] == "A second article"
    assert table.rows[1]["__source_row__"] == "4"


def test_header_can_be_reselected_without_losing_raw_rows() -> None:
    payload = b"wrong,row\nTitle,Year\nOne,2020\nTwo,2021\n"
    table = parse_delimited(payload, "file1", "rows.csv", ".csv")[0]
    rebuilt = rebuild_source_table(table, 1)

    assert rebuilt.columns == ["Title", "Year"]
    assert len(rebuilt.rows) == 2
    assert rebuilt.rows[0]["Title"] == "One"


def test_tabular_mapping_preserves_unmapped_raw_fields() -> None:
    payload = (
        b"Title,Year,Abstract,Custom flag\n"
        b"One,2020,The first abstract,KEEP-ME\n"
        b"Two,2021,The second abstract,ALSO-ME\n"
    )
    table = parse_delimited(payload, "file1", "rows.csv", ".csv")[0]
    records = table_to_records(
        table,
        {
            "title": "Title",
            "year": "Year",
            "first_author": None,
            "doi": None,
            "journal": None,
            "abstract": "Abstract",
        },
    )

    assert len(records) == 2
    assert records[0].abstract == "The first abstract"
    assert records[0].raw_fields["Custom flag"] == "KEEP-ME"
    assert records[1].source_row == 3


def test_xlsx_reads_each_nonempty_worksheet(tmp_path: Path) -> None:
    workbook = Workbook()
    first = workbook.active
    first.title = "Database A"
    first.append(["Title", "Year"])
    first.append(["Article one", 2022])
    second = workbook.create_sheet("Database B")
    second.append(["Titel", "Jaar"])
    second.append(["Artikel twee", 2023])
    workbook.create_sheet("Empty")
    path = tmp_path / "sources.xlsx"
    workbook.save(path)

    tables = parse_workbook(path, "file1", path.name)

    assert [table.sheet_name for table in tables] == ["Database A", "Database B"]
    assert [len(table.rows) for table in tables] == [1, 1]
    assert tables[0].rows[0]["Year"] == "2022"
