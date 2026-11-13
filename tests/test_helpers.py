"""시트에 접속하지 않고 확인할 수 있는 순수 함수 시험."""

import datetime as dt

import pytest

from async_sheet_mcp import server as s


def test_norm_ignores_spaces_newlines_and_trailing_dot():
    assert s._norm("No.") == "No"
    assert s._norm("발생\n일자") == "발생일자"
    assert s._norm("보조 설명") == "보조설명"


def test_no_normalizes_sheet_numbers():
    assert s._no("12.0") == "12"
    assert s._no(" 7 ") == "7"
    assert s._no("") == ""
    assert s._no("메모") == "메모"


def test_find_header_skips_title_rows_and_maps_columns():
    rows = [
        ["", "", "完"],
        ["▶ 프로젝트 : 예시"],
        ["No.", "발생\n일자", "문제점", "보조 설명", "상태", "비고"],
        ["1", "2026-01-02", "…", "", "등록", ""],
    ]
    head, cols = s._find_header(rows)
    assert head == 3
    assert cols == {"No": 1, "발생일자": 2, "문제점": 3, "보조설명": 4, "상태": 5, "비고": 6}


def test_find_header_missing():
    with pytest.raises(RuntimeError):
        s._find_header([["번호", "내용"]])


def test_cell_handles_short_rows_and_unknown_columns():
    cols = {"No": 1, "상태": 5}
    assert s._cell(["3", "a"], cols, "상태") == ""
    assert s._cell(["3", "", "", "", " 등록 "], cols, "상태") == "등록"
    assert s._cell(["3"], cols, "없는열") == ""


def test_comment_line_format():
    now = dt.datetime(2026, 9, 30, 21, 0)
    assert s._comment_line("  고쳤습니다 ", now) == f"[9/30 {s.TAG}] 고쳤습니다"
    assert s._comment_line("x", now, date="10/1").startswith("[10/1 ")


def test_need_px_grows_with_text_and_narrow_columns():
    one = s._need_px("짧은 글", 300)
    long = s._need_px("가" * 200, 300)
    narrow = s._need_px("가" * 200, 100)
    assert one == 27  # 한 줄
    assert long > one and narrow > long
    assert s._need_px("a\nb\nc", 300) == 3 * 17 + 10
