"""示例：从列表回填到归一化 patch 与 dry-run 报告（不真实调用 API）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feishu_bitable_utils import (  # noqa: E402
    TableViewRef,
    build_patches_from_rows_by_id,
    dry_run_update,
    ensure_view_schema,
    format_dry_run_text,
)


def main() -> None:
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "sample_fields.json"), encoding="utf-8") as f:
        field_items = json.load(f)
    ref = TableViewRef(app_token="APP", table_name="样例", table_id="tbl0")
    schema = ensure_view_schema(ref, field_items)
    raw = {
        "record_id": "recXXXX01",
        "fields": {"fldQty": 1, "fldName": "旧名"},
    }
    patches = build_patches_from_rows_by_id(
        write_back_columns=["数量", "名称"],
        write_back_rows={"recXXXX01": [42, "新名"]},
        schema=schema.fields,
    )
    r = dry_run_update(patches, [raw], schema.fields)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("---\n" + format_dry_run_text(r))


if __name__ == "__main__":
    main()
