"""
飞书多维表 (Bitable) 通用工具：读取扁平化、列筛选、时间范围过滤、写入值归一化、dry-run。

只依赖标准库与（可选）requests。核心逻辑为纯函数，可被子进程/RPA 直接 import。
"""
import csv
import json
import os
import re
import sys
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
FEISHU_APP_CREDENTIALS = {'app_id': '', 'app_secret': ''}
DEFAULT_ENV_FILE = str(Path(__file__).with_name('.env'))
FEISHU_APP_CREDENTIAL_ENV_KEYS = {'app_id': 'FEISHU_APP_ID', 'app_secret': 'FEISHU_APP_SECRET'}

# RPA 版本保留轻量类，避免 dataclass/类型注解在部分自动化工具中被误解析。
FeishuRawRecord = Dict[str, Any]
FieldSchemaMap = Dict[str, 'TableField']
ColumnNames = List[str]
RowValues = List[Any]
RowsByRecordId = Dict[str, RowValues]
FT_TEXT = 1
FT_NUMBER = 2
FT_SINGLE_SELECT = 3
FT_MULTI_SELECT = 4
FT_DATE = 5
FT_CHECKBOX = 7
FT_USER = 11
FT_HYPERLINK = 15
FT_ATTACHMENT = 17
FT_FORMULA = 20
FT_DUPLEX_LINK = 21
FT_AUTO_NUMBER = 1005
FT_CREATED_TIME = 1001
FT_MODIFIED_TIME = 1002
FT_CREATED_BY = 1003
FT_MODIFIED_BY = 1004
FT_LOCATION = 22

# 写入前会用这些集合拒绝只读字段，避免误写公式、系统时间等特殊列。
READONLY_FIELD_TYPES = frozenset({FT_FORMULA, FT_AUTO_NUMBER, FT_CREATED_TIME, FT_MODIFIED_TIME, FT_CREATED_BY, FT_MODIFIED_BY})
PRIMARY_WRITABLE_FIELD_TYPES = frozenset({FT_TEXT, FT_NUMBER, FT_SINGLE_SELECT, FT_MULTI_SELECT, FT_DATE, FT_CHECKBOX})

class TableViewRef:

    def __init__(self, app_token, table_name, table_id, view_name=None, view_id=None):
        self.app_token = app_token
        self.table_name = table_name
        self.table_id = table_id
        self.view_name = view_name
        self.view_id = view_id

class FeishuAppCredentials:

    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret

class TableField:

    def __init__(self, field_id, field_name, type, writable, property=None, options=None, is_primary=False, ui_type=None):
        self.field_id = field_id
        self.field_name = field_name
        self.type = type
        self.writable = writable
        self.property = property
        self.options = options
        self.is_primary = is_primary
        self.ui_type = ui_type

class ViewSchema:

    def __init__(self, table_id, view_id, table_name, view_name, columns, fields, by_field_id=None):
        self.table_id = table_id
        self.view_id = view_id
        self.table_name = table_name
        self.view_name = view_name
        self.columns = columns
        self.fields = fields
        self.by_field_id = by_field_id if by_field_id is not None else {}

class ListReadResult:

    def __init__(self, columns, rows, rows_by_record_id, raw_records, schema):
        self.columns = columns
        self.rows = rows
        self.rows_by_record_id = rows_by_record_id
        self.raw_records = raw_records
        self.schema = schema

class TimeFilter:

    def __init__(self, time_column, condition):
        self.time_column = time_column
        self.condition = condition

class UpdatePatch:

    def __init__(self, record_id, fields):
        self.record_id = record_id
        self.fields = fields

class NormalizedWriteValue:

    def __init__(self, field, field_id, field_type, input_value, api_value, valid, error=None, old_display=None):
        self.field = field
        self.field_id = field_id
        self.field_type = field_type
        self.input_value = input_value
        self.api_value = api_value
        self.valid = valid
        self.error = error
        self.old_display = old_display

class NormalizedPatch:

    def __init__(self, fields, normalized_values, record_id=None):
        self.fields = fields
        self.normalized_values = normalized_values
        self.record_id = record_id

class ListWriteBackInput:

    def __init__(self, columns, rows_by_record_id):
        self.columns = columns
        self.rows_by_record_id = rows_by_record_id

class TableViewResolveInput:

    def __init__(self, name, table_id):
        self.name = name
        self.table_id = table_id

def _clean_config_value(value):
    return str(value or '').strip()

