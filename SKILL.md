# 飞书多维表处理助手

## 名称与描述

**飞书多维表处理助手**：把飞书 Bitable 返回的 `{ record_id, fields }` 转为二维列表、按 `record_id` 索引的列表、支持指定列/时间范围筛选、带类型校验的局部列回写、以及 **默认 dry-run** 的更新预演。核心实现为 **单文件 Python 模块**，便于 RPA/脚本 `import` 与版本管理。

## 实现语言

- 仅使用 **Python**；不生成 Node/TS/Go/Rust 等实现方案。

## 依赖

- 标准库：`csv`、`json`、`datetime`、`decimal`、`pathlib`（如需要）、`typing`、`dataclasses`。
- 唯一默认可用第三方库：`requests`（本核心模块不强制依赖；由调用方在直连 HTTP 时使用）。
- 默认 **不用** `pandas`、`openpyxl`、`pydantic`、`numpy`、`httpx` 等；若确需，须单独评估并征求用户同意。

## 单文件核心模块

- 文件名：`feishu_bitable_utils.py`（与本文档同目录）。
- 所有核心逻辑（类型、扁平化、schema、筛选、归一化、dry-run、报告格式、可选 CLI）均在该单文件内；**不得**将核心逻辑拆到多个库内 `.py` 再作为「核心」。
- 在线表格（Sheets）RPA 辅助文件：`feishu_sheets_utils_rpa.py`，用于普通在线表格的按列查询行和按匹配行更新单元格。
- 调试/示例仅放在 `debug/`，且只能 **import** 核心模块，不得复制核心逻辑。

## 导入与 RPA 调用

```python
from feishu_bitable_utils import (
    list_bitable_tables,
    query_records_by_time,
    query_records_by_ids,
    dry_run_update_by_names,
    update_record_by_names,
    create_records_by_names,
    query_linked_record_ids_by_records,
)
```

RPA/自动化应 **直接调用函数**；`python feishu_bitable_utils.py` 仅提示查看文件底部调用案例。

## 配置与参数边界

业务目标参数不做全局配置，调用读取/写入函数时显式传入：

```python
app_token = "base_xxx"      # 多维表 app_token，不是开放平台 App ID
table_name = "数据表"
view_name = "默认视图"
```

需要配置的是飞书开放平台创建的应用凭证：`App ID` 和 `App Secret`。在 Cursor Skill 环境中放到脚本同目录的 `.env`：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

如果把 `feishu_bitable_utils.py` 单独拷贝到 RPA 环境运行，也可以在文件顶部常量区配置：

```python
FEISHU_APP_CREDENTIALS = {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
}
```

优先级：显式函数参数 > 环境变量/`.env` > 脚本顶部 `FEISHU_APP_CREDENTIALS`。

常用调用：

```python
rows = query_records_by_time(
    app_token=app_token,
    table_name=table_name,
    time_column="申请时间",
    condition="Yesterday",
    query_columns=["状态", "名称", "数量"],
)
```

## 主接口（与计划对齐）


| 能力            | 函数                                                                                            |
| ------------- | --------------------------------------------------------------------------------------------- |
| 列出 table/view | `list_bitable_tables(app_token)`                                                              |
| 按时间查询并返回二维结构  | `query_records_by_time(app_token, table_name, time_column, ...)`                              |
| 按记录 ID 查询并返回二维结构 | `query_records_by_ids(app_token, table_name, record_ids, query_columns=...)`                  |
| 更新预演          | `dry_run_update_by_names(app_token, table_name, record_id, columns, values)`                  |
| 安全更新          | `update_record_by_names(..., confirm_write=True)`                                             |
| 安全新增          | `create_records_by_names(app_token, table_name, columns, rows, confirm_write=True)`           |
| 读取关联记录 ID     | `query_linked_record_ids_by_records(app_token, table_name, record_ids, column_name)`          |
| 在线表格按名称读取整页数据 | `query_sheet_all_rows(spreadsheet_token, sheet_name)`                                         |
| 在线表格按列查询行     | `query_sheet_row_by_column(spreadsheet_token, sheet_name, match_column, match_value)`          |
| 在线表格按列查询所有匹配行 | `query_sheet_rows_by_column(spreadsheet_token, sheet_name, match_column, match_value)`         |
| 在线表格按匹配行更新列   | `update_sheet_row_by_column(spreadsheet_token, sheet_name, match_column, match_value, ...)`    |
| 底层转换          | `flatten_records`, `records_to_list_result`, `build_time_filter_for_search`, `dry_run_update` |


## 数据结构

与 `feishu_bitable_utils.py` 中 `dataclass` 一致，包括：`TableViewRef`、`TableField`、`ViewSchema`、`ListReadResult`、`TimeFilter`、`UpdatePatch`、`NormalizedWriteValue`、`NormalizedPatch`、`ListWriteBackInput` 等。

## `record_id` 规则

- 技术主键；读入后必须保留；`rows` 可第一列为 `record_id`；`rows_by_record_id` 以 `record_id` 为 key。
- 回写时 **不可** 把 `record_id` 当普通业务字段写入 `fields`。
- 无 `record_id` 的输入行视为**新增**（本模块可构造 `fields`；真正 `create` 由 MCP/HTTP 执行）。
- `query_records_by_ids` 遇到不存在或读取失败的 `record_id` 时跳过该 ID，并把 `{record_id, error}` 放入 `result.errors`。
- 需要读取关联字段背后的原始关联记录 ID 时，使用 `query_linked_record_ids_by_records`，返回 `{源 record_id: [关联 record_id, ...]}`。
- 在线表格 Sheets 的富文本/链接对象单元格应解析为可读展示值；`row_dicts` 遇到重复表头时使用 `_2`、`_3` 后缀保留所有列。

