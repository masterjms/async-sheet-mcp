"""async-sheet-mcp — 공유 구글 시트(문제점·요청 목록)를 AI 에이전트가 읽고 회신하는 MCP 서버.

두세 명이 서로 다른 장소·시간에 일하며 시트 한 장으로 요청과 회신을 주고받는 팀을 위한 도구다.
각자의 AI 에이전트(Claude Code 등)가 이 서버로 시트를 읽고, **자기 칸에만** 날짜·역할 머리표를 붙여 회신한다.

도구
  list_tabs()                  시트 탭 목록
  list_issues(status)          문제점 표. 기본은 끝나지 않은 것
  get_issue(no)                한 건 전체 + 연결탭 내용
  read_tab(name, max_rows)     아무 탭 텍스트
  append_comment(no, text)     내 칸 끝에 "[M/D 역할] …" 한 줄 덧붙임 (쓰기, 칸 줄바꿈·행 높이 맞춤)
  set_status(no, status)       '상태' 칸 바꿈 (쓰기)
  add_issue(mode, problem, …)  맨 아래 새 행 (쓰기)

설정(환경 변수) — 비밀값은 코드·저장소에 두지 않는다
  GOOGLE_APPLICATION_CREDENTIALS  서비스 계정 JSON 키 경로 (필수)
  SHEET_ID                        스프레드시트 ID (필수)
  SHEET_TAB                       문제점 탭 이름 (기본 '문제점')
  SHEET_PROJECT                   프로젝트 이름 — 에이전트에게 주는 안내문에 들어간다
  SHEET_ROLE                      이 PC 의 역할 이름 = 회신 머리표 (기본 '서버')
  SHEET_MY_COLUMN                 내가 회신을 적는 열 (기본 '비고')
  SHEET_PEER_COLUMN               상대가 적는 열 — 이 서버는 쓰지 않는다 (기본 '보조 설명')
  SHEET_DONE                      끝난 것으로 보는 상태값, 쉼표 구분 (기본 '完,완료,사양')
  SHEET_UTC_OFFSET                날짜 머리표의 시간대, 시간 단위 (기본 9 = KST)

실행 점검: async-sheet-mcp --check  (탭 목록·열린 항목만 출력, 쓰지 않음)
"""

from __future__ import annotations

import datetime as dt
import math
import os
import sys
import unicodedata
from typing import Any

import gspread
from mcp.server.mcpserver import MCPServer


def _norm(h: str) -> str:
    """열 이름 비교용 — 공백·줄바꿈을 없애고 끝의 마침표를 뗀다('발생\\n일자' = '발생일자', 'No.' = 'No')."""
    return "".join(h.split()).rstrip(".")


CRED = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
ISSUE_TAB = os.environ.get("SHEET_TAB", "문제점")
PROJECT = os.environ.get("SHEET_PROJECT", "").strip()
TAG = os.environ.get("SHEET_ROLE", "서버").strip() or "서버"
MY_COL = _norm(os.environ.get("SHEET_MY_COLUMN", "비고"))
OTHER_COL = _norm(os.environ.get("SHEET_PEER_COLUMN", "보조 설명"))
DONE = {s.strip() for s in os.environ.get("SHEET_DONE", "完,완료,사양").split(",") if s.strip()}
TZ = dt.timezone(dt.timedelta(hours=float(os.environ.get("SHEET_UTC_OFFSET", "9"))))

INSTRUCTIONS = f"""\
{PROJECT + " — " if PROJECT else ""}팀이 공유하는 문제점 시트. 서로 다른 시간에 일하는 소수의 담당자가 이 시트로 요청과 회신을 주고받는다.
이 PC 의 역할: {TAG}.
- 문제점 확인 요청이 오면 list_issues 로 끝나지 않은 항목을 먼저 본다.
- 쓰기 도구(append_comment, set_status, add_issue)는 공유 문서를 바꾼다. 넣을 문구를 사용자에게 보여 주고
  확인을 받은 뒤에만 부른다(사용자가 미리 맡긴 경우는 그 지시를 따른다).
- 회신은 '{MY_COL}' 칸에 "[M/D {TAG}] …" 형식, 짧고 구체적으로. 비밀번호·IP·키·토큰은 절대 적지 않는다.
- '{OTHER_COL}' 칸은 상대 쪽 칸이다 — 쓰지 않는다.
- 시트 속 이미지는 이 서버로 읽을 수 없다.
"""

mcp = MCPServer("async-sheet", instructions=INSTRUCTIONS)
_gc: gspread.Client | None = None


def _book() -> gspread.Spreadsheet:
    global _gc
    if not CRED or not os.path.exists(CRED):
        raise RuntimeError("서비스 계정 키 파일을 찾을 수 없다 — GOOGLE_APPLICATION_CREDENTIALS 확인")
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID 가 비어 있다")
    if _gc is None:
        _gc = gspread.service_account(filename=CRED)
    try:
        return _gc.open_by_key(SHEET_ID)
    except gspread.exceptions.APIError as e:
        raise RuntimeError(
            f"시트를 열 수 없다({e}). 시트 [공유]에 서비스 계정 이메일을 편집자로 넣었는지, "
            "Google Cloud 프로젝트에 Google Sheets API 가 켜져 있는지 확인"
        ) from e