def load_dotenv_values(env_file=DEFAULT_ENV_FILE):
    """
    读取简单 `.env` 文件，不依赖 python-dotenv。

    支持 `KEY=value`、`export KEY=value`、单双引号包裹值；不解析复杂 shell 展开。
    """
    path = Path(env_file)
    if not path.exists():
        return {}
    values = {}
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        if '=' not in line:
            continue
        key, val = line.split('=', 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and (val[0] in {"'", '"'}):
            val = val[1:-1]
        if key:
            values[key] = val
    return values

def load_app_credentials(*, app_id=None, app_secret=None, env_file=DEFAULT_ENV_FILE):
    """
    加载飞书开放平台应用凭证（App ID / App Secret），优先级：
    1. 函数显式参数
    2. 环境变量 / `.env`
    3. 脚本顶部 FEISHU_APP_CREDENTIALS（RPA 单文件运行时可在此配置）

    `.env` 示例：
        FEISHU_APP_ID=cli_xxx
        FEISHU_APP_SECRET=xxx

    注意：多维表 app_token、table_name、view_name 是业务目标参数，
    不在这里配置，应由读取/写入调用方显式传入。
    """
    dotenv = load_dotenv_values(env_file)

    def pick(explicit, default_value, env_key):
        if explicit is not None and _clean_config_value(explicit):
            return _clean_config_value(explicit)
        env_value = _clean_config_value(os.environ.get(env_key, dotenv.get(env_key, '')))
        if env_value:
            return env_value
        if _clean_config_value(default_value):
            return _clean_config_value(default_value)
        return ''
    return FeishuAppCredentials(app_id=pick(app_id, FEISHU_APP_CREDENTIALS.get('app_id', ''), FEISHU_APP_CREDENTIAL_ENV_KEYS['app_id']), app_secret=pick(app_secret, FEISHU_APP_CREDENTIALS.get('app_secret', ''), FEISHU_APP_CREDENTIAL_ENV_KEYS['app_secret']))

def require_app_credentials(credentials=None, *, env_file=DEFAULT_ENV_FILE):
    """返回完整凭证；缺少 App ID / App Secret 时抛出清晰错误。"""
    creds = credentials or load_app_credentials(env_file=env_file)
    missing = []
    if not creds.app_id:
        missing.append(FEISHU_APP_CREDENTIAL_ENV_KEYS['app_id'])
    if not creds.app_secret:
        missing.append(FEISHU_APP_CREDENTIAL_ENV_KEYS['app_secret'])
    if missing:
        raise ValueError('未配置飞书开放平台应用凭证: ' + ', '.join(missing) + '。Cursor Skill 环境请放入 .env；RPA 单文件运行可填写 FEISHU_APP_CREDENTIALS。')
    return creds

def make_table_view_ref(app_token, table_name, view_name=None, *, tables=None, table_id=None, view_id=None):
    """
    用业务参数构造 TableViewRef。

    推荐调用方只传 app_token/table_name/view_name；集成层先通过表/视图列表 API
    拿到 tables/view_id 后再传入本函数，核心模块不把这些业务目标写进全局配置。
    """
    return resolve_table_view_ref(app_token=app_token, table_name=table_name, view_name=view_name, table_id=table_id, view_id=view_id, tables=tables)

def _dup_names(items, attr):
    seen = {}
    for it in items:
        k = getattr(it, attr)
        seen[k] = seen.get(k, 0) + 1
    return [k for k, v in seen.items() if v > 1]

def resolve_table_view_ref(app_token, table_name, view_name=None, *, table_id=None, view_id=None, tables=None):
    """将表名/视图名解析为 ID。`tables` 为 bitable 表列表 API 返回项（或 TableViewResolveInput 列表）。"""
    t_id = table_id
    v_id = view_id
    if t_id is None:
        if not tables:
            raise ValueError('未提供 table_id 且 tables 为空，无法解析表名')
        resolved = []
        for t in tables:
            if isinstance(t, TableViewResolveInput):
                resolved.append(t)
            else:
                resolved.append(TableViewResolveInput(name=t.get('name', ''), table_id=t.get('table_id', t.get('id', ''))))
        matches = [x for x in resolved if x.name == table_name and x.table_id]
        dups = _dup_names([TableViewResolveInput(x.name, x.table_id) for x in matches], 'name')
        if dups and len([x for x in resolved if x.name == table_name]) > 1:
            all_same = [x for x in resolved if x.name == table_name]
            if len({x.table_id for x in all_same}) > 1:
                raise ValueError(f'表名 {table_name!r} 重复，请显式传入 table_id')
        if not matches:
            names = [x.name for x in resolved]
            raise ValueError(f'找不到表 {table_name!r}。可用: {names[:20]!r}...')
        t_id = matches[0].table_id
    if view_name and (not v_id):
        raise NotImplementedError('本模块不包含『视图名→view_id』HTTP 请求；请用 bitable 视图相关 API 或传入 view_id。计划建议由 Agent 用 MCP/HTTP 查询后传入 view_id')
    return TableViewRef(app_token=app_token, table_name=table_name, table_id=t_id, view_name=view_name, view_id=v_id)

def _is_readonly_type(ft):
    if isinstance(ft, int):
        return ft in READONLY_FIELD_TYPES
    s = str(ft)
    if s.isdigit() and int(s) in READONLY_FIELD_TYPES:
        return True
    return False

def _is_writable_primary_field(field):
    t = int(field.type) if str(field.type).isdigit() else 0
    return bool(field.is_primary and field.field_name and t in PRIMARY_WRITABLE_FIELD_TYPES)

def table_field_from_api(item, *, allow_write_override=None):
    """从 `bitable_v1_appTableField_list` 的单个 item 构造 TableField。"""
    fid = item.get('field_id', '')
    name = item.get('field_name', item.get('name', ''))
    t = item.get('type', 0)
    options = None
    prop = item.get('property')
    if isinstance(prop, dict):
        o = prop.get('options')
        if isinstance(o, list):
            options = o
    if allow_write_override is not None:
        readonly = not allow_write_override
    else:
        readonly = _is_readonly_type(t)
    low = (name or '').lower()
    if '公式' in (name or '') or 'lookup' in low or '引用' in (name or ''):
        if allow_write_override is None:
            readonly = True
    return TableField(field_id=fid, field_name=name, type=t, writable=not readonly and bool(name), property=prop, options=options, is_primary=bool(item.get('is_primary', False)), ui_type=item.get('type_name'))

def ensure_view_schema(ref, field_list_items):
    """
    由字段列表项生成 ViewSchema。`field_list_items` 为 MCP/API `items` 数组，顺序为列序（有 view 时以视图顺序为准）。"""
    by_name = {}
    by_fid = {}
    columns = []
    for item in field_list_items:
        tf = table_field_from_api(item)
        if tf.field_name in by_name and by_name[tf.field_name].field_id != tf.field_id:
            raise ValueError(f'字段名重复: {tf.field_name!r} 对应多个 field_id，请检查表/视图')
        by_name[tf.field_name] = tf
        by_fid[tf.field_id] = tf
        columns.append(tf.field_name)
    return ViewSchema(table_id=ref.table_id, view_id=ref.view_id, table_name=ref.table_name, view_name=ref.view_name, columns=columns, fields=by_name, by_field_id=by_fid)

def _as_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]

def _person_display(v):
    """保留可读 + 可回溯的简要结构。"""
    rows = _as_list(v)
    out = []
    for p in rows:
        if isinstance(p, dict):
            d = {k: p.get(k) for k in ('id', 'name', 'email', 'en_name') if k in p}
            out.append({a: b for a, b in d.items() if b is not None})
        else:
            out.append({'name': str(p)})
    if len(out) == 1:
        return out[0].get('name', json.dumps(out[0], ensure_ascii=False))
    return out

def _number_display(cell):
    if isinstance(cell, (int, float, Decimal, str)):
        if isinstance(cell, str):
            try:
                d = Decimal(cell)
            except (InvalidOperation, ValueError):
                return cell
        else:
            d = Decimal(str(cell))
        if d == d.to_integral():
            return int(d)
        return float(d)
    return str(cell)

def _text_display(cell):
    readable = _readable_cell_value(cell)
    if isinstance(readable, (str, int, float, bool)):
        return str(readable)
    if isinstance(cell, dict) and 'text' in cell:
        return str(cell.get('text', ''))
    if isinstance(cell, str):
        return cell
    if isinstance(cell, (int, float, bool)) and (not isinstance(cell, dict)):
        return str(cell)
    return str(cell)

def _readable_cell_value(cell):
    """把飞书常见富文本/查找/公式返回值压成可读值。"""
    if cell is None:
        return None
    if isinstance(cell, list):
        values = [_readable_cell_value(item) for item in cell]
        values = [v for v in values if v is not None]
        if not values:
            return None
        if all((isinstance(v, str) for v in values)):
            return ''.join(values)
        return values[0] if len(values) == 1 else values
    if isinstance(cell, dict):
        if isinstance(cell.get('value'), list):
            return _readable_cell_value(cell.get('value'))
        for key in ('text', 'name', 'link', 'record_id'):
            if cell.get(key) is not None:
                return cell.get(key)
        if 'date' in cell:
            return cell.get('date')
        return cell
    return cell

def _date_display(field, cell):
    value = cell.get('date') if isinstance(cell, dict) and 'date' in cell else cell
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, (int, float)):
        return value
    ms = int(value)
    if ms < 1000000000000:
        ms *= 1000
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8)))
    if isinstance(field.property, dict) and field.property.get('date_type') == 'date_time':
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    return dt.strftime('%Y-%m-%d')

def _attachment_files(cell):
    items = cell if isinstance(cell, list) else [cell]
    files = []
    for item in items:
        if isinstance(item, dict):
            token = item.get('file_token') or item.get('token') or item.get('fileToken')
            url = item.get('url') or item.get('tmp_url') or item.get('file_url') or item.get('preview_url') or item.get('download_url')
            if token or url:
                files.append({'file_token': token, 'url': url})
        elif isinstance(item, str) and item.startswith('file_'):
            files.append({'file_token': item, 'url': None})
    return files

