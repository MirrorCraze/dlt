import pytest
from copy import deepcopy
from typing import Any
from hexbytes import HexBytes

from dlt.common import Wei, Decimal, pendulum, json
from dlt.common.json import custom_pua_decode
from dlt.common.schema import Schema, utils
from dlt.common.schema.exceptions import CannotCoerceColumnException, CannotCoerceNullException, ParentTableNotFoundException, TablePropertiesConflictException
from tests.common.utils import load_json_case


@pytest.fixture
def schema() -> Schema:
    return Schema("event")


def test_get_preferred_type(schema: Schema) -> None:
    _add_preferred_types(schema)

    assert "timestamp" in map(lambda m: m[1], schema._compiled_preferred_types)
    assert "double" in map(lambda m: m[1], schema._compiled_preferred_types)

    assert schema.get_preferred_type("timestamp") == "timestamp"
    assert schema.get_preferred_type("value") == "wei"
    assert schema.get_preferred_type("timestamp_confidence_entity") == "double"
    assert schema.get_preferred_type("_timestamp") is None


def test_map_column_preferred_type(schema: Schema) -> None:
    _add_preferred_types(schema)
    # preferred type match
    assert schema._infer_column_type(1278712.0, "confidence") == "double"
    # preferred type can be coerced
    assert schema._infer_column_type(1278712, "confidence") == "double"
    assert schema._infer_column_type("18271", "confidence") == "double"

    # timestamp from coercable type
    assert schema._infer_column_type(18271, "timestamp") == "timestamp"
    assert schema._infer_column_type("18271.11", "timestamp") == "timestamp"
    assert schema._infer_column_type("2022-05-10T00:54:38.237000+00:00", "timestamp") == "timestamp"

    # value should be wei
    assert schema._infer_column_type(" 0xfe ", "value") == "wei"
    # number should be decimal
    assert schema._infer_column_type(" -0.821 ", "number") == "decimal"

    # if value cannot be coerced, column type still preferred types
    assert schema._infer_column_type(False, "value") == "wei"
    assert schema._infer_column_type("AA", "confidence") == "double"

    # skip preferred
    assert schema._infer_column_type(False, "value", skip_preferred=True) == "bool"
    assert schema._infer_column_type("AA", "confidence", skip_preferred=True) == "text"



def test_map_column_type(schema: Schema) -> None:
    # default mappings
    assert schema._infer_column_type("18271.11", "_column_name") == "text"
    assert schema._infer_column_type(["city"], "_column_name") == "complex"
    assert schema._infer_column_type(0x72, "_column_name") == "bigint"
    assert schema._infer_column_type(0x72, "_column_name") == "bigint"
    assert schema._infer_column_type(b"bytes str", "_column_name") == "binary"
    assert schema._infer_column_type(b"bytes str", "_column_name") == "binary"
    assert schema._infer_column_type(HexBytes(b"bytes str"), "_column_name") == "binary"


def test_map_column_type_complex(schema: Schema) -> None:
    # complex type mappings
    v_list = [1, 2, "3", {"complex": True}]
    v_dict = {"list": [1, 2], "str": "complex"}
    # complex types must be cast to text
    assert schema._infer_column_type(v_list, "cx_value") == "complex"
    assert schema._infer_column_type(v_dict, "cx_value") == "complex"