## 表/视图

- 优先由调用方传 **表名/视图名**；`query_records_by_time`、`dry_run_update_by_names`、`update_record_by_names`、`create_records_by_names` 会内部解析 `table_id` / `view_id`。
- `table_id` / `view_id` 仍保留为可选参数，用于重名消歧或跳过解析。

## 指定列查询

- `query_columns` 中列名须存在于表结构；不可猜测列名。
- 相对时间条件：`Today` / `Yesterday` / `CurrentMonth` / `LastMonth`，优先使用飞书接口侧过滤，写法为 `operator: "is"` + `value: ["Yesterday"]`。
- 精确日期和日期范围：`exact_date`、`start_date`、`end_date` 支持 `yyyy-mm-dd`、`yyyy/mm/dd`、`yyyy.mm.dd`；范围为包含式，例如 `2026-04-26` 到 `2026-04-28` 包含三天。
- 飞书接口不支持具体日期字符串/范围比较时，工具会先拉候选记录，再按北京时间日期本地过滤。

## 局部列回写

- 更新协议：`record_id` + `columns` + `values`，列表顺序与列名一一对应。
- 新增协议：`columns` + `rows` 二维列表，不需要 `record_id`；每一行长度必须等于 `columns` 长度。多行新增会自动走飞书 `batch_create` 批量接口，默认每批 500 行；传 `batch_size=1` 可退回逐条创建。
- 写入前会读取 schema 并做字段存在性、只读字段和类型归一化校验。

## Dry-run 安全

- `dry_run_update_by_names` 永远只预演。
- `update_record_by_names` 和 `create_records_by_names` 默认 `confirm_write=False`，只返回预演；只有显式 `confirm_write=True` 才真实写入。
- `confirm_write=True` 时不再先生成 dry-run 报告；仍会做 schema、字段可写性和类型归一化校验，校验失败不写入。
- `update_record_by_names` 默认 `readback=True`，真实更新后读回本次字段；RPA 高频场景可传 `readback=False` 跳过。新增接口不额外 readback，直接返回 create/batch_create API 的记录结果。

## 字段类型与只读

- 文本/数字/日期/单多选/复选框/人员/附件等：读取见 `field_cell_display`；写入见 `normalize_value_for_write`。
- 人员、附件首版对「纯文本/本地路径」写入会报错；公式、创建/修改时间等只读类型不可写。

## 错误与返回

- 工具函数以 **返回值** 为主（`dict` 含 `ok`、`errors`、`summary`、`per_record`）；不要在库内 `print` 作为唯一输出。

## MCP 约定

- 读表/字段/记录/创建/更新：优先用当前环境 **user-lark-mcp**（如 `bitable_v1_appTable_list`、`bitable_v1_appTableField_list`、`bitable_v1_appTableRecord_search` 等）；调用前阅读工具 schema。
- 不在输出中暴露 app secret、token、Authorization 头、cookie。

## 示例任务（用户话术）

- 读取并转成二维列表；生成 `rows_by_record_id`。
- 只读 A/B/C 列；A/B/C + 时间列 `LastMonth`。
- 读 5 列、只回填 3 列（`write_back_columns` + 行数据）。
- 有 `record_id` 的更新、无 `record_id` 的新增（分步：归一化 → 集成层 `create`）。
- 可选导出 CSV 字符串（非 `.xlsx`）。

## 示例与调试

- 文件底部 `_reference_usage_cases()` 包含 RPA 函数调用参考；每个调用前都有两行注释：用途说明 + 单行结果参考。
- `debug/demo_flatten.py`：样例 `sample_fields.json` + `sample_records.json` → CSV / 展平。
- `debug/demo_dry_run_update.py`：展示底层 `dry_run_update` 与 `format_dry_run_text`。

常用调用片段：

```python
# 查询昨日指定三列，返回只包含目标列和 record_id 的 ListReadResult。
# 结果参考：ListReadResult(columns=["record_id","状态","名称","数量"], rows=[["recxxx","待处理","示例名称",3]], ...)
rows = query_records_by_time(
    app_token=app_token,
    table_name="示例数据表",
    time_column="申请时间",
    condition="Yesterday",
    query_columns=["状态", "名称", "数量"],
)

# 批量新增记录，入参为字段名列表和二维数据，默认 dry-run 不写入。
# 结果参考：{"ok": true, "summary": {"to_create": 2, "to_skip": 0}, "dry_run": true}
create_result = create_records_by_names(
    app_token=app_token,
    table_name="示例数据表",
    columns=["名称", "数量", "状态"],
    rows=[["示例名称", 1, "待处理"]],
)

# 查询指定记录的关联列，返回被关联记录的 record_id 列表。
# 结果参考：{"recxxx": ["recyyy", "reczzz"]}
linked_record_ids = query_linked_record_ids_by_records(
    app_token=app_token,
    table_name="示例数据表",
    record_ids=["recxxx"],
    column_name="关联活动机制",
)
```
