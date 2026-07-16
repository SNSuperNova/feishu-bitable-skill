"""
飞书在线表格（Sheets）RPA 工具。

只依赖标准库和 requests。面向 RPA 直接 import 函数调用，不使用函数参数/返回值类型注解。

主要能力：
1. 按 sheet 名读取整页数据。
2. 按 sheet 名、列名、匹配值查询匹配行。
3. 按 sheet 名、定位列匹配行，并更新该行指定列。
4. 按表头清理或批量写入第 2 行往下的数据。
"""
import os
import time as _time
from pathlib import Path
from urllib.parse import quote

FEISHU_APP_CREDENTIALS = {"app_id": "", "app_secret": ""}
DEFAULT_ENV_FILE = str(Path(__file__).with_name(".env"))
FEISHU_APP_CREDENTIAL_ENV_KEYS = {
    "app_id": "FEISHU_APP_ID",
    "app_secret": "FEISHU_APP_SECRET",
}
FEISHU_OPEN_API_BASE = "https://open.feishu.cn/open-apis"

# 长进程/RPA 会频繁重复查询同一份表格，缓存 token 和 sheet 元数据能减少接口调用。
_TENANT_TOKEN_CACHE = {}
_SHEETS_CACHE = {}


def clear_feishu_sheets_cache():
    """清空本进程内 token/sheet 元数据缓存。"""
    _TENANT_TOKEN_CACHE.clear()
    _SHEETS_CACHE.clear()


def _clean_config_value(value):
    return str(value or "").strip()


def load_dotenv_values(env_file=DEFAULT_ENV_FILE):
    """读取简单 KEY=value 格式的 .env，避免额外依赖 python-dotenv。"""
    values = {}
    if not env_file:
        return values
    path = Path(env_file)
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_app_credentials(app_id=None, app_secret=None, env_file=DEFAULT_ENV_FILE):
    dotenv = load_dotenv_values(env_file)

    def pick(explicit, default_value, env_key):
        if explicit:
            return _clean_config_value(explicit)
        if os.environ.get(env_key):
            return _clean_config_value(os.environ.get(env_key))
        if dotenv.get(env_key):
            return _clean_config_value(dotenv.get(env_key))
        if default_value:
            return _clean_config_value(default_value)
        return ""

    return {
        "app_id": pick(app_id, FEISHU_APP_CREDENTIALS.get("app_id", ""), FEISHU_APP_CREDENTIAL_ENV_KEYS["app_id"]),
        "app_secret": pick(app_secret, FEISHU_APP_CREDENTIALS.get("app_secret", ""), FEISHU_APP_CREDENTIAL_ENV_KEYS["app_secret"]),
    }


def require_app_credentials(credentials=None, env_file=DEFAULT_ENV_FILE):
    creds = credentials or load_app_credentials(env_file=env_file)
    missing = []
    if not creds.get("app_id"):
        missing.append(FEISHU_APP_CREDENTIAL_ENV_KEYS["app_id"])
    if not creds.get("app_secret"):
        missing.append(FEISHU_APP_CREDENTIAL_ENV_KEYS["app_secret"])
    if missing:
        raise ValueError(
            "未配置飞书开放平台应用凭证: "
            + ", ".join(missing)
            + "。请在 .env 中配置，或在 FEISHU_APP_CREDENTIALS 中填写。"
        )
    return creds


def _require_requests():
    try:
        import requests
    except Exception as exc:
        raise RuntimeError("需要安装 requests 才能调用飞书开放 API") from exc
    return requests


def _get_tenant_access_token(credentials=None, env_file=DEFAULT_ENV_FILE):
    creds = require_app_credentials(credentials, env_file=env_file)
    now = _time.time()
    cache_key = (creds.get("app_id"), str(env_file))
    cached = _TENANT_TOKEN_CACHE.get(cache_key)
    if cached and cached.get("expires_at", 0) > now:
        return cached.get("token", "")
    requests = _require_requests()
    response = requests.post(
        FEISHU_OPEN_API_BASE + "/auth/v3/tenant_access_token/internal",
        json={"app_id": creds.get("app_id"), "app_secret": creds.get("app_secret")},
        timeout=20,
    )
    data = response.json()
    if data.get("code") != 0:
        safe = {k: v for k, v in data.items() if k != "tenant_access_token"}
        raise RuntimeError("获取 tenant_access_token 失败: " + repr(safe))
    token = str(data.get("tenant_access_token", ""))
    ttl = int(data.get("expire") or 6900)
    _TENANT_TOKEN_CACHE[cache_key] = {"token": token, "expires_at": now + max(60, ttl - 120)}
    return token


