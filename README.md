# 飞书多维表 Skill

这是一个 Python-only 的飞书/Lark 多维表自动化工具。它可以读取多维表记录，把飞书接口返回的嵌套 `fields` 结构转换成更适合脚本和 RPA 处理的二维列表，并提供带安全校验的查询、更新和新增能力。

## 功能

- 通过 `app_token`、`table_name` 和可选 `view_name` 查询多维表记录。
- 将记录转换为 `columns`、`rows`、`rows_by_record_id`，同时保留 `record_id`。
- 支持 `Today`、`Yesterday`、`CurrentMonth`、`LastMonth` 等相对时间条件。
- 支持 `yyyy-mm-dd`、`yyyy/mm/dd`、`yyyy.mm.dd` 格式的精确日期和日期范围筛选。
- 按字段名更新或新增记录，写入前会做 schema 校验和字段值归一化。
- 默认 dry-run，只有显式传入 `confirm_write=True` 才会真实写入。
- 在当前 Python 进程内缓存 tenant token、table/view ID 和 schema，减少重复请求。
- 提供 RPA 专用版本，去掉函数参数和返回值类型注解，降低 RPA 解析问题。

## 文件说明

- `feishu_bitable_utils.py`：主实现，保留类型注解，适合普通 Python 项目和 Cursor Skill。
- `feishu_bitable_utils_rpa.py`：RPA 友好版本，移除了函数参数和返回值类型注解。
- `SKILL.md`：Cursor Skill 使用说明和详细设计约束。
- `.env.example`：凭证配置模板。
- `debug/`：本地调试示例和样例数据。

## 运行要求

- Python 3.7+
- `requests`

如果运行环境没有安装 `requests`，可以执行：

```bash
pip install requests
```

## 配置

在 Python 文件同目录创建本地 `.env`：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

不要提交 `.env`，它已经被 `.gitignore` 排除。如果把脚本单文件复制到 RPA 环境，也可以直接填写文件顶部的 `FEISHU_APP_CREDENTIALS` 常量。

多维表目标信息不是全局配置，需要在每次调用时传入：

```python
app_token = "base_xxx"
table_name = "数据表"
view_name = "默认视图"
```

## 快速开始

```python
from feishu_bitable_utils import query_records_by_time

rows = query_records_by_time(
    app_token=app_token,
    table_name=table_name,
    time_column="申请时间",
    condition="Today",
    query_columns=["状态", "名称", "数量"],
)

print(rows.columns)
print(rows.rows)
print(rows.rows_by_record_id)
```

## 更新已有记录

默认情况下，更新函数只返回 dry-run 预演报告，不会写入飞书：

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

如果确认要真实写入，传入 `confirm_write=True`。高频 RPA 更新场景可以传 `readback=False`，跳过更新后的额外读回请求：

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

## 新增记录

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

传入多行时会自动使用飞书 `batch_create` 接口批量新增，默认每批最多 500 行。需要退回逐条创建时，可以传 `batch_size=1`。

## RPA 使用

如果 RPA 工具解析 Python 类型注解有问题，请使用 `feishu_bitable_utils_rpa.py`。它的公开函数行为与 `feishu_bitable_utils.py` 保持一致，但函数签名中不包含参数类型和返回值类型注解。

## 安全说明

- `.env`、Python 缓存文件、日志和构建产物都已被忽略。
- 写入函数在调用飞书写接口前，会校验 schema、字段可写性和字段值类型。
- `record_id` 被视为飞书记录技术主键，不会作为普通字段写回。
- 如果长进程运行期间表名、视图名或 schema 发生变化，可以调用 `clear_feishu_cache()` 清空进程内缓存。