def _link_record_ids_display(cell):
    """兼容飞书关联字段的几种返回形态，统一先提取 record_id 列表。"""
    if isinstance(cell, dict):
        if 'link_record_ids' in cell:
            ids = cell.get('link_record_ids')
        elif 'record_ids' in cell:
            ids = cell.get('record_ids')
        else:
            ids = None
        if isinstance(ids, list):
            return [x.get('record_id', x) if isinstance(x, dict) else x for x in ids]
        if ids is not None:
            return [ids]
        if 'link_record_ids' in cell or 'record_ids' in cell:
            return None
        records = cell.get('records')
        if isinstance(records, list):
            return [x.get('record_id', x) if isinstance(x, dict) else x for x in records]
    if isinstance(cell, list):
        return [x.get('record_id', x) if isinstance(x, dict) else x for x in cell]
    return cell

def _extract_link_record_ids(cell):
    """从关联字段原始 cell 中提取关联记录 ID，保留原始 ID 字符串。"""
    ids = []

    def add_one(value):
        if value is None:
            return
        if isinstance(value, dict):
            nested = value.get('record_id') or value.get('id')
            if nested is not None:
                ids.append(str(nested))
            return
        ids.append(str(value))

    def add_many(value):
        if isinstance(value, list):
            for item in value:
                add_one(item)
        else:
            add_one(value)

    if isinstance(cell, dict):
        if 'link_record_ids' in cell:
            add_many(cell.get('link_record_ids'))
            return ids
        if 'record_ids' in cell:
            add_many(cell.get('record_ids'))
            return ids
        if 'records' in cell:
            add_many(cell.get('records'))
            return ids
        add_one(cell)
        return ids
    if isinstance(cell, list):
        for item in cell:
            if isinstance(item, dict):
                if 'link_record_ids' in item:
                    add_many(item.get('link_record_ids'))
                elif 'record_ids' in item:
                    add_many(item.get('record_ids'))
                elif 'records' in item:
                    add_many(item.get('records'))
                else:
                    add_one(item)
            else:
                add_one(item)
        return ids
    add_one(cell)
    return ids

def field_cell_display(field, cell):
    """将飞书记录中某一字段的原始 `cell` 转为便于调试/CSV 的展示值。"""
    t = int(field.type) if str(field.type).isdigit() else 0
    if cell is None or cell == '':
        return None
    if t in (FT_NUMBER, 2) or (not str(field.type).isdigit() and field.type in (2, '2')):
        return _number_display(_readable_cell_value(cell))
    if t in (FT_TEXT, 1):
        return _text_display(cell)
    if t in (3, FT_SINGLE_SELECT):
        readable = _readable_cell_value(cell)
        if isinstance(readable, str):
            return readable
        if isinstance(cell, str):
            return cell
        if isinstance(cell, dict) and 'text' in cell:
            return cell.get('text', cell)
        if isinstance(cell, dict) and 'name' in cell:
            return cell.get('name')
    if t in (4, FT_MULTI_SELECT):
        if isinstance(cell, str):
            return [cell]
        if isinstance(cell, list):
            out = []
            for c in cell:
                if isinstance(c, dict) and c.get('text') is not None:
                    out.append(c.get('text'))
                elif isinstance(c, dict) and c.get('name') is not None:
                    out.append(c.get('name'))
                else:
                    out.append(str(c))
            return out
    if t in (5, FT_DATE):
        return _date_display(field, cell)
    if t in (FT_CREATED_TIME, FT_MODIFIED_TIME):
        return _date_display(field, cell)
    if t in (7, FT_CHECKBOX):
        if isinstance(cell, bool):
            return cell
    if t in (11, FT_USER):
        return _person_display(cell)
    if t in (FT_ATTACHMENT, 17):
        files = _attachment_files(cell)
        return files if files else cell
    if t in (FT_HYPERLINK, 15):
        readable = _readable_cell_value(cell)
        return readable if readable is not None else cell
    if t in (18, 21, FT_DUPLEX_LINK):
        return _link_record_ids_display(cell)
    readable = _readable_cell_value(cell)
    if not isinstance(readable, (dict, list)):
        return readable
    if isinstance(readable, (dict, list)):
        return readable
    if isinstance(cell, (int, float, bool)):
        return cell
    return str(cell)

def get_field_raw(record, f):
    fields = record.get('fields') or {}
    if f.field_id and f.field_id in fields:
        return fields.get(f.field_id)
    if f.field_name in fields:
        return fields.get(f.field_name)
    for k, v in fields.items():
        if k == f.field_id or k == f.field_name:
            return v
    return None

def flatten_records(raw_records, schema=None):
    """
    将 {record_id, fields} 展平为 { record_id, <列名>: 展示值 } 便于调试。
    未提供 schema 时仅平铺能识别的 `fields` 的键（多为 field_id，可读性一般）。
    """
    out = []
    for r in raw_records:
        row = {'record_id': r.get('record_id', '')}
        fmap = r.get('fields') or {}
        if not schema:
            for k, v in fmap.items():
                row[str(k)] = v
        else:
            for name, tf in schema.fields.items():
                raw = get_field_raw(r, tf)
                row[name] = field_cell_display(tf, raw) if raw is not None else None
        out.append(row)
    return out

def records_to_list_result(raw_records, schema, include_record_id_column=True):
    col_names = ['record_id', *schema.columns] if include_record_id_column else list(schema.columns)
    rows = []
    rbr = {}
    for r in raw_records:
        rid = r.get('record_id', '')
        rvals = []
        for c in schema.columns:
            tf = schema.fields.get(c)
            if not tf:
                rvals.append(None)
            else:
                rawv = get_field_raw(r, tf)
                rvals.append(field_cell_display(tf, rawv) if rawv is not None else None)
        rbr[rid] = rvals
        if include_record_id_column:
            rows.append([rid, *rvals])
        else:
            rows.append(rvals)
    return ListReadResult(columns=col_names, rows=rows, rows_by_record_id=rbr, raw_records=raw_records, schema=schema)
TCOND_LAST = 'LastMonth'
TCOND_CURRENT = 'CurrentMonth'
TCOND_YESTERDAY = 'Yesterday'
TCOND_TODAY = 'Today'
ALLOWED_TIME_CONDITIONS = frozenset({TCOND_LAST, TCOND_CURRENT, TCOND_YESTERDAY, TCOND_TODAY})

def _naive_utc_today():
    return datetime.now(timezone.utc).date()

def time_window_ms_utc(condition, *, now=None):
    """[start_ms, end_ms) 左闭右开, UTC 日界。"""
    if condition not in ALLOWED_TIME_CONDITIONS:
        raise ValueError(f'time_condition 必须是 {sorted(ALLOWED_TIME_CONDITIONS)} 之一, 收到 {condition!r}')
    today = now or _naive_utc_today()
    if condition == TCOND_TODAY:
        s = datetime.combine(today, time.min, tzinfo=timezone.utc)
        e = s + timedelta(days=1)
        return (int(s.timestamp() * 1000), int(e.timestamp() * 1000))
    if condition == TCOND_YESTERDAY:
        d0 = today - timedelta(days=1)
        s = datetime.combine(d0, time.min, tzinfo=timezone.utc)
        e = s + timedelta(days=1)
        return (int(s.timestamp() * 1000), int(e.timestamp() * 1000))
    if condition == TCOND_CURRENT:
        d0 = date(today.year, today.month, 1)
        if today.month == 12:
            d1 = date(today.year + 1, 1, 1)
        else:
            d1 = date(today.year, today.month + 1, 1)
        s = datetime.combine(d0, time.min, tzinfo=timezone.utc)
        e = datetime.combine(d1, time.min, tzinfo=timezone.utc)
        return (int(s.timestamp() * 1000), int(e.timestamp() * 1000))
    if condition == TCOND_LAST:
        this_month = date(today.year, today.month, 1)
        last_eom = this_month - timedelta(days=1)
        d0 = date(last_eom.year, last_eom.month, 1)
        s = datetime.combine(d0, time.min, tzinfo=timezone.utc)
        e = datetime.combine(this_month, time.min, tzinfo=timezone.utc)
        return (int(s.timestamp() * 1000), int(e.timestamp() * 1000))
    raise AssertionError('unreachable')