def _feishu_request(method, path, token, params=None, json_body=None):
    requests = _require_requests()
    response = requests.request(
        method,
        FEISHU_OPEN_API_BASE + path,
        headers={"Authorization": "Bearer " + token},
        params=params or {},
        json=json_body,
        timeout=30,
    )
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError("飞书 API 返回非 JSON: " + response.text[:500]) from exc
    if data.get("code") != 0:
        raise RuntimeError("飞书 API 调用失败: " + repr(data))
    return data


def _quote_range(range_name):
    """飞书 Sheets v2 的 range 放在 URL path 中，需要保留 A1 语法字符。"""
    return quote(range_name, safe="!:$")


def _col_index_to_letter(index):
    if index < 1:
        raise ValueError("列序号必须从 1 开始")
    letters = []
    while index:
        index, rem = divmod(index - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _normalize_column_letter(column):
    letter = str(column or "").strip().upper()
    if not letter or not letter.isalpha():
        raise ValueError("列标识必须是 A、B、C、AB 这类字母: " + repr(column))
    return letter


def _target_column_letters(columns):
    if isinstance(columns, str):
        result = [_normalize_column_letter(columns)]
    else:
        result = [_normalize_column_letter(column) for column in (columns or [])]
    if not result:
        raise ValueError("列标识不能为空")
    return result


def _trim_empty_tail(values):
    out = list(values or [])
    while out and _normalize_cell(out[-1]) == "":
        out.pop()
    return out


def _trim_empty_row_tail(rows):
    out = list(rows or [])
    while out and not _trim_empty_tail(out[-1]):
        out.pop()
    return out


def _normalize_cell(value):
    value = _sheet_cell_display(value)
    if value is None:
        return ""
    return str(value).strip()


def _sheet_cell_display(cell):
    """把飞书 Sheets 富文本/链接对象压成 RPA 友好的展示值。"""
    if cell is None:
        return None
    if isinstance(cell, list):
        values = [_sheet_cell_display(item) for item in cell]
        values = [value for value in values if value is not None]
        if not values:
            return None
        if all(isinstance(value, str) for value in values):
            return "".join(values)
        return values[0] if len(values) == 1 else values
    if isinstance(cell, dict):
        if isinstance(cell.get("value"), list):
            return _sheet_cell_display(cell.get("value"))
        if cell.get("text") is not None:
            return cell.get("text")
        if cell.get("name") is not None:
            return cell.get("name")
        if cell.get("link") is not None:
            return cell.get("link")
        if cell.get("record_id") is not None:
            return cell.get("record_id")
        if cell.get("date") is not None:
            return cell.get("date")
        if cell.get("text_arr") is not None:
            return _sheet_cell_display(cell.get("text_arr"))
        return cell
    return cell


def _sheet_row_display(row_values):
    return [_sheet_cell_display(value) for value in (row_values or [])]


def _match_value(cell_value, expected, exact_match=True, case_sensitive=True):
    left = _normalize_cell(cell_value)
    right = _normalize_cell(expected)
    if not case_sensitive:
        left = left.lower()
        right = right.lower()
    if exact_match:
        return left == right
    return right in left


def _sheet_row_count(sheet):
    for key in ("row_count", "rowCount"):
        if sheet.get(key):
            return int(sheet.get(key))
    grid = sheet.get("grid_properties") or sheet.get("gridProperties") or {}
    for key in ("row_count", "rowCount"):
        if grid.get(key):
            return int(grid.get(key))
    return 5000


def _sheet_column_count(sheet):
    for key in ("column_count", "columnCount"):
        if sheet.get(key):
            return int(sheet.get(key))
    grid = sheet.get("grid_properties") or sheet.get("gridProperties") or {}
    for key in ("column_count", "columnCount"):
        if grid.get(key):
            return int(grid.get(key))
    return 200


def _list_sheets_raw(spreadsheet_token, token):
    cache_key = spreadsheet_token
    if cache_key in _SHEETS_CACHE:
        return _SHEETS_CACHE[cache_key]
    data = _feishu_request(
        "GET",
        "/sheets/v3/spreadsheets/" + spreadsheet_token + "/sheets/query",
        token,
    )
    body = data.get("data") or {}
    sheets = body.get("sheets") or body.get("items") or []
    _SHEETS_CACHE[cache_key] = sheets
    return sheets


def list_sheets(spreadsheet_token, credentials=None):
    """列出在线表格里的 sheet 名称和 sheet_id。"""
    token = _get_tenant_access_token(credentials)
    sheets = _list_sheets_raw(spreadsheet_token, token)
    result = []
    for sheet in sheets:
        result.append(
            {
                "sheet_id": sheet.get("sheet_id") or sheet.get("sheetId"),
                "title": sheet.get("title"),
                "row_count": _sheet_row_count(sheet),
                "column_count": _sheet_column_count(sheet),
            }
        )
    return result


def _resolve_sheet(spreadsheet_token, sheet_name, token):
    sheets = _list_sheets_raw(spreadsheet_token, token)
    matches = []
    for sheet in sheets:
        if sheet.get("title") == sheet_name:
            matches.append(sheet)
    if not matches:
        names = [sheet.get("title") for sheet in sheets]
        raise ValueError("找不到 sheet: " + repr(sheet_name) + "。可用 sheet: " + repr(names))
    ids = set([str(s.get("sheet_id") or s.get("sheetId")) for s in matches])
    if len(ids) > 1:
        raise ValueError("sheet 名重复，请调整 sheet 名称或扩展为按 sheet_id 调用: " + repr(sheet_name))
    sheet = matches[0]
    return {
        "sheet_id": sheet.get("sheet_id") or sheet.get("sheetId"),
        "title": sheet.get("title"),
        "row_count": _sheet_row_count(sheet),
        "column_count": _sheet_column_count(sheet),
    }


def _get_values(spreadsheet_token, range_name, token):
    data = _feishu_request(
        "GET",
        "/sheets/v2/spreadsheets/" + spreadsheet_token + "/values/" + _quote_range(range_name),
        token,
    )
    body = data.get("data") or {}
    value_range = body.get("valueRange") or body.get("value_range") or body
    return value_range.get("values") or []


def _update_values(spreadsheet_token, range_name, values, token):
    data = _feishu_request(
        "PUT",
        "/sheets/v2/spreadsheets/" + spreadsheet_token + "/values",
        token,
        json_body={"valueRange": {"range": range_name, "values": values}},
    )
    return data.get("data") or data


def _batch_update_values(spreadsheet_token, value_ranges, token):
    data = _feishu_request(
        "POST",
        "/sheets/v2/spreadsheets/" + spreadsheet_token + "/values_batch_update",
        token,
        json_body={"valueRanges": value_ranges},
    )
    return data.get("data") or data


def _apply_value_ranges(spreadsheet_token, value_ranges, token):
    try:
        api_result = _batch_update_values(spreadsheet_token, value_ranges, token)
        return api_result, 1, []
    except Exception as batch_exc:
        responses = []
        for value_range in value_ranges:
            responses.append(_update_values(spreadsheet_token, value_range.get("range"), value_range.get("values"), token))
        return responses, len(value_ranges), ["batch_update 失败，已退回逐列更新: " + str(batch_exc)]


def _read_sheet_table(spreadsheet_token, sheet_name, token, header_row=1, max_rows=None, max_columns=None):
    """按第一行表头读取二维区域，后续查询/更新都基于列名定位。"""
    sheet = _resolve_sheet(spreadsheet_token, sheet_name, token)
    row_count = max_rows or sheet.get("row_count") or 5000
    column_count = max_columns or sheet.get("column_count") or 200
    last_col = _col_index_to_letter(int(column_count))
    range_name = "%s!A%s:%s%s" % (sheet.get("sheet_id"), int(header_row), last_col, int(row_count))
    values = _get_values(spreadsheet_token, range_name, token)
    if not values:
        return {
            "sheet": sheet,
            "headers": [],
            "rows": [],
            "start_row_number": int(header_row) + 1,
            "range": range_name,
        }
    headers = [_normalize_cell(x) for x in _trim_empty_tail(values[0])]
    rows = [_sheet_row_display(row_values) for row_values in values[1:]]
    return {
        "sheet": sheet,
        "headers": headers,
        "rows": rows,
        "start_row_number": int(header_row) + 1,
        "range": range_name,
    }


def _column_index(headers, column_name):
    target = _normalize_cell(column_name)
    matches = [idx for idx, name in enumerate(headers) if _normalize_cell(name) == target]
    if not matches:
        raise KeyError("列不存在: " + repr(column_name) + "。可用列: " + repr(headers))
    if len(matches) > 1:
        raise ValueError("列名重复: " + repr(column_name))
    return matches[0]


def _target_headers(headers):
    if isinstance(headers, str):
        result = [_normalize_cell(headers)]
    else:
        result = [_normalize_cell(header) for header in (headers or [])]
    result = [header for header in result if header]
    if not result:
        raise ValueError("表头不能为空")
    return result


def _column_indexes_by_headers(headers, target_headers):
    indexes = {}
    for header in target_headers:
        indexes[header] = _column_index(headers, header)
    return indexes


def _unique_headers(headers):
    counts = {}
    unique = []
    for idx, name in enumerate(headers or []):
        base = _normalize_cell(name) or "列" + str(idx + 1)
        count = counts.get(base, 0) + 1
        counts[base] = count
        unique.append(base if count == 1 else base + "_" + str(count))
    return unique


def _row_to_dict(headers, row_values):
    result = {}
    for idx, name in enumerate(_unique_headers(headers)):
        result[name] = row_values[idx] if idx < len(row_values) else None
    return result


def clear_sheet_columns_by_headers(spreadsheet_token, sheet_name, headers, header_row=1, start_row=2, end_row=None, confirm_write=False):
    """
    按指定表头清理下面所有行数据。

    默认从第 2 行开始清理到 sheet 当前最大行数；传 confirm_write=True 才真实写入空值。
    """
    target_headers = _target_headers(headers)
    token = _get_tenant_access_token()
    table = _read_sheet_table(spreadsheet_token, sheet_name, token, header_row, 1, None)
    sheet = table.get("sheet") or {}
    sheet_id = sheet.get("sheet_id")
    sheet_row_count = int(sheet.get("row_count") or 5000)
    start_row = int(start_row)
    end_row = int(end_row or sheet_row_count)
    if start_row <= int(header_row):
        raise ValueError("start_row 必须大于 header_row")
    if end_row < start_row:
        raise ValueError("end_row 必须大于等于 start_row")

    column_indexes = _column_indexes_by_headers(table.get("headers") or [], target_headers)
    row_count = end_row - start_row + 1
    value_ranges = []
    preview = []
    for header in target_headers:
        idx = column_indexes[header]
        col_letter = _col_index_to_letter(idx + 1)
        range_name = "%s!%s%s:%s%s" % (sheet_id, col_letter, start_row, col_letter, end_row)
        values = [[""] for _ in range(row_count)]
        value_ranges.append({"range": range_name, "values": values})
        preview.append({"header": header, "range": range_name, "rows": row_count})

    result = {
        "ok": True,
        "sheet_id": sheet_id,
        "sheet_name": sheet_name,
        "headers": target_headers,
        "clears": preview,
        "dry_run": not confirm_write,
    }
    if not confirm_write:
        return result

    api_result, request_count, warnings = _apply_value_ranges(spreadsheet_token, value_ranges, token)
    result["api_response"] = api_result
    result["request_count"] = request_count
    if warnings:
        result["warnings"] = warnings
    result["dry_run"] = False
    return result


def write_sheet_rows_by_headers(spreadsheet_token, sheet_name, headers, rows, header_row=1, start_row=2, confirm_write=False):
    """
    按指定表头从第 2 行向下写入数据。

    rows 支持二维列表，也支持字典列表；传 confirm_write=True 才真实写入。
    """
    target_headers = _target_headers(headers)
    rows = list(rows or [])
    token = _get_tenant_access_token()
    table = _read_sheet_table(spreadsheet_token, sheet_name, token, header_row, 1, None)
    sheet_id = table.get("sheet", {}).get("sheet_id")
    start_row = int(start_row)
    if start_row <= int(header_row):
        raise ValueError("start_row 必须大于 header_row")
    column_indexes = _column_indexes_by_headers(table.get("headers") or [], target_headers)

    value_ranges = []
    preview = []
    if rows:
        end_row = start_row + len(rows) - 1
        for header_pos, header in enumerate(target_headers):
            idx = column_indexes[header]
            col_letter = _col_index_to_letter(idx + 1)
            range_name = "%s!%s%s:%s%s" % (sheet_id, col_letter, start_row, col_letter, end_row)
            values = []
            for row in rows:
                if isinstance(row, dict):
                    value = row.get(header)
                else:
                    value = row[header_pos] if header_pos < len(row) else None
                values.append(["" if value is None else value])
            value_ranges.append({"range": range_name, "values": values})
            preview.append({"header": header, "range": range_name, "rows": len(rows)})

    result = {
        "ok": True,
        "sheet_id": sheet_id,
        "sheet_name": sheet_name,
        "headers": target_headers,
        "row_count": len(rows),
        "writes": preview,
        "dry_run": not confirm_write,
    }
    if not confirm_write or not value_ranges:
        return result

    api_result, request_count, warnings = _apply_value_ranges(spreadsheet_token, value_ranges, token)
    result["api_response"] = api_result
    result["request_count"] = request_count
    if warnings:
        result["warnings"] = warnings
    result["dry_run"] = False
    return result


def clear_sheet_columns_by_letters(spreadsheet_token, sheet_name, columns, start_row=2, end_row=None, confirm_write=False):
    """
    按 A/B/C/AB 这类列标识清理下方行数据。

    默认从第 2 行清理到 sheet 当前最大行数；传 confirm_write=True 才真实写入空值。
    """
    target_columns = _target_column_letters(columns)
    token = _get_tenant_access_token()
    sheet = _resolve_sheet(spreadsheet_token, sheet_name, token)
    sheet_id = sheet.get("sheet_id")
    sheet_row_count = int(sheet.get("row_count") or 5000)
    start_row = int(start_row)
    end_row = int(end_row or sheet_row_count)
    if start_row < 1:
        raise ValueError("start_row 必须大于等于 1")
    if end_row < start_row:
        raise ValueError("end_row 必须大于等于 start_row")

    row_count = end_row - start_row + 1
    value_ranges = []
    preview = []
    for column in target_columns:
        range_name = "%s!%s%s:%s%s" % (sheet_id, column, start_row, column, end_row)
        values = [[""] for _ in range(row_count)]
        value_ranges.append({"range": range_name, "values": values})
        preview.append({"column": column, "range": range_name, "rows": row_count})

    result = {
        "ok": True,
        "sheet_id": sheet_id,
        "sheet_name": sheet_name,
        "columns": target_columns,
        "clears": preview,
        "dry_run": not confirm_write,
    }
    if not confirm_write:
        return result

    api_result, request_count, warnings = _apply_value_ranges(spreadsheet_token, value_ranges, token)
    result["api_response"] = api_result
    result["request_count"] = request_count
    if warnings:
        result["warnings"] = warnings
    result["dry_run"] = False
    return result


def write_sheet_rows_by_letters(spreadsheet_token, sheet_name, columns, rows, start_row=2, confirm_write=False):
    """
    按 A/B/C/AB 这类列标识顺序，从指定行向下写入二维列表数据。

    rows 中每行的第 N 个值写入 columns 的第 N 个列；传 confirm_write=True 才真实写入。
    """
    target_columns = _target_column_letters(columns)
    rows = list(rows or [])
    token = _get_tenant_access_token()
    sheet = _resolve_sheet(spreadsheet_token, sheet_name, token)
    sheet_id = sheet.get("sheet_id")
    start_row = int(start_row)
    if start_row < 1:
        raise ValueError("start_row 必须大于等于 1")

    value_ranges = []
    preview = []
    if rows:
        end_row = start_row + len(rows) - 1
        for column_pos, column in enumerate(target_columns):
            range_name = "%s!%s%s:%s%s" % (sheet_id, column, start_row, column, end_row)
            values = []
            for row in rows:
                value = row[column_pos] if column_pos < len(row) else None
                values.append(["" if value is None else value])
            value_ranges.append({"range": range_name, "values": values})
            preview.append({"column": column, "range": range_name, "rows": len(rows)})

    result = {
        "ok": True,
        "sheet_id": sheet_id,
        "sheet_name": sheet_name,
        "columns": target_columns,
        "row_count": len(rows),
        "writes": preview,
        "dry_run": not confirm_write,
    }
    if not confirm_write or not value_ranges:
        return result

    api_result, request_count, warnings = _apply_value_ranges(spreadsheet_token, value_ranges, token)
    result["api_response"] = api_result
    result["request_count"] = request_count
    if warnings:
        result["warnings"] = warnings
    result["dry_run"] = False
    return result


def query_sheet_all_rows(spreadsheet_token, sheet_name, header_row=1, max_rows=None, max_columns=None):
    """
    查询指定名称 sheet 页的全部数据。

    默认把第 1 行作为表头，返回:
        {
            "sheet_id": "...",
            "sheet_name": "...",
            "columns": [...],
            "rows": [[...], [...]],
            "row_dicts": [{"列名": "值"}],
            "range": "sheet_id!A1:Z100"
        }
    """
    token = _get_tenant_access_token()
    table = _read_sheet_table(spreadsheet_token, sheet_name, token, header_row, max_rows, max_columns)
    headers = table.get("headers") or []
    rows = _trim_empty_row_tail(table.get("rows") or [])
    return {
        "sheet_id": table.get("sheet", {}).get("sheet_id"),
        "sheet_name": sheet_name,
        "columns": headers,
        "rows": rows,
        "row_dicts": [_row_to_dict(headers, row_values) for row_values in rows],
        "range": table.get("range"),
    }


def query_sheet_row_by_column(spreadsheet_token, sheet_name, match_column, match_value):
    """
    查询指定 sheet 中某列内容匹配的第一行。

    返回:
        {
            "ok": True,
            "matched": True,
            "sheet_id": "...",
            "row_number": 2,
            "columns": [...],
            "row_values": [...],
            "row": {"列名": "值"}
        }
    """
    token = _get_tenant_access_token()
    table = _read_sheet_table(spreadsheet_token, sheet_name, token, 1, None, None)
    headers = table.get("headers") or []
    match_idx = _column_index(headers, match_column)
    for offset, row_values in enumerate(table.get("rows") or []):
        cell = row_values[match_idx] if match_idx < len(row_values) else None
        if _match_value(cell, match_value, exact_match=True, case_sensitive=True):
            row_number = table.get("start_row_number") + offset
            return {
                "ok": True,
                "matched": True,
                "sheet_id": table.get("sheet", {}).get("sheet_id"),
                "sheet_name": sheet_name,
                "row_number": row_number,
                "columns": headers,
                "row_values": row_values,
                "row": _row_to_dict(headers, row_values),
            }
    return {
        "ok": True,
        "matched": False,
        "sheet_id": table.get("sheet", {}).get("sheet_id"),
        "sheet_name": sheet_name,
        "row_number": None,
        "columns": headers,
        "row_values": [],
        "row": {},
    }


def query_sheet_rows_by_column(spreadsheet_token, sheet_name, match_column, match_value):
    """
    查询指定 sheet 中某列内容匹配的所有行。

    返回:
        {
            "columns": [...],
            "rows": [[...], [...]]
        }
    """
    token = _get_tenant_access_token()
    table = _read_sheet_table(spreadsheet_token, sheet_name, token, 1, None, None)
    headers = table.get("headers") or []
    match_idx = _column_index(headers, match_column)
    matched_rows = []
    for offset, row_values in enumerate(table.get("rows") or []):
        cell = row_values[match_idx] if match_idx < len(row_values) else None
        if _match_value(cell, match_value, exact_match=True, case_sensitive=True):
            matched_rows.append(row_values)
    return {
        "columns": headers,
        "rows": matched_rows
    }


def update_sheet_row_by_column(spreadsheet_token, sheet_name, match_column, match_value, update_columns, update_values=None, confirm_write=False):
    """
    定位指定 sheet 中某列内容匹配的第一行，并更新该行指定列。

    update_columns 支持两种形式：
        1. dict: {"对账日期": "2026-05-12", "状态": "已对账"}
        2. list + update_values: ["对账日期"] 和 ["2026-05-12"]

    默认 confirm_write=False，只返回 dry-run 预览；传 True 才真实写入。
    """
    if isinstance(update_columns, dict):
        updates = dict(update_columns)
    else:
        if update_values is None:
            raise ValueError("update_columns 为列表时必须提供 update_values")
        if len(update_columns) != len(update_values):
            raise ValueError("update_columns 与 update_values 长度不一致")
        updates = dict(zip(update_columns, update_values))
    if not updates:
        raise ValueError("更新内容不能为空")

    token = _get_tenant_access_token()
    table = _read_sheet_table(spreadsheet_token, sheet_name, token, 1, None, None)
    headers = table.get("headers") or []
    match_idx = _column_index(headers, match_column)
    update_indexes = {}
    for col in updates:
        update_indexes[col] = _column_index(headers, col)

    matched_row = None
    matched_row_number = None
    for offset, row_values in enumerate(table.get("rows") or []):
        cell = row_values[match_idx] if match_idx < len(row_values) else None
        if _match_value(cell, match_value, exact_match=True, case_sensitive=True):
            matched_row = row_values
            matched_row_number = table.get("start_row_number") + offset
            break
    if matched_row is None:
        return {
            "ok": False,
            "matched": False,
            "errors": ["未找到匹配行: %s=%s" % (match_column, match_value)],
            "dry_run": not confirm_write,
        }

    sheet_id = table.get("sheet", {}).get("sheet_id")
    value_ranges = []
    preview = []
    for col, value in updates.items():
        idx = update_indexes[col]
        col_letter = _col_index_to_letter(idx + 1)
        range_name = "%s!%s%s:%s%s" % (sheet_id, col_letter, matched_row_number, col_letter, matched_row_number)
        old_value = matched_row[idx] if idx < len(matched_row) else None
        value_ranges.append({"range": range_name, "values": [[value]]})
        preview.append({"column": col, "range": range_name, "old": old_value, "new": value})

    result = {
        "ok": True,
        "matched": True,
        "sheet_id": sheet_id,
        "sheet_name": sheet_name,
        "row_number": matched_row_number,
        "matched_row": _row_to_dict(headers, matched_row),
        "updates": preview,
        "dry_run": not confirm_write,
    }
    if not confirm_write:
        return result

    api_result, request_count, warnings = _apply_value_ranges(spreadsheet_token, value_ranges, token)
    result["api_response"] = api_result
    result["request_count"] = request_count
    if warnings:
        result["warnings"] = warnings
    result["dry_run"] = False
    return result


def _reference_usage_cases():
    spreadsheet_token = "shtcn_xxx"
    sheet_name = "示例Sheet"

    all_rows = query_sheet_all_rows(
        spreadsheet_token=spreadsheet_token,
        sheet_name=sheet_name,
    )

    clear_preview = clear_sheet_columns_by_headers(
        spreadsheet_token=spreadsheet_token,
        sheet_name=sheet_name,
        headers=["状态", "备注"],
    )

    write_preview = write_sheet_rows_by_headers(
        spreadsheet_token=spreadsheet_token,
        sheet_name=sheet_name,
        headers=["名称", "状态"],
        rows=[["示例名称", "待处理"], ["示例名称 2", "已完成"]],
    )

    clear_by_letters_preview = clear_sheet_columns_by_letters(
        spreadsheet_token=spreadsheet_token,
        sheet_name=sheet_name,
        columns=["A", "B", "AB"],
    )

    write_by_letters_preview = write_sheet_rows_by_letters(
        spreadsheet_token=spreadsheet_token,
        sheet_name=sheet_name,
        columns=["A", "B"],
        rows=[["示例名称", "待处理"], ["示例名称 2", "已完成"]],
    )

    matched = query_sheet_row_by_column(
        spreadsheet_token=spreadsheet_token,
        sheet_name=sheet_name,
        match_column="名称",
        match_value="示例名称",
    )

    updated = update_sheet_row_by_column(
        spreadsheet_token=spreadsheet_token,
        sheet_name=sheet_name,
        match_column="名称",
        match_value="示例名称",
        update_columns=["状态"],
        update_values=["已完成"],
        confirm_write=True,
    )

    _ = (
        all_rows,
        clear_preview,
        write_preview,
        clear_by_letters_preview,
        write_by_letters_preview,
        matched,
        updated,
    )


if __name__ == "__main__":
    print("feishu_sheets_utils_rpa.py 提供 RPA 可直接 import 的在线表格读写函数。")
