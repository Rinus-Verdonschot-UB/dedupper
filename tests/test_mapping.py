from __future__ import annotations

from dedupper.mapping import infer_mapping
from dedupper.parsers import parse_delimited


def test_known_dutch_and_english_headers_are_mapped() -> None:
    payload = (
        "Titel;Publicatiejaar;Auteurs;Digital Object Identifier;Tijdschrift\n"
        "A sufficiently long scientific article title;2024;Smith, Jane;"
        "10.1234/example;Journal of Examples\n"
    ).encode()
    table = parse_delimited(payload, "file1", "input.csv", ".csv")[0]
    mapping = infer_mapping(table)

    assert mapping["title"]["selected"] == "Titel"
    assert mapping["year"]["selected"] == "Publicatiejaar"
    assert mapping["first_author"]["selected"] == "Auteurs"
    assert mapping["doi"]["selected"] == "Digital Object Identifier"
    assert mapping["journal"]["selected"] == "Tijdschrift"


def test_gibberish_title_header_is_suggested_from_values() -> None:
    payload = (
        "X34b,C2,Z9,J7\n"
        "Neural mechanisms underlying visual word recognition,2021,"
        "10.1234/word.1,Cognitive Science\n"
        "Developmental changes in language control and attention,2022,"
        "10.1234/word.2,Brain and Language\n"
        "A systematic investigation of lexical decision performance,2023,"
        "10.1234/word.3,Journal of Memory\n"
    ).encode()
    table = parse_delimited(payload, "file1", "mystery.csv", ".csv")[0]
    mapping = infer_mapping(table)

    assert mapping["title"]["selected"] == "X34b"
    assert mapping["title"]["reason"] == "Values only—please verify"
    assert mapping["year"]["selected"] == "C2"
    assert mapping["doi"]["selected"] == "Z9"


def test_abstract_header_and_long_prose_values_are_detected() -> None:
    payload = (
        "Title,Year,Abstract\n"
        "A sufficiently long scientific title,2024,"
        "\"This is a long abstract containing enough connected prose to distinguish "
        "it from the article title. It describes the background, methods, results, "
        "and conclusion of a scientific investigation in several complete sentences.\"\n"
    ).encode()
    table = parse_delimited(payload, "file1", "abstracts.csv", ".csv")[0]
    mapping = infer_mapping(table)

    assert mapping["title"]["selected"] == "Title"
    assert mapping["abstract"]["selected"] == "Abstract"


def test_publication_type_header_and_values_are_detected() -> None:
    payload = (
        "Title,Authors,DOI,Publication Type\n"
        "First sufficiently long scientific title,Smith,10.1234/a,Conference Abstract\n"
        "Second sufficiently long scientific title,Jones,10.1234/b,Journal Article\n"
    ).encode()
    table = parse_delimited(payload, "file1", "types.csv", ".csv")[0]
    mapping = infer_mapping(table)

    assert mapping["publication_type"]["selected"] == "Publication Type"


def test_richer_bibliographic_headers_are_detected() -> None:
    payload = (
        "Title,Authors,Pages,Article Number,ISSN,ISBN\n"
        '"A long and distinctive scientific article title",'
        '"Smith J; Jones P",123-129,e45678,1234-5678,978-3-16-148410-0\n'
    ).encode()
    table = parse_delimited(payload, "file1", "metadata.csv", ".csv")[0]
    mapping = infer_mapping(table)

    assert mapping["authors"]["selected"] == "Authors"
    assert mapping["first_author"]["selected"] == "Authors"
    assert mapping["pages"]["selected"] == "Pages"
    assert mapping["article_number"]["selected"] == "Article Number"
    assert mapping["issn"]["selected"] == "ISSN"
    assert mapping["isbn"]["selected"] == "ISBN"