def extract_time_ms_for_filter(cell):
    """从日期类字段原始值抽取毫秒时间戳，供本地时间过滤。"""
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        return int(cell)
    if isinstance(cell, str):
        s = cell.strip()
        if s.isdigit() and len(s) >= 10:
            return int(s[:13])
    if isinstance(cell, dict) and 'date' in cell:
        d = cell['date']
        if isinstance(d, (int, float, str)) and str(d).replace('-', '').isdigit():
            if isinstance(d, str) and len(d) >= 10 and ('-' in d):
                try:
                    p = d[:10]
                    t = datetime.strptime(p, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    return int(t.timestamp() * 1000)
                except ValueError:
                    pass
            if isinstance(d, (int, float, str)) and str(d).isdigit():
                return int(str(d)[:13]) if len(str(d)) > 2 else int(d)
    return None

def _record_in_time(r, schema, time_column, start_ms, end_ms):
    tf = schema.fields.get(time_column)
    if not tf:
        raise KeyError(f'时间列 {time_column!r} 不在 schema 中. 已注册列: {list(schema.fields)[:20]}...')
    raw = get_field_raw(r, tf)
    ms = extract_time_ms_for_filter(raw)
    if ms is None:
        return False
    return start_ms <= ms < end_ms

def build_time_filter_for_search(time_column, time_condition):
    """
    构造 `bitable_v1_appTableRecord_search` 的 `data.filter` 片段。

    飞书日期字段不支持 `isGreaterEqual` / `isLess` 这类范围操作符；
    相对时间应使用 `operator: "is"` + `value: ["CurrentMonth"]` 等关键字。
    """
    if time_condition not in ALLOWED_TIME_CONDITIONS:
        raise ValueError(f'time_condition 必须是 {sorted(ALLOWED_TIME_CONDITIONS)} 之一, 收到 {time_condition!r}')
    return {'conjunction': 'and', 'conditions': [{'field_name': time_column, 'operator': 'is', 'value': [time_condition]}]}

def select_columns(raw_records, schema, query_columns, time_column=None, time_condition=None):
    """
    只保留 `query_columns` 列；`record_id` 始终可包含在 `columns` 第一列。
    当提供 time_condition 时必须同时提供 time_column。过滤在**已拉取的**记录上完成。
    """
    if not query_columns:
        raise ValueError('query_columns 为空')
    for c in query_columns:
        if c not in schema.fields:
            raise KeyError(f'列 {c!r} 不存在. 可选项: {list(schema.fields)[:30]}...')
    if (time_column is None) != (time_condition is None):
        raise ValueError('time_column 与 time_condition 必须成对同时提供或同时为空')
    recs = raw_records
    if time_condition and time_column:
        s, e = time_window_ms_utc(time_condition)
        recs = [r for r in recs if _record_in_time(r, schema, time_column, s, e)]
    sub_schema = _schema_subset(schema, query_columns)
    return records_to_list_result(recs, sub_schema, include_record_id_column=True)

def _schema_subset(schema, names):
    fmap = {n: schema.fields[n] for n in names if n in schema.fields}
    cols = [n for n in names if n in fmap]
    byid = {f.field_id: f for f in fmap.values()}
    return ViewSchema(table_id=schema.table_id, view_id=schema.view_id, table_name=schema.table_name, view_name=schema.view_name, columns=cols, fields=fmap, by_field_id=byid)

def _opt_names(field):
    s = set()
    if not field.options:
        return s
    for o in field.options:
        if isinstance(o, dict):
            n = o.get('name') or o.get('text')
            if n:
                s.add(str(n))
    return s

def _date_input_to_ms(v):
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        if n < 1000000000000:
            n *= 1000
        return n
    if isinstance(v, str) and re.match('^\\d{13,}$', v):
        return int(v[:13])
    if isinstance(v, str):
        for fmt, mlen in (('%Y-%m-%d', 10), ('%Y/%m/%d', 10), ('%Y-%m-%d %H:%M:%S', 19)):
            if len(v) < mlen:
                continue
            try:
                s = v[:mlen]
                d = datetime.strptime(s, fmt)
                d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
                if mlen == 10:
                    d = datetime.combine(d.date(), time.min, tzinfo=timezone.utc)
                return int(d.timestamp() * 1000)
            except ValueError:
                continue
    return None

def normalize_value_for_write(field, value):
    """(api_value, error)。"""
    t = int(field.type) if str(field.type).isdigit() else 0
    if not field.writable and field.field_name and (t in READONLY_FIELD_TYPES):
        return (None, f'只读/特殊字段不可写: type={t}')
    if t in (FT_FORMULA, FT_AUTO_NUMBER) or t in (FT_CREATED_TIME, FT_MODIFIED_TIME):
        return (None, f'只读 type={t}')
    if t in (1, FT_TEXT):
        if value is None:
            return (None, None)
        return (str(value), None)
    if t in (2, FT_NUMBER):
        if value is None or value == '':
            return (None, None)
        if isinstance(value, (int, float, Decimal)):
            v = value
        else:
            try:
                d = Decimal(str(value).replace(',', ''))
            except (InvalidOperation, ValueError) as e:
                return (None, f'非数字: {e}')
            v = d
        d = v if isinstance(v, Decimal) else Decimal(str(v))
        if d == d.to_integral():
            return (int(d), None)
        return (float(d), None)
    if t in (3, FT_SINGLE_SELECT):
        opts = _opt_names(field)
        s = str(value) if value is not None else ''
        if opts and s and (s not in opts):
            return (None, f'单选 {s!r} 不在选项: {list(opts)[:8]}...')
        return (s, None)
    if t in (4, FT_MULTI_SELECT):
        opts = _opt_names(field)
        if value is None or value == '':
            return ([], None)
        if isinstance(value, list):
            parts = [str(x) for x in value]
        else:
            parts = [p.strip() for p in re.split('[,;，；]', str(value)) if p.strip()]
        if opts and parts:
            bad = [p for p in parts if p not in opts]
            if bad:
                return (None, f'多选 {bad!r} 非合法项')
        return (parts, None)
    if t in (5, FT_DATE):
        ms = _date_input_to_ms(value)
        if value not in (None, '') and ms is None:
            return (None, '无法解析日期/时间')
        if ms is None:
            return (None, None)
        if isinstance(field.property, dict) and field.property.get('date_type') == 'date_time':
            return (ms, None)
        return (ms, None)
    if t in (7, FT_CHECKBOX):
        if value is None or value == '':
            return (None, None)
        s = str(value).strip().lower()
        if s in ('1', 'true', 'y', 'yes', '是', 't'):
            return (True, None)
        if s in ('0', 'false', 'n', 'no', '否', 'f'):
            return (False, None)
        return (None, f'非布尔: {value!r}')
    if t in (11, FT_USER):
        return (None, '人员需 open_id/user_id 等对象, 首版不凭纯文本名写入')
    if t in (15, FT_ATTACHMENT) or t == 17:
        if isinstance(value, (dict, list)) or (value is not None and str(value).startswith('file_')):
            return (value, None)
        return (None, '附件/链接需 file_token/结构化对象, 非本地路径/URL 文本')
    if t in (18, 21) or t == 22:
        return (value, None)
    if value is None or value == '':
        return (None, None)
    return (value, None)

def normalize_patch(row, schema):
    """按字段名 -> 值的行 dict 归一化；可含 `record_id` 键。"""
    rid = str(row.get('record_id', ''))
    body = {k: v for k, v in row.items() if k != 'record_id'}
    return build_patch_dict(body, schema, rid)

def build_patch_dict(name_to_value, schema, record_id):
    """`name_to_value` 使用字段名; 产出的 `fields` 为 field_id 键, 与 Feishu API 常见写法一致。"""
    norm = []
    out = {}
    for k, v in name_to_value.items():
        f = schema.get(k)
        if f is None:
            norm.append(NormalizedWriteValue(k, k, '?', v, None, False, error='未知列'))
            continue
        if (not f.writable) and (not _is_writable_primary_field(f)):
            norm.append(NormalizedWriteValue(f.field_name, f.field_id, f.type, v, None, False, '不可写'))
            continue
        av, err = normalize_value_for_write(f, v)
        nv = NormalizedWriteValue(f.field_name, f.field_id, f.type, v, av, err is None, err)
        norm.append(nv)
        if err is None and av is not None:
            out[f.field_id] = av
    return NormalizedPatch(fields=out, normalized_values=norm, record_id=record_id or None)

def build_patches_from_rows_by_id(write_back_columns, write_back_rows, schema):
    patches = []
    for rid, vals in write_back_rows.items():
        if len(vals) != len(write_back_columns):
            p = NormalizedPatch({}, [NormalizedWriteValue('(row)', '', 0, vals, None, False, error=f'列数{len(write_back_columns)} 与 值{len(vals)} 不匹配')], record_id=rid)
            patches.append(p)
            continue
        m = dict(zip(write_back_columns, vals))
        patches.append(build_patch_dict(m, schema, rid))
    return patches

def build_update_patches(rows, schema):
    out = []
    for row in rows:
        rid = str(row.get('record_id', ''))
        body = {k: v for k, v in row.items() if k not in ('record_id', 'fields')}
        if 'fields' in row and isinstance(row['fields'], dict):
            body = dict(row['fields'])
        if not rid:
            out.append(build_patch_dict(body, schema, ''))
        else:
            out.append(build_patch_dict(body, schema, rid))
    return out

def _old_display_for(rec, f):
    if not rec or f is None:
        return None
    raw = get_field_raw(rec, f)
    return field_cell_display(f, raw) if raw is not None else None

def dry_run_update(patches, old_by_record_id, field_schema):
    """
    `old_by_record_id` 为 record_id->原始记录，或 原始 `raw_records` 列表。
    返回 { ok, errors, summary, per_record, dry_run: True }。
    """
    by_id = {}
    if isinstance(old_by_record_id, list):
        for r in old_by_record_id:
            rid = r.get('record_id', '')
            if rid:
                by_id[str(rid)] = r
    elif isinstance(old_by_record_id, dict):
        by_id = dict(old_by_record_id)
    errors = []
    per = []
    to_update = []
    to_skip = []
    for p in patches:
        rid = p.record_id or ''
        orec = by_id.get(rid) if rid else None
        line = {'record_id': rid, 'values': []}
        any_bad = False
        for n in p.normalized_values:
            f = field_schema.get(n.field)
            old_d = _old_display_for(orec, f)
            line['values'].append({'field': n.field, 'field_type': n.field_type, 'old': old_d, 'input': n.input_value, 'api': n.api_value, 'valid': n.valid, 'error': n.error})
            if n.error and (not n.valid):
                any_bad = True
                if n.error:
                    errors.append(f"{rid or 'new'} / {n.field}: {n.error}")
        if any_bad and (not p.fields):
            to_skip.append(rid)
        if p.fields:
            to_update.append({'record_id': rid, 'fields': p.fields, 'ok': not any_bad})
        per.append(line)
    return {'ok': len(errors) == 0, 'errors': errors, 'warnings': [], 'summary': {'patch_count': len(patches), 'to_update': len([x for x in to_update if x.get('record_id')]), 'to_skip': len(to_skip)}, 'per_record': per, 'dry_run': True}

def format_dry_run_text(result, *, max_records=50):
    """人类可读预演，类似计划中的示例。"""
    lines = []
    for pr in (result.get('per_record') or [])[:max_records]:
        rid = pr.get('record_id', '')
        lines.append(f'record_id: {rid}')
        for v in pr.get('values') or []:
            if not isinstance(v, dict):
                continue
            lines.append(f"  字段：{v.get('field')}")
            lines.append(f"  字段类型：{v.get('field_type')}")
            if v.get('old') is not None:
                lines.append(f"  原值：{v.get('old')!r}")
            lines.append(f"  输入值：{v.get('input')!r}")
            lines.append(f"  API 写入值：{v.get('api')!r}")
            lines.append('  结果：' + ('OK' if v.get('valid') else f"失败: {v.get('error')}"))
        lines.append('')
    lines.append('summary: ' + json.dumps(result.get('summary'), ensure_ascii=False))
    return '\n'.join(lines)
FEISHU_OPEN_API_BASE = 'https://open.feishu.cn/open-apis'
FEISHU_LOCAL_TZ = timezone(timedelta(hours=8))
_TENANT_TOKEN_CACHE = {}
_TABLES_CACHE = {}
_VIEWS_CACHE = {}
_REF_CACHE = {}
_SCHEMA_CACHE = {}

def clear_feishu_cache():
    _TENANT_TOKEN_CACHE.clear()
    _TABLES_CACHE.clear()
    _VIEWS_CACHE.clear()
    _REF_CACHE.clear()
    _SCHEMA_CACHE.clear()

def _require_requests():
    try:
        import requests
    except Exception as exc:
        raise RuntimeError('需要 requests 才能直连飞书开放 API') from exc
    return requests

def _get_tenant_access_token(credentials=None, *, env_file=DEFAULT_ENV_FILE):
    creds = require_app_credentials(credentials, env_file=env_file)
    now = _time.time()
    cache_key = (creds.app_id, str(env_file))
    cached = _TENANT_TOKEN_CACHE.get(cache_key)
    if cached and cached.get('expires_at', 0) > now:
        return cached.get('token', '')
    requests = _require_requests()
    response = requests.post(f'{FEISHU_OPEN_API_BASE}/auth/v3/tenant_access_token/internal', json={'app_id': creds.app_id, 'app_secret': creds.app_secret}, timeout=20)
    data = response.json()
    if data.get('code') != 0:
        safe = {k: v for k, v in data.items() if k != 'tenant_access_token'}
        raise RuntimeError(f'获取 tenant_access_token 失败: {safe}')
    token = str(data.get('tenant_access_token', ''))
    ttl = int(data.get('expire') or 6900)
    _TENANT_TOKEN_CACHE[cache_key] = {'token': token, 'expires_at': now + max(60, ttl - 120)}
    return token

def _feishu_request(method, path, *, token, params=None, json_body=None):
    requests = _require_requests()
    response = requests.request(method, f'{FEISHU_OPEN_API_BASE}{path}', headers={'Authorization': f'Bearer {token}'}, params=params or {}, json=json_body, timeout=30)
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f'飞书 API 返回非 JSON: {response.text[:500]}') from exc
    if data.get('code') != 0:
        raise RuntimeError(f'飞书 API 调用失败: {data}')
    return data