def test_coerce_row(schema: Schema) -> None:
    _add_preferred_types(schema)
    timestamp_float = 78172.128
    timestamp_str = "1970-01-01T21:42:52.128000+00:00"
    # add new column with preferred
    row_1 = {"timestamp": timestamp_float, "confidence": "0.1", "value": "0xFF", "number": Decimal("128.67")}
    new_row_1, new_table = schema.coerce_row("event_user", None, row_1)
    # convert columns to list, they must correspond to the order of fields in row_1
    new_columns = list(new_table["columns"].values())
    assert new_columns[0]["data_type"] == "timestamp"
    assert new_columns[0]["name"] == "timestamp"
    assert new_columns[1]["data_type"] == "double"
    assert new_columns[2]["data_type"] == "wei"
    assert new_columns[3]["data_type"] == "decimal"
    # also rows values should be coerced (confidence)
    assert new_row_1 == {"timestamp": pendulum.parse(timestamp_str), "confidence": 0.1, "value": 255, "number": Decimal("128.67")}

    # update schema
    schema.update_schema(new_table)

    # no coercion on confidence
    row_2 = {"timestamp": timestamp_float, "confidence": 0.18721}
    new_row_2, new_table = schema.coerce_row("event_user", None, row_2)
    assert new_table is None
    assert new_row_2 == {"timestamp": pendulum.parse(timestamp_str), "confidence": 0.18721}

    # all coerced
    row_3 = {"timestamp": "78172.128", "confidence": 1}
    new_row_3, new_table = schema.coerce_row("event_user", None, row_3)
    assert new_table is None
    assert new_row_3 == {"timestamp": pendulum.parse(timestamp_str), "confidence": 1.0}

    # create variant column where variant column will have preferred
    # variant column should not be checked against preferred
    row_4 = {"timestamp": "78172.128", "confidence": "STR"}
    new_row_4, new_table = schema.coerce_row("event_user", None, row_4)
    new_columns = list(new_table["columns"].values())
    assert new_columns[0]["data_type"] == "text"
    assert new_columns[0]["name"] == "confidence__v_text"
    assert new_row_4 == {"timestamp": pendulum.parse(timestamp_str), "confidence__v_text": "STR"}
    schema.update_schema(new_table)

    # add against variant
    new_row_4, new_table = schema.coerce_row("event_user", None, row_4)
    assert new_table is None
    assert new_row_4 == {"timestamp": pendulum.parse(timestamp_str), "confidence__v_text": "STR"}

    # another variant
    new_row_5, new_table = schema.coerce_row("event_user", None, {"confidence": False})
    new_columns = list(new_table["columns"].values())
    assert new_columns[0]["data_type"] == "bool"
    assert new_columns[0]["name"] == "confidence__v_bool"
    assert new_row_5 == {"confidence__v_bool": False}
    schema.update_schema(new_table)

    # variant column clashes with existing column - create new_colbool_v_binary column that would be created for binary variant, but give it a type datetime
    _, new_table = schema.coerce_row("event_user", None, {"new_colbool": False, "new_colbool__v_timestamp": b"not fit"})
    schema.update_schema(new_table)
    with pytest.raises(CannotCoerceColumnException) as exc_val:
        # now pass the binary that would create binary variant - but the column is occupied by text type
        schema.coerce_row("event_user", None, {"new_colbool": pendulum.now()})
    assert exc_val.value.table_name == "event_user"
    assert exc_val.value.column_name == "new_colbool__v_timestamp"
    assert exc_val.value.from_type == "timestamp"
    assert exc_val.value.to_type == "binary"
    # this must be datatime instance
    assert not isinstance(exc_val.value.coerced_value, bytes)


def test_coerce_row_iso_timestamp(schema: Schema) -> None:
    _add_preferred_types(schema)
    timestamp_str = "2022-05-10T00:17:15.300000+00:00"
    # will generate timestamp type
    row_1 = {"timestamp": timestamp_str}
    _, new_table = schema.coerce_row("event_user", None, row_1)
    new_columns = list(new_table["columns"].values())
    assert new_columns[0]["data_type"] == "timestamp"
    assert new_columns[0]["name"] == "timestamp"
    schema.update_schema(new_table)

    # will coerce float
    row_2 = {"timestamp": 78172.128}
    _, new_table = schema.coerce_row("event_user", None, row_2)
    # no new columns
    assert new_table is None

    # will generate variant
    row_3 = {"timestamp": "übermorgen"}
    _, new_table = schema.coerce_row("event_user", None, row_3)
    new_columns = list(new_table["columns"].values())
    assert new_columns[0]["name"] == "timestamp__v_text"


