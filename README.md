# Feishu Bitable Skill

Python-only utilities for Feishu/Lark Bitable and Sheets automation. The project is designed for direct `import` in scripts, Cursor Skills, and RPA tools.

## Highlights

- Read Bitable records by `app_token`, table name, and optional view.
- Convert Feishu nested `fields` into `columns`, `rows`, and `rows_by_record_id`.
- Preserve `record_id` for reliable readback and updates.
- Expand linked-record fields to the linked table primary value, so fields like `所属活动` can display `日常销售` instead of `rec...`.
- Read raw linked-record `record_id` values from a linked-record column when automation needs IDs instead of display text.
- Filter date columns with `Today`, `Yesterday`, `CurrentMonth`, `LastMonth`, exact dates, or inclusive date ranges.
- Update and create records by field names with schema validation and value normalization.
- Default to dry-run for writes; real writes require `confirm_write=True`.
- Cache tenant tokens, table/view metadata, and schemas in the current Python process.
- Include a no-type-annotation RPA version for automation platforms that struggle with typed Python.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `feishu_bitable_utils.py` | Main Bitable utility with type hints. Best for Python projects and Cursor Skill use. |
| `feishu_bitable_utils_rpa.py` | RPA-friendly Bitable utility without function type annotations. |
| `feishu_sheets_utils_rpa.py` | Feishu Sheets helper for row lookup and update by sheet name and column name. |
| `SKILL.md` | Cursor Skill instructions and implementation constraints. |
| `.env.example` | Local credential template. |
| `debug/` | Small local demos and sample API payloads. |

## Requirements

- Python 3.7+
- `requests`

Install the only runtime dependency if your environment does not already provide it:

```bash
pip install requests
```

## Configuration

Create a local `.env` file next to the Python files:

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

`.env` is ignored by Git. Do not commit app secrets, tenant tokens, authorization headers, cookies, or exported browser state.

Bitable targets are business parameters, not global configuration. Pass them explicitly when calling functions:

```python
app_token = "base_xxx"
table_name = "数据表"
view_name = "默认视图"
```

## Quick Start

```python
from feishu_bitable_utils import query_records_by_time, query_records_by_ids

result = query_records_by_time(
    app_token=app_token,
    table_name=table_name,
    time_column="申请时间",
    condition="Today",
    query_columns=["状态", "名称", "数量"],
)

print(result.columns)
print(result.rows)
print(result.rows_by_record_id)

selected = query_records_by_ids(
    app_token=app_token,
    table_name=table_name,
    record_ids=["recxxxx"],
    query_columns=["状态", "名称"],
)
```

When `query_records_by_ids` cannot read one of the requested record IDs, it skips that ID and appends `{"record_id": "...", "error": "..."}` to `result.errors`.

## Linked Records

Linked-record fields are expanded automatically when records are read through `query_records_by_time` or `query_records_by_ids`.

For example, if `赠品配置表.所属活动` links to `活动周期表.活动名称`, the returned row displays:

```python
["recvm8boBWJE1l", "日常销售"]
```

If a cell links to multiple records, the value is returned as a list of linked primary-field values.

If you need the raw linked record IDs instead of display values, query the linked column directly:

```python
from feishu_bitable_utils import query_linked_record_ids_by_records

linked = query_linked_record_ids_by_records(
    app_token=app_token,
    table_name=table_name,
    record_ids=["recxxxx"],
    column_name="关联活动机制",
)

# {"recxxxx": ["recyyyy"]}
```

## Update Records

Update helpers are dry-run by default. This returns a preview and does not write to Feishu:

```python
from feishu_bitable_utils import update_record_by_names

preview = update_record_by_names(
    app_token=app_token,
    table_name=table_name,
    record_id="recxxxx",
    columns=["状态"],
    values=["已完成"],
)
```

Set `confirm_write=True` to perform the write. For high-frequency RPA jobs, set `readback=False` to skip the extra post-update read:

```python
result = update_record_by_names(
    app_token=app_token,
    table_name=table_name,
    record_id="recxxxx",
    columns=["状态"],
    values=["已完成"],
    confirm_write=True,
    readback=False,
)
```