def _paged_items(path, *, token, method='GET', params=None, json_body=None, page_size=500):
    items = []
    page_token = None
    while True:
        req_params = dict(params or {})
        req_params['page_size'] = page_size
        if page_token:
            req_params['page_token'] = page_token
        data = _feishu_request(method, path, token=token, params=req_params, json_body=json_body)
        body = data.get('data') or {}
        items.extend(body.get('items') or [])
        if not body.get('has_more'):
            return items
        page_token = body.get('page_token')
        if not page_token:
            return items

def _list_tables_raw(app_token, *, token):
    if app_token not in _TABLES_CACHE:
        _TABLES_CACHE[app_token] = _paged_items(f'/bitable/v1/apps/{app_token}/tables', token=token, page_size=100)
    return _TABLES_CACHE[app_token]

def _list_views_raw(app_token, table_id, *, token):
    cache_key = (app_token, table_id)
    if cache_key not in _VIEWS_CACHE:
        _VIEWS_CACHE[cache_key] = _paged_items(f'/bitable/v1/apps/{app_token}/tables/{table_id}/views', token=token, page_size=100)
    return _VIEWS_CACHE[cache_key]

def _resolve_table_and_view(app_token, table_name, *, view_name=None, table_id=None, view_id=None, token):
    cache_key = (app_token, table_name, view_name, table_id, view_id)
    cached = _REF_CACHE.get(cache_key)
    if cached:
        return cached
    tables = _list_tables_raw(app_token, token=token)
    ref = resolve_table_view_ref(app_token=app_token, table_name=table_name, view_name=None, table_id=table_id, tables=tables)
    resolved_view_id = view_id
    if view_name and (not resolved_view_id):
        views = _list_views_raw(app_token, ref.table_id, token=token)
        matches = [v for v in views if (v.get('view_name') or v.get('name')) == view_name and (v.get('view_id') or v.get('id'))]
        if not matches:
            names = [v.get('view_name') or v.get('name') for v in views]
            raise ValueError(f'找不到视图 {view_name!r}. 可用视图: {names!r}')
        ids = {str(v.get('view_id') or v.get('id')) for v in matches}
        if len(ids) > 1:
            raise ValueError(f'视图名 {view_name!r} 重复，请显式传入 view_id')
        resolved_view_id = next(iter(ids))
    resolved = TableViewRef(app_token=app_token, table_name=table_name, table_id=ref.table_id, view_name=view_name, view_id=resolved_view_id)
    _REF_CACHE[cache_key] = resolved
    return resolved