def test_coerce_complex_variant(schema: Schema) -> None:
    # create two columns to which complex type cannot be coerced
    row = {"floatX": 78172.128, "confidenceX": 1.2, "strX": "STR"}
    new_row, new_table = schema.coerce_row("event_user", None, row)
    assert new_row == row
    schema.update_schema(new_table)

    # add two more complex columns that should be coerced to text
    v_list = [1, 2, "3", {"complex": True}]
    v_dict = {"list": [1, 2], "str": "complex"}
    c_row = {"c_list": v_list, "c_dict": v_dict}
    c_new_row, c_new_table = schema.coerce_row("event_user", None, c_row)
    c_new_columns = list(c_new_table["columns"].values())
    assert c_new_columns[0]["name"] == "c_list"
    assert c_new_columns[0]["data_type"] == "complex"
    assert c_new_columns[1]["name"] == "c_dict"
    assert c_new_columns[1]["data_type"] == "complex"
    assert c_new_row["c_list"] == v_list
    schema.update_schema(c_new_table)

    # add same row again
    c_new_row, c_new_table = schema.coerce_row("event_user", None, c_row)
    assert c_new_table is None
    assert c_new_row["c_dict"] == v_dict

    # add complex types on the same columns
    c_row_v = {"floatX": v_list, "confidenceX": v_dict, "strX": v_dict}
    # expect two new variant columns to be created
    c_new_row_v, c_new_table_v = schema.coerce_row("event_user", None, c_row_v)
    c_new_columns_v = list(c_new_table_v["columns"].values())
    # two new variant columns added
    assert len(c_new_columns_v) == 2
    assert c_new_columns_v[0]["name"] == "floatX__v_complex"
    assert c_new_columns_v[1]["name"] == "confidenceX__v_complex"
    assert c_new_row_v["floatX__v_complex"] == v_list
    assert c_new_row_v["confidenceX__v_complex"] == v_dict
    assert c_new_row_v["strX"] == json.dumps(v_dict)
    schema.update_schema(c_new_table_v)

    # add that row again
    c_row_v = {"floatX": v_list, "confidenceX": v_dict, "strX": v_dict}
    c_new_row_v, c_new_table_v = schema.coerce_row("event_user", None, c_row_v)
    assert c_new_table_v is None
    assert c_new_row_v["floatX__v_complex"] == v_list
    assert c_new_row_v["confidenceX__v_complex"] == v_dict
    assert c_new_row_v["strX"] == json.dumps(v_dict)


def test_supports_variant_pua_decode(schema: Schema) -> None:
    rows = load_json_case("pua_encoded_row")
    normalized_row = list(schema.normalize_data_item(schema, rows[0], "0912uhj222", "event"))
    # pua encoding still present
    assert normalized_row[0][1]["wad"].startswith("")
    # decode pua
    decoded_row = {k: custom_pua_decode(v) for k,v in normalized_row[0][1].items()}
    assert isinstance(decoded_row["wad"], Wei)
    c_row, new_table = schema.coerce_row("eth", None, decoded_row)
    assert c_row["wad__v_str"] == str(2**256-1)
    assert new_table["columns"]["wad__v_str"]["data_type"] == "text"


def test_supports_variant(schema: Schema) -> None:
    rows = [{"evm": Wei.from_int256(2137*10**16, decimals=18)}, {"evm": Wei.from_int256(2**256-1)}]
    normalized_rows = []
    for row in rows:
        normalized_rows.extend(schema.normalize_data_item(schema, row, "128812.2131", "event"))
    # row 1 contains Wei
    assert isinstance(normalized_rows[0][1]["evm"], Wei)
    assert normalized_rows[0][1]["evm"] == Wei("21.37")
    # row 2 contains Wei
    assert "evm" in normalized_rows[1][1]
    assert isinstance(normalized_rows[1][1]["evm"], Wei)
    assert normalized_rows[1][1]["evm"] == 2**256-1
    # coerce row
    c_row, new_table = schema.coerce_row("eth", None, normalized_rows[0][1])
    assert isinstance(c_row["evm"], Wei)
    assert c_row["evm"] == Wei("21.37")
    assert new_table["columns"]["evm"]["data_type"] == "wei"
    schema.update_schema(new_table)
    # coerce row that should expand to variant
    c_row, new_table = schema.coerce_row("eth", None, normalized_rows[1][1])
    assert isinstance(c_row["evm__v_str"], str)
    assert c_row["evm__v_str"] == str(2**256-1)
    assert new_table["columns"]["evm__v_str"]["data_type"] == "text"


