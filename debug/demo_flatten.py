"""示例：MCP 拉取 records + field list 后，转二维表。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feishu_bitable_utils import (  # noqa: E402
    TableViewRef,
    ensure_view_schema,
    flatten_records,
    records_to_list_result,
    list_result_to_csv_string,
)


def main() -> None:
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "sample_fields.json"), encoding="utf-8") as f:
        field_items = json.load(f)
    with open(os.path.join(here, "sample_records.json"), encoding="utf-8") as f:
        raw = json.load(f)
    ref = TableViewRef(app_token="APP", table_name="样例", table_id="tbl0")
    schema = ensure_view_schema(ref, field_items)
    res = records_to_list_result(raw, schema)
    print("columns:", res.columns)
    print("rows:", res.rows)
    print("csv:\n", list_result_to_csv_string(res))
    print("flat:", json.dumps(flatten_records(raw, schema), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