def _list_fields_raw(ref, *, token):
    params = {}
    if ref.view_id:
        params['view_id'] = ref.view_id
    return _paged_items(f'/bitable/v1/apps/{ref.app_token}/tables/{ref.table_id}/fields', token=token, params=params, page_size=100)

def _fetch_view_schema(ref, *, token):
    cache_key = (ref.app_token, ref.table_id, ref.view_id)
    if cache_key not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[cache_key] = ensure_view_schema(ref, _list_fields_raw(ref, token=token))
    return _SCHEMA_CACHE[cache_key]

def _search_records(ref, *, token, query_columns=None, filter_=None):
    body = {}
    if ref.view_id:
        body['view_id'] = ref.view_id
    if query_columns:
        body['field_names'] = query_columns
    if filter_:
        body['filter'] = filter_
    return _paged_items(f'/bitable/v1/apps/{ref.app_token}/tables/{ref.table_id}/records/search', token=token, method='POST', json_body=body, page_size=500)

def _primary_field(schema):
    """关联记录展示时优先使用主字段；缺失主字段时退回第一列。"""
    for field in schema.fields.values():
        if field.is_primary:
            return field
    if schema.columns:
        return schema.fields.get(schema.columns[0])
    return None

def _record_field_key(record, field):
    """保持原始 record 的字段键风格，避免 field_id/name 混用时写错位置。"""
    fields = record.get('fields') or {}
    if field.field_id and field.field_id in fields:
        return field.field_id
    if field.field_name in fields:
        return field.field_name
    return field.field_name or field.field_id

def _linked_record_display_map(app_token, table_id, table_name, *, token):
    """读取关联表主字段，构造 record_id -> 展示值 的映射。"""
    ref = TableViewRef(app_token=app_token, table_name=table_name or table_id, table_id=table_id)
    schema = _fetch_view_schema(ref, token=token)
    primary = _primary_field(schema)
    if primary is None:
        return {}
    records = _search_records(ref, token=token, query_columns=[primary.field_name])
    return {str(record.get('record_id')): field_cell_display(primary, get_field_raw(record, primary)) for record in records if record.get('record_id') and get_field_raw(record, primary) is not None}

def _expand_link_record_displays(raw_records, schema, *, app_token, token):
    """
    将关联字段的 record_id 展开为关联表主字段展示值。

    飞书 records/search 默认只返回关联记录 ID；为了让 RPA/CSV 结果更接近界面显示，
    这里会按关联字段 property.table_id 额外读取一次关联表主字段。
    """
    link_fields = [field for field in schema.fields.values() if (int(field.type) if str(field.type).isdigit() else 0) in (18, 21, FT_DUPLEX_LINK) and isinstance(field.property, dict) and field.property.get('table_id')]
    if not link_fields:
        return raw_records
    display_maps = {}
    for field in link_fields:
        linked_table_id = str(field.property.get('table_id'))
        if linked_table_id not in display_maps:
            display_maps[linked_table_id] = _linked_record_display_map(app_token, linked_table_id, field.property.get('table_name'), token=token)
    expanded = []
    for record in raw_records:
        copied = dict(record)
        fields = dict(record.get('fields') or {})
        copied['fields'] = fields
        for field in link_fields:
            raw = get_field_raw(record, field)
            ids = _link_record_ids_display(raw)
            if not isinstance(ids, list):
                continue
            display_map = display_maps.get(str(field.property.get('table_id')), {})
            values = [display_map.get(str(record_id), record_id) for record_id in ids]
            fields[_record_field_key(record, field)] = values[0] if len(values) == 1 else values
        expanded.append(copied)
    return expanded

def _read_record(ref, record_id, *, token):
    data = _feishu_request('GET', f'/bitable/v1/apps/{ref.app_token}/tables/{ref.table_id}/records/{record_id}', token=token)
    return (data.get('data') or {}).get('record') or data.get('data') or {}

def _update_record(ref, record_id, fields, *, token):
    data = _feishu_request('PUT', f'/bitable/v1/apps/{ref.app_token}/tables/{ref.table_id}/records/{record_id}', token=token, json_body={'fields': fields})
    return (data.get('data') or {}).get('record') or data.get('data') or {}

def _create_record(ref, fields, *, token):
    data = _feishu_request('POST', f'/bitable/v1/apps/{ref.app_token}/tables/{ref.table_id}/records', token=token, json_body={'fields': fields})
    return (data.get('data') or {}).get('record') or data.get('data') or {}