def test_supports_recursive_variant(schema: Schema) -> None:


    class RecursiveVariant(int):
        # provide __call__ for SupportVariant
        def __call__(self) -> Any:
            if self == 1:
                return self
            else:
                return ("div2", RecursiveVariant(self // 2))


    row = {"rv": RecursiveVariant(8)}
    c_row, new_table = schema.coerce_row("rec_variant", None, row)
    # this variant keeps expanding until the value is 1, we start from 8 so there are log2(8) == 3 divisions
    col_name = "rv" + "__v_div2"*3
    assert c_row[col_name] == 1
    assert new_table["columns"][col_name]["data_type"] == "bigint"


def test_supports_variant_autovariant_conflict(schema: Schema) -> None:

    class PureVariant(int):
        def __init__(self, v: Any) -> None:
            self.v = v

        # provide __call__ for SupportVariant
        def __call__(self) -> Any:
            if isinstance(self.v, int):
                return self.v
            if isinstance(self.v, float):
                return ("text", self.v)

    assert issubclass(PureVariant,int)
    rows = [{"pv": PureVariant(3377)}, {"pv": PureVariant(21.37)}]
    normalized_rows = []
    for row in rows:
        normalized_rows.extend(schema.normalize_data_item(schema, row, "128812.2131", "event"))
    assert normalized_rows[0][1]["pv"]() == 3377
    assert normalized_rows[1][1]["pv"]() == ("text", 21.37)
    # first normalized row fits into schema (pv is int)
    _, new_table = schema.coerce_row("pure_variant", None, normalized_rows[0][1])
    schema.update_schema(new_table)
    assert new_table["columns"]["pv"]["data_type"] == "bigint"
    _, new_table = schema.coerce_row("pure_variant", None, normalized_rows[1][1])
    # we trick the normalizer to create text variant but actually provide double value
    schema.update_schema(new_table)
    assert new_table["columns"]["pv__v_text"]["data_type"] == "double"

    # second row does not coerce: there's `pv__v_bool` field in it of type double but we already have a column that is text
    with pytest.raises(CannotCoerceColumnException) as exc_val:
        _, new_table = schema.coerce_row("pure_variant", None, {"pv": "no double"})
    assert exc_val.value.column_name == "pv__v_text"
    assert exc_val.value.from_type == "text"
    assert exc_val.value.to_type == "double"
    assert exc_val.value.coerced_value == "no double"


def test_corece_new_null_value(schema: Schema) -> None:
    row = {"timestamp": None}
    new_row, new_table = schema.coerce_row("event_user", None, row)
    assert "timestamp" not in new_row
    # columns were not created
    assert new_table is None


def test_coerce_null_value_over_existing(schema: Schema) -> None:
    row = {"timestamp": 82178.1298812}
    new_row, new_table = schema.coerce_row("event_user", None, row)
    schema.update_schema(new_table)
    row = {"timestamp": None}
    new_row, _ = schema.coerce_row("event_user", None, row)
    assert "timestamp" not in new_row


def test_corece_null_value_over_not_null(schema: Schema) -> None:
    row = {"timestamp": 82178.1298812}
    _, new_table = schema.coerce_row("event_user", None, row)
    schema.update_schema(new_table)
    schema.get_table_columns("event_user")["timestamp"]["nullable"] = False
    row = {"timestamp": None}
    with pytest.raises(CannotCoerceNullException):
        schema.coerce_row("event_user", None, row)


def test_infer_with_autodetection(schema: Schema) -> None:
    c = schema._infer_column("ts", pendulum.now().timestamp())
    assert c["data_type"] == "timestamp"
    schema._type_detections = []
    c = schema._infer_column("ts", pendulum.now().timestamp())
    assert c["data_type"] == "double"


def test_update_schema_parent_missing(schema: Schema) -> None:
    tab1 = utils.new_table("tab1", parent_table_name="tab_parent")
    # tab_parent is missing in schema
    with pytest.raises(ParentTableNotFoundException) as exc_val:
        schema.update_schema(tab1)
    assert exc_val.value.parent_table_name == "tab_parent"
    assert exc_val.value.table_name == "tab1"


def test_update_schema_table_prop_conflict(schema: Schema) -> None:
    # parent table conflict
    tab1 = utils.new_table("tab1", write_disposition="append")
    tab_parent = utils.new_table("tab_parent", write_disposition="replace")
    schema.update_schema(tab1)
    schema.update_schema(tab_parent)
    tab1_u1 = deepcopy(tab1)
    tab1_u1["parent"] = "tab_parent"
    with pytest.raises(TablePropertiesConflictException) as exc_val:
        schema.update_schema(tab1_u1)
    assert exc_val.value.table_name == "tab1"
    assert exc_val.value.prop_name == "parent"
    assert exc_val.value.val1 is None
    assert exc_val.value.val2 == "tab_parent"

    # write disposition conflict
    tab1_u2 = deepcopy(tab1)
    tab1_u2["write_disposition"] = "merge"
    with pytest.raises(TablePropertiesConflictException) as exc_val:
        schema.update_schema(tab1_u2)
    assert exc_val.value.table_name == "tab1"
    assert exc_val.value.prop_name == "write_disposition"
    assert exc_val.value.val1 == "append"
    assert exc_val.value.val2 == "merge"
    # without write disposition will merge
    del tab1_u2["write_disposition"]
    schema.update_schema(tab1_u2)
    # tab1 no write disposition, table update has write disposition
    tab1["write_disposition"] = None
    tab1_u2["write_disposition"] = "merge"
    # this will not merge
    with pytest.raises(TablePropertiesConflictException) as exc_val:
        schema.update_schema(tab1_u2)
    # both write dispositions are None
    tab1_u2["write_disposition"] = None
    schema.update_schema(tab1_u2)


def test_update_schema_column_conflict(schema: Schema) -> None:
    tab1 = utils.new_table("tab1", write_disposition="append", columns=[
        {"name": "col1", "data_type": "text", "nullable": False},
    ])
    schema.update_schema(tab1)
    tab1_u1 = deepcopy(tab1)
    # simulate column that had other datatype inferred
    tab1_u1["columns"]["col1"]["data_type"] = "bool"
    with pytest.raises(CannotCoerceColumnException) as exc_val:
        schema.update_schema(tab1_u1)
    assert exc_val.value.column_name == "col1"
    assert exc_val.value.from_type == "bool"
    assert exc_val.value.to_type == "text"
    # whole column mismatch
    assert exc_val.value.coerced_value is None


def _add_preferred_types(schema: Schema) -> None:
    schema._settings["preferred_types"] = {}
    schema._settings["preferred_types"]["timestamp"] = "timestamp"
    # any column with confidence should be float
    schema._settings["preferred_types"]["re:confidence"] = "double"
    # value should be wei
    schema._settings["preferred_types"]["value"] = "wei"
    # number should be decimal
    schema._settings["preferred_types"]["re:^number$"] = "decimal"

    schema._compile_settings()


def test_autodetect_convert_type(schema: Schema) -> None:
    # add to wei to float converter
    schema._type_detections.append("wei_to_double")
    row = {"evm": Wei(1)}
    c_row, new_table = schema.coerce_row("eth", None, row)
    assert c_row["evm"] == 1.0
    assert isinstance(c_row["evm"], float)
    assert new_table["columns"]["evm"]["data_type"] == "double"
    schema.update_schema(new_table)
    # add another row
    row = {"evm": Wei("21.37")}
    c_row, new_table = schema.coerce_row("eth", None, row)
    assert new_table is None
    assert c_row["evm"] == 21.37
    assert isinstance(c_row["evm"], float)

    # wei are converted to float before variants are generated
    row = {"evm": Wei.from_int256(2**256)}
    c_row, new_table = schema.coerce_row("eth", None, row)
    assert new_table is None
    assert c_row["evm"] == float(2**256)
    assert isinstance(c_row["evm"], float)

    # make sure variants behave the same


    class AlwaysWei(Decimal):
        def __call__(self) -> Any:
            return ("up", Wei(self))


    # create new column
    row = {"evm2": AlwaysWei(22)}
    c_row, new_table = schema.coerce_row("eth", None, row)
    assert c_row["evm2__v_up"] == 22.0
    assert isinstance(c_row["evm2__v_up"], float)
    assert new_table["columns"]["evm2__v_up"]["data_type"] == "double"
    schema.update_schema(new_table)
    # add again
    row = {"evm2": AlwaysWei(22.2)}
    c_row, new_table = schema.coerce_row("eth", None, row)
    assert c_row["evm2__v_up"] == 22.2
    assert isinstance(c_row["evm2__v_up"], float)
    assert new_table is None
    # create evm2 column
    row = {"evm2": 22.1}
    _, new_table = schema.coerce_row("eth", None, row)
    assert new_table["columns"]["evm2"]["data_type"] == "double"
    schema.update_schema(new_table)
    # and add variant again
    row = {"evm2": AlwaysWei(22.2)}
    # and this time variant will not be expanded
    # because the "evm2" column already has a type so it goes directly into double as a normal coercion
    c_row, new_table = schema.coerce_row("eth", None, row)
    assert c_row["evm2"] == 22.2
    assert isinstance(c_row["evm2"], float)