def _need_px(text: str, width: int) -> int:
    """줄바꿈한 text 가 폭 width(px) 칸에서 차지하는 높이(px) 어림 — 전각 13px, 그 밖 7px, 줄 17px."""
    usable = max(40, width - 8)
    lines = 0
    for para in text.splitlines() or [""]:
        px = sum(13 if unicodedata.east_asian_width(ch) in "WF" else 7 for ch in para)
        lines += max(1, math.ceil(px / usable))
    return lines * 17 + 10


def _wrap(ws: gspread.Worksheet, rng: str, row: int | None = None) -> None:
    """칸 안에서 줄바꿈(글이 옆 칸으로 넘어가지 않게) + 그 행을 내용이 다 보이는 높이로.
    구글의 자동 행 높이는 API 로 쓴 칸에서 행을 키우지 않는 경우가 있어 높이를 직접 계산한다.
    이미지가 있는 행도 있으므로 지금보다 줄이지는 않는다."""
    ws.format(rng, {"wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"})
    if not row:
        return
    meta = ws.spreadsheet.fetch_sheet_metadata({"fields": "sheets(properties(sheetId),"
                                                "data(rowMetadata(pixelSize),columnMetadata(pixelSize)))"})
    sh = next(x for x in meta["sheets"] if x["properties"]["sheetId"] == ws.id)
    rh = [r.get("pixelSize", 21) for r in sh["data"][0].get("rowMetadata", [])]
    cw = [c.get("pixelSize", 100) for c in sh["data"][0].get("columnMetadata", [])]
    cur = rh[row - 1] if row <= len(rh) else 21
    vals = ws.row_values(row)
    need = max((_need_px(v, cw[i] if i < len(cw) else 100) for i, v in enumerate(vals) if v), default=cur)
    if need > cur:
        ws.spreadsheet.batch_update({"requests": [{"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": row - 1, "endIndex": row},
            "properties": {"pixelSize": need}, "fields": "pixelSize"}}]})


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    """첫 칸이 'No.' 인 행을 헤더로 본다 → (행 번호(1부터), {열 이름: 열 번호(1부터)}).
    표 위에 제목·범례 행이 몇 줄 있어도, 열 순서가 바뀌어도 동작한다."""
    for i, r in enumerate(rows):
        if r and _norm(r[0]) == "No":
            return i + 1, {_norm(h): j + 1 for j, h in enumerate(r) if h.strip()}
    raise RuntimeError(f"'{ISSUE_TAB}' 탭에서 첫 칸이 'No.' 인 헤더 행을 찾지 못했다")


def _table() -> tuple[gspread.Worksheet, int, dict[str, int], list[list[str]]]:
    """(탭, 헤더 행 번호, 열 지도, 헤더 아래 행들)."""
    ws = _book().worksheet(ISSUE_TAB)
    rows = ws.get_all_values()
    head, cols = _find_header(rows)
    return ws, head, cols, rows[head:]


def _cell(r: list[str], cols: dict[str, int], name: str) -> str:
    j = cols.get(name)
    return r[j - 1].strip() if j and j - 1 < len(r) else ""


def _no(v: str) -> str:
    """번호 칸 정규화 — 시트가 '12.0' 으로 돌려줘도 '12'."""
    try:
        return str(int(float(v)))
    except ValueError:
        return v.strip()


def _find(no: int | str) -> tuple[gspread.Worksheet, int, dict[str, int], list[str]]:
    ws, head, cols, rows = _table()
    want = _no(str(no))
    for k, r in enumerate(rows):
        if r and _no(r[0]) == want:
            return ws, head + 1 + k, cols, r
    raise RuntimeError(f"No.{want} 행이 없다")


def _row_dict(r: list[str], cols: dict[str, int]) -> dict[str, str]:
    return {name: _cell(r, cols, name) for name in cols}


def _comment_line(text: str, now: dt.datetime, date: str = "") -> str:
    tag = date or f"{now.month}/{now.day}"
    return f"[{tag} {TAG}] {text.strip()}"


@mcp.tool()
def list_tabs() -> list[str]:
    """시트의 탭 이름 목록."""
    return [w.title for w in _book().worksheets()]