def _create_records_batch(ref, records_fields, *, token):
    if not records_fields:
        return []
    data = _feishu_request('POST', f'/bitable/v1/apps/{ref.app_token}/tables/{ref.table_id}/records/batch_create', token=token, json_body={'records': [{'fields': fields} for fields in records_fields]})
    body = data.get('data') or {}
    return body.get('records') or body.get('items') or []

def _cell_to_local_date(value):
    ms = extract_time_ms_for_filter(value)
    if ms is None:
        return None
    if ms < 1000000000000:
        ms *= 1000
    return datetime.fromtimestamp(ms / 1000, tz=FEISHU_LOCAL_TZ).date()

def parse_date_string(value):
    """解析常见日期文本，支持 yyyy-mm-dd、yyyy/mm/dd、yyyy.mm.dd。"""
    text = value.strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f'无法识别日期格式: {value!r}，请使用 yyyy-mm-dd 或 yyyy/mm/dd')

def _filter_records_by_local_date(records, *, time_column, exact_date=None, start_date=None, end_date=None):
    if exact_date:
        target = parse_date_string(exact_date)
        start = target
        end = target
    else:
        start = parse_date_string(start_date) if start_date else None
        end = parse_date_string(end_date) if end_date else None
    filtered = []
    for record in records:
        record_date = _cell_to_local_date((record.get('fields') or {}).get(time_column))
        if record_date is None:
            continue
        if start and record_date < start:
            continue
        if end and record_date > end:
            continue
        filtered.append(record)
    return filtered

def _patch_to_field_name_fields(patch):
    fields = {}
    for item in patch.normalized_values:
        if item.valid and item.error is None and (item.api_value is not None):
            fields[item.field] = item.api_value
    return fields

def _patch_errors(patch):
    return [f"{patch.record_id or 'new'} / {item.field}: {item.error}" for item in patch.normalized_values if item.error or (not item.valid)]

def _patches_errors(patches):
    errors = []
    for patch in patches:
        errors.extend(_patch_errors(patch))
    return errors

def _build_create_dry_run(patches):
    errors = []
    per_record = []
    to_create = 0
    to_skip = 0
    for idx, patch in enumerate(patches):
        row_errors = [f'row {idx + 1} / {v.field}: {v.error}' for v in patch.normalized_values if v.error]
        errors.extend(row_errors)
        if row_errors or not _patch_to_field_name_fields(patch):
            to_skip += 1
        else:
            to_create += 1
        per_record.append({'row_index': idx, 'values': [{'field': v.field, 'field_type': v.field_type, 'input': v.input_value, 'api': v.api_value, 'valid': v.valid, 'error': v.error} for v in patch.normalized_values]})
    return {'ok': not errors, 'errors': errors, 'warnings': [], 'summary': {'to_create': to_create, 'to_skip': to_skip}, 'per_record': per_record, 'dry_run': True}

def list_bitable_tables(app_token, credentials=None):
    """
    查询一个多维表 app 下的所有 table，并附带每个 table 的 view 列表。

    参数:
        app_token: 多维表 app_token，例如 `base_xxx`。
        credentials: 可选飞书开放平台应用凭证；不传时读取 `.env` / 环境变量 / 顶部 DEFAULT。

    返回:
        `[{"table_name": "...", "table_id": "...", "views": [{"view_name": "...", "view_id": "..."}]}]`
    """
    token = _get_tenant_access_token(credentials)
    tables = _list_tables_raw(app_token, token=token)
    result = []
    for table in tables:
        table_id = table.get('table_id') or table.get('id')
        views = _list_views_raw(app_token, table_id, token=token) if table_id else []
        result.append({'table_name': table.get('name'), 'table_id': table_id, 'views': [{'view_name': v.get('view_name') or v.get('name'), 'view_id': v.get('view_id') or v.get('id'), 'view_type': v.get('view_type')} for v in views]})
    return result

def query_records_by_time(app_token, table_name, query_columns=None, time_column=None, condition=None, start_date=None, end_date=None):
    """
    查询记录，并返回二维列表结构。

    最小调用只需要传 `app_token + table_name`，默认查询全表所有列。
    如只需要部分列，传 `query_columns`。
    如需要按日期筛选，再额外传 `time_column + condition` 或 `time_column + start_date/end_date`。

    时间参数:
        condition: 飞书原生相对时间关键字，支持 `Today`、`Yesterday`、`CurrentMonth`、`LastMonth`。
            提供后优先使用接口侧过滤。
        start_date/end_date: 包含式日期范围，例如 `2026-04-26` 到 `2026-04-28`。

    返回:
        `ListReadResult(columns=[...], rows=[...], rows_by_record_id={...}, raw_records=[...], schema=...)`
    """
    if (condition or start_date or end_date) and (not time_column):
        raise ValueError('使用日期筛选时必须传 time_column；不传 time_column 时默认查询全表')
    if condition and (start_date or end_date):
        raise ValueError('condition 不能和 start_date/end_date 同时使用')
    token = _get_tenant_access_token()
    ref = _resolve_table_and_view(app_token, table_name, token=token)
    schema = _fetch_view_schema(ref, token=token)
    output_columns = query_columns or list(schema.columns)
    validate_columns = list(output_columns)
    if time_column:
        validate_columns.append(time_column)
    for col in validate_columns:
        if col not in schema.fields:
            raise KeyError(f'列 {col!r} 不存在. 可用列: {list(schema.fields)}')
    filter_ = build_time_filter_for_search(time_column, condition) if (time_column and condition) else None
    fetch_columns = list(dict.fromkeys(([time_column] if time_column else []) + list(output_columns)))
    raw_records = _search_records(ref, token=token, query_columns=fetch_columns, filter_=filter_)
    if start_date or end_date:
        raw_records = _filter_records_by_local_date(raw_records, time_column=time_column, start_date=start_date, end_date=end_date)
    raw_records = _expand_link_record_displays(raw_records, _schema_subset(schema, output_columns), app_token=app_token, token=token)
    return records_to_list_result(raw_records, _schema_subset(schema, output_columns), include_record_id_column=True)

def dry_run_update_by_names(app_token, table_name, record_id, columns, values, view_name=None, table_id=None, credentials=None):
    """
    用字段名列表和单行值生成更新预演，不写入飞书。

    参数:
        columns: 要更新的字段名列表。
        values: 与 `columns` 等长的一维数据。

    返回:
        `{"ok": true, "summary": {"patch_count": 1, "to_update": 1}, "dry_run": true, ...}`
    """
    token = _get_tenant_access_token(credentials)
    ref = _resolve_table_and_view(app_token, table_name, view_name=view_name, table_id=table_id, token=token)
    schema = _fetch_view_schema(ref, token=token)
    record = _read_record(ref, record_id, token=token)
    patches = build_patches_from_rows_by_id(columns, {record_id: values}, schema.fields)
    return dry_run_update(patches, [record], schema.fields)

