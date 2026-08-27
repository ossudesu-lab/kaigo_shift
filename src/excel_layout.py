"""Excel帳票のレイアウト読み込みと検証（設計メモ 6.4）

`data/sheet_layout.yaml` を読み、シートの実物と突き合わせる。
read_wishes.py と write_excel.py の両方が使う。

**レイアウトの取り違えは黙って間違った結果を作る。** 帳票には職員行のあいだに
「出　勤」などの集計行が挟まっており、行番号が1つずれるだけで集計行を
職員行と誤認する。値は入っているので「空欄チェック」では捕まらない。
そのため、職員行に**勤務記号以外の値が入っていないか**まで検証する。
"""

from __future__ import annotations

import calendar
from pathlib import Path

import yaml

# 帳票で使う勤務記号。
SHIFT_SYMBOLS = {"Ａ", "ＡC", "Ｂ", "Ｃ", "D", "入", "明", "休", "有"}


def _looks_like_tally(v) -> bool:
    """集計行の値か。数式か数値ならそう。

    レイアウト取り違えの決め手はこれ。帳票は職員行のあいだに「出　勤」などの
    集計行が挟まっており、そこには COUNTIF の数式や 0/1 の数値が並ぶ。
    一方 'PM休' や '13時' のような**手書きの但し書きは職員行に普通に現れる**ので、
    これを取り違えの証拠にしてはいけない（実際それで正常なシートを弾いてしまった）。"""
    s = str(v).strip()
    if s.startswith("="):
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False


def load_layout(data_dir: Path) -> dict:
    with (data_dir / "sheet_layout.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def pick_layout(layout_all: dict, override: str | None = None) -> tuple[str, dict]:
    """使う行レイアウトを決める。日数とは独立（日付列はシートから自動判定する）。"""
    name = override or layout_all["default_layout"]
    if name not in layout_all["layouts"]:
        raise SystemExit(
            "行レイアウト " + repr(name) + " が data/sheet_layout.yaml にありません。"
            + chr(10) + "  --layout で指定してください（候補: "
            + str(list(layout_all["layouts"])) + "）。")
    return name, layout_all["layouts"][name]


def detect_day_cols(ws, ndays: int) -> tuple[int, int]:
    """2行目の日付から日付列の範囲を返す。日数が合わなければ止める。"""
    cols = [c.column for c in ws[2] if isinstance(c.value, (int, float))]
    if len(cols) != ndays:
        raise SystemExit(
            f"シートの日数が合いません（シート{len(cols)}日 / 対象月{ndays}日）。\n"
            f"  {ndays}日ぶんの列があるシートを指定してください。")
    return min(cols), max(cols)


def sheet_has_any_shift(ws, c0: int, ndays: int, rows=range(5, 47)) -> bool:
    """シートの勤務欄に勤務記号が1つでもあるか。数式や数値は勤務ではないので除く。
    「行レイアウトが違う」のか「本当に何も書かれていない」のかを見分けるのに使う。"""
    for r in rows:
        for d in range(ndays):
            v = ws.cell(row=r, column=c0 + d).value
            if v is not None and str(v).strip() in SHIFT_SYMBOLS:
                return True
    return False


def check_pool_rows(ws, layout: dict, c0: int, ndays: int,
                    require_filled: bool = True, rowmap: dict | None = None) -> list[str]:
    """職員行が本当に職員行かを確かめる。問題のある行の説明を返す。

    集計行（0/1の数値や数式が並ぶ）を職員行と取り違えると、
    勤務記号以外の値が並ぶのでここで捕まる。

    require_filled=False は**希望の記入用シート**を読むとき。
    希望が無い職員の行は全日空欄が正常なので、空欄そのものは問題にしない。
    ただし全職員が空欄ならレイアウト違いを疑うので、その場合だけ止める。"""
    problems = []
    n_filled = 0
    for sid, r in (rowmap if rowmap is not None else layout["pool_rows"]).items():
        vals = []
        for d in range(ndays):
            v = ws.cell(row=r, column=c0 + d).value
            if v is not None and str(v).strip() != "":
                vals.append(str(v).strip())
        bad = [v for v in vals if _looks_like_tally(v)]
        if vals:
            n_filled += 1
        if bad:
            problems.append(
                f"{sid}（{r}行目）: 集計行の値が {len(bad)}件 "
                f"（例 {', '.join(repr(v) for v in bad[:3])}）")
        elif not vals and require_filled:
            problems.append(f"{sid}（{r}行目）: 全日空欄")
    if not require_filled and n_filled == 0:
        # 職員行が全部空。シートのどこかに勤務記号があればレイアウト違い、
        # どこにも無ければ単に何も書かれていないだけ。区別して伝える。
        if sheet_has_any_shift(ws, c0, ndays):
            problems.append("職員行が全部空欄なのに、シートの別の行には勤務記号があります。"
                            "行レイアウトが違う可能性があります")
        else:
            problems.append("__EMPTY_SHEET__")
    return problems


def days_in(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]