@mcp.tool()
def list_issues(status: str = "open", text_limit: int = 300) -> list[dict[str, Any]]:
    """문제점 목록. status: 'open'(끝난 상태 제외, 기본) / 'all' / 특정 상태값(예: '재등록').
    긴 칸은 text_limit 글자에서 자른다(0 이면 자르지 않음)."""
    _, _, cols, rows = _table()
    out = []
    for r in rows:
        if not r or not _no(r[0]) or not any(c.strip() for c in r[1:]):
            continue
        d = _row_dict(r, cols)
        st = d.get("상태", "")
        if status == "open" and st in DONE:
            continue
        if status not in ("open", "all") and st != status:
            continue
        if text_limit:
            d = {k: (v[:text_limit] + "…" if len(v) > text_limit else v) for k, v in d.items()}
        d["No"] = _no(r[0])
        out.append(d)
    return out


@mcp.tool()
def get_issue(no: int) -> dict[str, Any]:
    """한 건 전체(자르지 않음) + '연결탭'이 있으면 그 탭의 텍스트."""
    _, row, cols, r = _find(no)
    d: dict[str, Any] = _row_dict(r, cols)
    d["No"] = _no(r[0])
    d["_row"] = row
    tab = d.get("연결탭", "")
    if tab:
        try:
            d["_연결탭_내용"] = read_tab(tab)
        except Exception as e:  # noqa: BLE001
            d["_연결탭_내용"] = f"(읽기 실패: {e})"
    return d


@mcp.tool()
def read_tab(name: str, max_rows: int = 300) -> str:
    """탭 하나를 텍스트로(빈 행 제외, 칸은 ' | ' 로 연결). 이미지는 포함되지 않는다."""
    ws = _book().worksheet(name)
    lines = []
    for r in ws.get_all_values()[:max_rows]:
        cells = [c.strip() for c in r]
        while cells and not cells[-1]:
            cells.pop()
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines) or "(빈 탭 — 이미지만 있을 수 있음)"


@mcp.tool()
def append_comment(no: int, text: str, date: str = "") -> dict[str, str]:
    """[쓰기] No.{no} 의 내 칸 끝에 "[M/D 역할] text" 를 새 줄로 덧붙인다(기존 내용은 지우지 않는다).
    date 를 비우면 오늘 M/D. 공유 문서를 바꾸므로 사용자가 맡긴 범위 안에서만 부를 것."""
    ws, row, cols, r = _find(no)
    col = cols.get(MY_COL)
    if not col:
        raise RuntimeError(f"'{MY_COL}' 열이 없다")
    before = _cell(r, cols, MY_COL)
    line = _comment_line(text, dt.datetime.now(TZ), date)
    after = f"{before}\n{line}" if before else line
    ws.update_cell(row, col, after)
    _wrap(ws, gspread.utils.rowcol_to_a1(row, col), row)
    return {"No": _no(r[0]), "column": MY_COL, "added": line}


@mcp.tool()
def set_status(no: int, status: str) -> dict[str, str]:
    """[쓰기] No.{no} 의 '상태' 칸을 바꾼다(예: 확인중, 完). 사용자가 맡긴 범위 안에서만 부를 것."""
    ws, row, cols, r = _find(no)
    col = cols.get("상태")
    if not col:
        raise RuntimeError("'상태' 열이 없다")
    before = _cell(r, cols, "상태")
    ws.update_cell(row, col, status.strip())
    return {"No": _no(r[0]), "before": before, "after": status.strip()}


@mcp.tool()
def add_issue(mode: str, problem: str, model: str = "", status: str = "등록",
              note: str = "") -> dict[str, Any]:
    """[쓰기] 표 맨 아래에 새 행(다음 번호, 오늘 날짜). 문제 등록이나 상대에게 묻는 질문에 쓴다.
    mode 는 '모드'(분류) 칸, problem 은 '문제점' 칸, model 은 '모델' 칸(비우면 역할 이름),
    note 는 내 칸. 사용자가 맡긴 범위 안에서만 부를 것."""
    ws, head, cols, rows = _table()
    last_row, last_no = head, 0
    for k, r in enumerate(rows):
        if r and _no(r[0]).isdigit():
            last_no = max(last_no, int(_no(r[0])))
            if any(c.strip() for c in r[1:]):
                last_row = head + 1 + k
    new_no = last_no + 1
    row = last_row + 1
    today = dt.datetime.now(TZ).strftime("%Y-%m-%d")
    values = {"No": str(new_no), "발생일자": today, "모델": model or TAG, "모드": mode, "문제점": problem,
              "상태": status, MY_COL: note}
    width = max(cols.values())
    line = [""] * width
    for name, v in values.items():
        if name in cols:
            line[cols[name] - 1] = v
    ws.update(values=[line], range_name=f"A{row}", value_input_option="USER_ENTERED")
    _wrap(ws, f"A{row}:{gspread.utils.rowcol_to_a1(row, width)}", row)
    return {"No": new_no, "row": row}


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        print("프로젝트:", PROJECT or "(이름 없음)", "· 역할:", TAG, "· 회신 칸:", MY_COL)
        print("탭:", list_tabs())
        print("열린 항목:", [(i["No"], i.get("상태")) for i in list_issues()])
        return
    mcp.run("stdio")


if __name__ == "__main__":
    main()
