import io
import pytest
from typing import Iterator

from dlt.common import pendulum, json
from dlt.common.typing import AnyFun
# from dlt.destinations.postgres import capabilities
from dlt.destinations.redshift import capabilities as redshift_caps
from dlt.common.data_writers.escape import escape_redshift_identifier, escape_bigquery_identifier, escape_redshift_literal, escape_postgres_literal, escape_duckdb_literal
from dlt.common.data_writers.writers import DataWriter, InsertValuesWriter, JsonlWriter

from tests.common.utils import load_json_case, row_to_column_schemas

ALL_LITERAL_ESCAPE = [escape_redshift_literal, escape_postgres_literal, escape_duckdb_literal]


@pytest.fixture
def insert_writer() -> Iterator[DataWriter]:
    with io.StringIO() as f:
        yield InsertValuesWriter(f, caps=redshift_caps())


@pytest.fixture
def jsonl_writer() -> Iterator[DataWriter]:
    with io.BytesIO() as f:
        yield JsonlWriter(f)


def test_simple_insert_writer(insert_writer: DataWriter) -> None:
    rows = load_json_case("simple_row")
    insert_writer.write_all(row_to_column_schemas(rows[0]), rows)
    lines = insert_writer._f.getvalue().split("\n")
    assert lines[0].startswith("INSERT INTO {}")
    assert '","'.join(rows[0].keys()) in lines[0]
    assert lines[1] == "VALUES"
    assert len(lines) == 4


def test_simple_jsonl_writer(jsonl_writer: DataWriter) -> None:
    rows = load_json_case("simple_row")
    jsonl_writer.write_all(None, rows)
    # remove b'' at the end
    lines = jsonl_writer._f.getvalue().split(b"\n")
    assert lines[-1] == b''
    assert len(lines) == 3


def test_bytes_insert_writer(insert_writer: DataWriter) -> None:
    rows = [{"bytes": b"bytes"}]
    insert_writer.write_all(row_to_column_schemas(rows[0]), rows)
    lines = insert_writer._f.getvalue().split("\n")
    assert lines[2] == "(from_hex('6279746573'));"


def test_datetime_insert_writer(insert_writer: DataWriter) -> None:
    rows = [{"datetime": pendulum.from_timestamp(1658928602.575267)}]
    insert_writer.write_all(row_to_column_schemas(rows[0]), rows)
    lines = insert_writer._f.getvalue().split("\n")
    assert lines[2] == "('2022-07-27T13:30:02.575267+00:00');"


def test_date_insert_writer(insert_writer: DataWriter) -> None:
    rows = [{"date": pendulum.date(1974, 8, 11)}]
    insert_writer.write_all(row_to_column_schemas(rows[0]), rows)
    lines = insert_writer._f.getvalue().split("\n")
    assert lines[2] == "('1974-08-11');"


@pytest.mark.skip("not implemented")
def test_unicode_insert_writer_postgres() -> None:
    # implements tests for the postgres encoding -> same cases as redshift
    pass


def test_unicode_insert_writer(insert_writer: DataWriter) -> None:
    rows = load_json_case("weird_rows")
    insert_writer.write_all(row_to_column_schemas(rows[0]), rows)
    lines = insert_writer._f.getvalue().split("\n")
    assert lines[2].endswith("', NULL''); DROP SCHEMA Public --'),")
    assert lines[3].endswith("'イロハニホヘト チリヌルヲ ''ワカヨタレソ ツネナラム'),")
    assert lines[4].endswith("'ऄअआइ''ईउऊऋऌऍऎए'),")
    assert lines[5].endswith("hello\\nworld\t\t\t\\r\x06'),")


def test_string_literal_escape() -> None:
    assert escape_redshift_literal(", NULL'); DROP TABLE --") == "', NULL''); DROP TABLE --'"
    assert escape_redshift_literal(", NULL');\n DROP TABLE --") == "', NULL'');\\n DROP TABLE --'"
    assert escape_redshift_literal(", NULL);\n DROP TABLE --") == "', NULL);\\n DROP TABLE --'"
    assert escape_redshift_literal(", NULL);\\n DROP TABLE --\\") == "', NULL);\\\\n DROP TABLE --\\\\'"
    # assert escape_redshift_literal(b'hello_word') == "\\x68656c6c6f5f776f7264"


@pytest.mark.parametrize("escaper", ALL_LITERAL_ESCAPE)
def test_string_complex_escape(escaper: AnyFun) -> None:
    doc = {"complex":[1,2,3,"a"], "link": "?commen\ntU\nrn=urn%3Ali%3Acomment%3A%28acti\0xA \0x0 \\vity%3A69'08444473\n\n551163392%2C6n \r \x8e9085"}
    escaped = escaper(doc)
    # should be same as string escape
    if escaper == escape_redshift_literal:
        assert escaped == f"json_parse({escaper(json.dumps(doc))})"
    else:
        assert escaped == escaper(json.dumps(doc))


def test_identifier_escape() -> None:
    assert escape_redshift_identifier(", NULL'); DROP TABLE\" -\\-") == '", NULL\'); DROP TABLE"" -\\\\-"'


def test_identifier_escape_bigquery() -> None:
    assert escape_bigquery_identifier(", NULL'); DROP TABLE\"` -\\-") == '`, NULL\'); DROP TABLE"\\` -\\\\-`'


def test_string_literal_escape_unicode() -> None:
    # test on some unicode characters
    assert escape_redshift_literal(", NULL);\n DROP TABLE --") == "', NULL);\\n DROP TABLE --'"
    assert escape_redshift_literal("イロハニホヘト チリヌルヲ ワカヨタレソ ツネナラム") == "'イロハニホヘト チリヌルヲ ワカヨタレソ ツネナラム'"
    assert escape_redshift_identifier("ąćł\"") == '"ąćł"""'
    assert escape_redshift_identifier("イロハニホヘト チリヌルヲ \"ワカヨタレソ ツネナラム") == '"イロハニホヘト チリヌルヲ ""ワカヨタレソ ツネナラム"'