def update_record_by_names(app_token, table_name, record_id, columns, values, view_name=None, table_id=None, credentials=None, confirm_write=False, readback=True):
    """
    用字段名列表和单行值更新指定 record。

    默认 `confirm_write=False`，读取旧记录并返回 dry-run，不执行真实写入。
    只有显式传 `confirm_write=True` 才会跳过 dry-run 旧值读取并调用飞书 update API。
    `readback=True` 时写入后再读回本次更新字段；RPA 高频场景可传 `False` 提速。
    """
    token = _get_tenant_access_token(credentials)
    ref = _resolve_table_and_view(app_token, table_name, view_name=view_name, table_id=table_id, token=token)
    schema = _fetch_view_schema(ref, token=token)
    patches = build_patches_from_rows_by_id(columns, {record_id: values}, schema.fields)
    if not confirm_write:
        record = _read_record(ref, record_id, token=token)
        return dry_run_update(patches, [record], schema.fields)
    errors = _patch_errors(patches[0])
    if errors:
        return {'ok': False, 'errors': errors, 'warnings': [], 'summary': {'to_update': 0, 'to_skip': 1}, 'dry_run': False}
    fields = _patch_to_field_name_fields(patches[0])
    if not fields:
        return {'ok': False, 'errors': [f'{record_id} / 更新字段为空'], 'warnings': [], 'summary': {'to_update': 0, 'to_skip': 1}, 'dry_run': False}
    updated = _update_record(ref, record_id, fields, token=token)
    result = {'ok': True, 'record_id': record_id, 'updated_fields': fields, 'update_response': updated}
    if readback:
        readback_record = _read_record(ref, record_id, token=token)
        result['readback'] = {k: (readback_record.get('fields') or {}).get(k) for k in fields}
    return result

def create_records_by_names(app_token, table_name, columns, rows, view_name=None, table_id=None, credentials=None, confirm_write=False, batch_size=500):
    """
    用字段名列表和二维数据批量新增记录。

    参数:
        columns: 新增字段名列表。
        rows: 二维列表；每一行必须与 `columns` 等长。新增不需要 `record_id`。
        confirm_write: 默认 false，只返回新增预演；true 时跳过 dry-run 报告并真实创建记录。
        batch_size: 多行新增时每次 batch_create 的记录数，默认 500；传 1 可退回逐条创建。

    返回:
        dry-run: `{"ok": true, "summary": {"to_create": 2, "to_skip": 0}, "dry_run": true}`
        写入: `{"ok": true, "created": [{"record_id": "..."}], ...}`
    """
    token = _get_tenant_access_token(credentials)
    ref = _resolve_table_and_view(app_token, table_name, view_name=view_name, table_id=table_id, token=token)
    schema = _fetch_view_schema(ref, token=token)
    patches = []
    for values in rows:
        if len(values) != len(columns):
            patches.append(NormalizedPatch({}, [NormalizedWriteValue('(row)', '', 0, values, None, False, error=f'列数{len(columns)} 与 值{len(values)} 不匹配')]))
        else:
            patches.append(build_patch_dict(dict(zip(columns, values)), schema.fields, ''))
    if not confirm_write:
        return _build_create_dry_run(patches)
    errors = _patches_errors(patches)
    if errors:
        return {'ok': False, 'errors': errors, 'warnings': [], 'summary': {'to_create': 0, 'to_skip': len(patches)}, 'dry_run': False}
    records_fields = []
    for patch in patches:
        fields = _patch_to_field_name_fields(patch)
        if fields:
            records_fields.append(fields)
    if batch_size < 1:
        raise ValueError('batch_size 必须大于等于 1')
    created = []
    batch_count = 0
    if len(records_fields) == 1 or batch_size == 1:
        for fields in records_fields:
            created.append(_create_record(ref, fields, token=token))
            batch_count += 1
        return {'ok': True, 'created': created, 'summary': {'created': len(created), 'request_count': batch_count}}
    for start in range(0, len(records_fields), batch_size):
        batch = records_fields[start:start + batch_size]
        created.extend(_create_records_batch(ref, batch, token=token))
        batch_count += 1
    return {'ok': True, 'created': created, 'summary': {'created': len(created), 'request_count': batch_count}}

def query_linked_record_ids_by_records(app_token, table_name, record_ids, column_name, view_name=None, table_id=None, credentials=None):
    """
    读取一批记录的指定关联列，返回每条记录关联到的 record_id 列表。

    返回:
        `{"recxxx": ["recyyy", "reczzz"], ...}`
    """
    token = _get_tenant_access_token(credentials)
    ref = _resolve_table_and_view(app_token, table_name, view_name=view_name, table_id=table_id, token=token)
    schema = _fetch_view_schema(ref, token=token)
    field = schema.fields.get(column_name)
    if field is None:
        raise KeyError(f'列 {column_name!r} 不存在. 可用列: {list(schema.fields)}')
    result = {}
    for record_id in record_ids:
        rid = str(record_id or '').strip()
        if not rid:
            continue
        record = _read_record(ref, rid, token=token)
        raw = get_field_raw(record, field)
        result[rid] = _extract_link_record_ids(raw)
    return result

def list_result_to_csv_string(res):
    buf = StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(res.columns)
    for row in res.rows:
        w.writerow(['' if c is None else c for c in row])
    return buf.getvalue()

def rows_to_csv(columns, rows=None, *, rbr=None):
    buf = StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(columns)
    if rows is not None:
        for r in rows:
            w.writerow(['' if c is None else c for c in r])
    elif rbr and columns[0:1] == ['record_id']:
        rest = columns[1:]
        n = len(rest)
        for rid, vals in rbr.items():
            vlist = (vals or [])[:n]
            w.writerow([rid] + ['' if v is None else v for v in vlist] + [''] * (n - len(vlist)))
    elif rbr and columns[0:1] != ['record_id']:
        n = len(columns)
        for _rid, vals in rbr.items():
            vlist = (vals or [])[:n]
            w.writerow(['' if v is None else v for v in vlist] + [''] * (n - len(vlist)))
    return buf.getvalue()

def _reference_usage_cases():
    """RPA 函数调用参考：复制需要的调用片段后填入自己的 app_token/table_name。"""
    app_token = "base_xxx"
    table_name = "示例数据表"
    tables = list_bitable_tables(app_token)
    all_rows = query_records_by_time(app_token=app_token, table_name=table_name)
    current_month_rows = query_records_by_time(app_token=app_token, table_name=table_name, time_column='申请时间', condition='CurrentMonth')
    yesterday_rows = query_records_by_time(app_token=app_token, table_name=table_name, time_column='申请时间', condition='Yesterday', query_columns=['状态', '名称', '数量'])
    specific_date_rows = query_records_by_time(app_token=app_token, table_name=table_name, time_column='申请时间', start_date='2026/04/28', end_date='2026/04/28')
    range_rows = query_records_by_time(app_token=app_token, table_name=table_name, time_column='申请时间', start_date='2026-04-26', end_date='2026-04-28', query_columns=['状态', '名称', '数量'])
    dry_run = dry_run_update_by_names(app_token=app_token, table_name=table_name, record_id='recxxxx', columns=['状态'], values=['已完成'])
    write_result = update_record_by_names(app_token=app_token, table_name=table_name, record_id='recxxxx', columns=['状态'], values=['已完成'], confirm_write=True)
    create_result = create_records_by_names(app_token=app_token, table_name=table_name, columns=['名称', '数量', '状态'], rows=[['示例名称 A', 1, '待处理'], ['示例名称 B', 2, '待处理']])
    linked_record_ids = query_linked_record_ids_by_records(app_token=app_token, table_name=table_name, record_ids=['recxxxx'], column_name='关联活动机制')
    _ = (tables, all_rows, current_month_rows, yesterday_rows, specific_date_rows, range_rows, dry_run, write_result, create_result, linked_record_ids)
if __name__ == '__main__':
    print('feishu_bitable_utils.py 提供 RPA 可直接 import 的函数；请查看文件底部 _reference_usage_cases() 中的调用案例。')