## Create Records

```python
from feishu_bitable_utils import create_records_by_names

result = create_records_by_names(
    app_token=app_token,
    table_name=table_name,
    columns=["名称", "数量", "状态"],
    rows=[
        ["示例名称", 1, "待处理"],
        ["示例名称 2", 2, "待处理"],
    ],
    confirm_write=True,
)
```

Multiple rows use Feishu `batch_create` automatically, with a default batch size of 500. Pass `batch_size=1` to create records one by one.

## Sheets Helpers

`feishu_sheets_utils_rpa.py` is for regular Feishu Sheets, not Bitable. It uses the first row as headers and locates columns by name.

```python
from feishu_sheets_utils_rpa import (
    query_sheet_all_rows,
    query_sheet_row_by_column,
    query_sheet_rows_by_column,
    update_sheet_row_by_column,
)

all_rows = query_sheet_all_rows(
    spreadsheet_token=spreadsheet_token,
    sheet_name="示例Sheet",
)

matched = query_sheet_row_by_column(
    spreadsheet_token=spreadsheet_token,
    sheet_name="示例Sheet",
    match_column="主播名",
    match_value="示例主播",
)

matched_rows = query_sheet_rows_by_column(
    spreadsheet_token=spreadsheet_token,
    sheet_name="示例Sheet",
    match_column="主播名",
    match_value="示例主播",
)

updated = update_sheet_row_by_column(
    spreadsheet_token=spreadsheet_token,
    sheet_name="示例Sheet",
    match_column="主播名",
    match_value="示例主播",
    update_columns=["对账日期"],
    update_values=["2026-05-12"],
    confirm_write=True,
)
```

Sheets rich-text cells returned as objects, such as `{"type": "url", "text": "...", "link": "..."}`, are normalized to readable values. Duplicate headers are preserved in `row_dicts` with suffixes such as `签约人_2`.

## Public APIs

### Bitable

| Function | Description |
| --- | --- |
| `list_bitable_tables(app_token)` | List Bitable tables and views. |
| `query_records_by_time(...)` | Read records, optionally filter by date, and return a `ListReadResult`. |
| `query_records_by_ids(...)` | Read records by Feishu `record_id` list, skip missing IDs, and return a `ListReadResult`. |
| `dry_run_update_by_names(...)` | Preview a field-name-based update. |
| `update_record_by_names(...)` | Update one record after validation. Defaults to dry-run. |
| `create_records_by_names(...)` | Create one or more records after validation. Defaults to dry-run. |
| `query_linked_record_ids_by_records(...)` | Read a linked-record column and return linked `record_id` values by source record. |
| `list_result_to_csv_string(result)` | Export a `ListReadResult` to CSV text. |
| `clear_feishu_cache()` | Clear in-process token/table/view/schema caches. |

### Sheets

| Function | Description |
| --- | --- |
| `list_sheets(spreadsheet_token)` | List sheet names and IDs. |
| `query_sheet_all_rows(...)` | Return all rows from a sheet by sheet name. |
| `query_sheet_row_by_column(...)` | Return the first row where a column exactly matches a value. |
| `query_sheet_rows_by_column(...)` | Return all rows where a column exactly matches a value. |
| `update_sheet_row_by_column(...)` | Update columns in the first matched row. Defaults to dry-run. |
| `clear_feishu_sheets_cache()` | Clear in-process Sheets caches. |

## Safety Notes

- Write helpers validate schema, field writability, and value types before calling write APIs.
- `record_id` is treated as Feishu's technical primary key and is never written back as a normal field.
- Read and write helpers resolve table/view names at runtime; pass `table_id` or `view_id` when names are duplicated.
- If a long-running process sees stale table/view/schema data after a Feishu-side change, call `clear_feishu_cache()`.
- Use `feishu_bitable_utils_rpa.py` when an RPA platform cannot parse Python type annotations.

## Development

Run a syntax check before publishing changes:

```bash
python3 -m py_compile feishu_bitable_utils.py feishu_bitable_utils_rpa.py feishu_sheets_utils_rpa.py
```

The `debug/` folder contains small examples for flattening records and dry-run update reports.
