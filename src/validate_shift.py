"""
手修正CSVの再検証（設計メモ 6.3 / 実装順序5）

出力した shift_YYYY-MM.csv を人が Excel 等で直したあと、読み込んで
ハード制約（設計4章の12項目）違反を検出する。あわせて設計6.2の観点から
warning（違反ではないが直した人に知らせたいこと）も出す。

使い方:
    python src/validate_shift.py [検証するCSVのパス]
    省略時は out/shift_{year}-{month}_codes.csv（無ければ shift_{...}.csv）。

CSVは2種を受け付ける。
- コード表記（DAY_L/DAY を区別）… リーダー枠まで正確に判定できる。検証はこちら推奨。
- 帳票記号（DAY_L/DAY がどちらも「Ａ」）… can_lead で推定するため、リーダーの
  区別は不正確になりうる（その旨を注記する）。

判定基準は生成時と同じ入力（staff.csv / month.yaml / requests_*.csv / carryover.yaml）。
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

from shift_solver import (
    ALL_SHIFTS, DAY, DAY_L, DAY_LATE, EARLY, LATE, NIGHT_IN, NIGHT_OUT,
    LATE_END, NONWORK, OFF, PAID, SYMBOL, WEEKDAY_JP, load_inputs,
)

_CELL_TO_CODE = {
    "Ｂ": EARLY, "Ｃ": LATE, "ＡC": DAY_LATE,
    "入": NIGHT_IN, "明": NIGHT_OUT, "休": OFF, "有": PAID, "a": DAY,
}


def cell_to_code(sym: str, can_lead: bool) -> tuple[str | None, bool]:
    """(内部コード, リーダー推定フラグ) を返す。空白は (None, False)。
    リーダー推定フラグ=True は「帳票のＡを can_lead から DAY_L と推定した」印。"""
    t = (sym or "").strip()
    if t == "":
        return None, False
    if t in ALL_SHIFTS:          # コード表記 → 正確
        return t, False
    if t == "Ａ":                # 帳票のＡ → 推定（can_lead=true なら曖昧）
        return (DAY_L, True) if can_lead else (DAY, False)
    if t in _CELL_TO_CODE:
        return _CELL_TO_CODE[t], False
    raise ValueError(f"未知の記号: {t!r}")


def _is_part(staff) -> bool:
    """パート（プール外の現場職員。管理者は除く）。"""
    return (not staff.in_pool) and staff.role in (
        "care", "driver", "nurse", "rehab", "nutrition", "counselor")


def read_edited_csv(path: Path, inp):
    """CSVを読み、pool割当 / part割当 / 未割当 / エラー / リーダー推定件数 を返す。"""
    assign: dict[tuple[str, int], str] = {}
    part_assign: dict[tuple[str, int], str] = {}
    blanks: list[str] = []
    errors: list[str] = []
    approx_leader = 0
    pool_ids = [s.staff_id for s in inp.staff.values() if s.in_pool]
    part_ids = [s.staff_id for s in inp.staff.values() if _is_part(s)]
    ND = len(inp.days)

    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    found = {r[0].strip() for r in rows if r}

    for row in rows:
        if not row:
            continue
        sid = row[0].strip()
        st = inp.staff.get(sid)
        if st is None or not (st.in_pool or _is_part(st)):
            continue  # ヘッダ・曜日・管理者などはスキップ
        cells = row[1:]
        if len(cells) < ND:
            errors.append(f"{sid}: 列数が {len(cells)} 日分（{ND}日必要）")
        for d in range(ND):
            sym = cells[d] if d < len(cells) else ""
            try:
                code, approx = cell_to_code(sym, st.can_lead)
            except ValueError as e:
                errors.append(f"{sid} {inp.days[d].day}日: {e}")
                code, approx = None, False
            if approx:
                approx_leader += 1
            if code is None:
                if st.in_pool:
                    blanks.append(f"{sid} {inp.days[d].day}日")
            elif st.in_pool:
                assign[(sid, d)] = code
            else:
                part_assign[(sid, d)] = code

    for sid in pool_ids:
        if sid not in found:
            errors.append(f"職員 {sid} の行が見つからない")
    return assign, part_assign, part_ids, blanks, errors, approx_leader


def validate(inp, assign: dict) -> list[str]:
    """ハード制約（設計4章）違反を返す。空なら違反なし。"""
    V: list[str] = []
    ND = len(inp.days)
    last = ND - 1
    pool = [s for s in inp.staff.values() if s.in_pool]

    def dlabel(d):
        return f"{inp.days[d].day}日({WEEKDAY_JP[inp.days[d].weekday()]})"

    fixed_req: dict[tuple[str, int], set] = {}
    for r in inp.requests:
        if r.strength == "fixed" and r.date in inp.days:
            d = (r.date - inp.days[0]).days
            fixed_req.setdefault((r.staff_id, d), set()).update(r.allowed)

    for s in pool:
        sid = s.staff_id
        codes = {d: assign.get((sid, d)) for d in range(ND)}

        for d in range(ND):
            c = codes[d]
            if c is None:
                continue
            # C5: 有給は職務能力ではなく権利なので allowed_shifts の対象外（設計3.5）
            if c != PAID and c not in s.allowed_shifts:            # C5
                V.append(f"[C5] {sid} {dlabel(d)}: 取れない勤務 {c}")
            # C14: 有給は本人の申請どおりの日にのみ入る
            if c == PAID and PAID not in fixed_req.get((sid, d), ()):
                V.append(f"[C14] {sid} {dlabel(d)}: 申請にない有給")
            if not s.can_lead and c in (DAY_L, DAY_LATE):          # C6
                V.append(f"[C6] {sid} {dlabel(d)}: can_lead=false に {c}")
            if c == NIGHT_IN and d < last and codes[d + 1] != NIGHT_OUT:   # C2
                V.append(f"[C2] {sid} {dlabel(d)}: 入の翌日が明でない（翌日={codes[d+1]}）")
            if c == NIGHT_OUT:                                     # C3
                # C3: 明の翌日は出勤しない（公休でも有給でもよい）
                if d < last and codes[d + 1] not in NONWORK:
                    V.append(f"[C3] {sid} {dlabel(d)}: 明の翌日が休でない（翌日={codes[d+1]}）")
                if d == 0:
                    if sid not in inp.night_out_day1:
                        V.append(f"[C3] {sid} 1日: 前月繰越にない明が初日にある")
                elif codes[d - 1] != NIGHT_IN:
                    V.append(f"[C3] {sid} {dlabel(d)}: 明の前日が入でない（前日={codes[d-1]}）")
            if c in LATE_END and d < last and codes[d + 1] == EARLY:       # C15
                V.append(f"[C15] {sid} {dlabel(d)}: 遅出({SYMBOL[c]})の翌日に早出")
            if (sid, d) in fixed_req and c not in fixed_req[(sid, d)]:     # C7 (fixed)
                want = "/".join(sorted(fixed_req[(sid, d)]))
                V.append(f"[C7] {sid} {dlabel(d)}: 固定希望[{want}]に反して {c}")
            if (WEEKDAY_JP[inp.days[d].weekday()] in s.fixed_off_weekdays
                    and c not in NONWORK):                         # C12
                V.append(f"[C12] {sid} {dlabel(d)}: 固定休の曜日に出勤 {c}")

        if sid in inp.night_out_day1 and codes[0] != NIGHT_OUT:    # 繰越の明
            V.append(f"[C3] {sid} 1日: 前月繰越で明のはずが {codes[0]}")

        # C8/C9 は公休のみを数える。有給は別枠で上乗せ（設計3.5）。
        offs = sum(1 for d in range(ND) if codes[d] == OFF)
        if offs < inp.off_min[sid]:                                # C8
            V.append(f"[C8] {sid}: 休{offs}日 < 下限{inp.off_min[sid]}")
        if offs > inp.off_max[sid]:                                # C9
            V.append(f"[C9] {sid}: 休{offs}日 > 上限{inp.off_max[sid]}")

        nights = sum(1 for d in range(ND) if codes[d] == NIGHT_IN)  # C11
        if s.night_max is not None and nights > s.night_max:
            V.append(f"[C11] {sid}: 夜勤{nights}回 > 上限{s.night_max}")
        if s.night_min is not None and nights < s.night_min:
            V.append(f"[C11] {sid}: 夜勤{nights}回 < 下限{s.night_min}")

        p = inp.prior_consecutive_days.get(sid, 0)                  # C10
        run, worst, worst_end = p, 0, None
        for d in range(ND):
            if codes[d] is not None and codes[d] not in NONWORK:
                run += 1
                if run > worst:
                    worst, worst_end = run, d
            else:
                run = 0
        if worst > 5:
            V.append(f"[C10] {sid}: 連続勤務{worst}日（〜{dlabel(worst_end)}）> 5")

    return V


def wish_warnings(inp, assign: dict) -> list[str]:
    """通らなくなった wish 希望の警告（違反ではない・設計6.2）。"""
    out = []
    # 検証対象のCSVはプール10名ぶん。希望ファイルにはプール外（管理者・相談員・
    # 看護・パート）の希望も入るので、そちらは照合しない（設計5.6）。
    pool_ids = {s.staff_id for s in inp.staff.values() if s.in_pool}
    for r in inp.requests:
        if r.staff_id not in pool_ids:
            continue
        if r.strength == "fixed" or r.date not in inp.days:
            continue
        d = (r.date - inp.days[0]).days
        got = assign.get((r.staff_id, d))
        if got not in r.allowed:
            syms = "/".join(SYMBOL.get(a, a) for a in sorted(r.allowed))
            gotsym = SYMBOL.get(got, "(未割当)") if got else "(未割当)"
            out.append(f"{r.staff_id} の {r.date.month}/{r.date.day} "
                       f"希望[{syms}]が通っていません（実際: {gotsym}）")
    return out


def part_warnings(inp, part_assign, part_ids) -> tuple[list[str] | None, str]:
    """パート人数が0〜1人の日への警告（日曜除外・設計6.2）。
    パートのデータが無い/CSVに含まれない場合は None を返しスキップ。"""
    if not part_ids:
        return None, "パート（プール外）が staff.csv に定義されていないためスキップ。"
    if not part_assign:
        return None, "パート行が CSV に含まれていないためスキップ。"
    ND = len(inp.days)
    warns = []
    for d in range(ND):
        if inp.days[d].weekday() == 6:  # 日曜は除外
            continue
        cnt = sum(1 for sid in part_ids if part_assign.get((sid, d)) not in (None, OFF))
        if cnt <= 1:
            warns.append(f"  {inp.days[d].day:>2}日({WEEKDAY_JP[inp.days[d].weekday()]}): パート{cnt}人")
    return warns, ""


def coverage_info(inp, assign: dict) -> list[str]:
    """参考: 日別の必要枠の未充足（未配置）。ハードではない。"""
    info = []
    ND = len(inp.days)
    pool_ids = [s.staff_id for s in inp.staff.values() if s.in_pool]
    for d in range(ND):
        e = sum(1 for s in pool_ids if assign.get((s, d)) == EARLY)
        la = sum(1 for s in pool_ids if assign.get((s, d)) in (LATE, DAY_LATE))
        ni = sum(1 for s in pool_ids if assign.get((s, d)) == NIGHT_IN)
        le = sum(1 for s in pool_ids if assign.get((s, d)) in (DAY_L, DAY_LATE))
        short = []
        if e < inp.required["early"]:
            short.append(f"早出{inp.required['early']-e}")
        if la < inp.required["late"]:
            short.append(f"遅出{inp.required['late']-la}")
        if ni < inp.required["night_in"]:
            short.append(f"夜勤入{inp.required['night_in']-ni}")
        if le < inp.required["leader"]:
            short.append("プールにリーダーなし(管理者/パートで補完する日)")
        if short:
            info.append(f"  {inp.days[d].day:>2}日: " + " ".join(short))
    return info


def _default_target(inp) -> Path:
    out = Path(__file__).resolve().parent.parent / "out"
    tag = f"{inp.year}-{inp.month:02d}"
    codes = out / f"shift_{tag}_codes.csv"
    return codes if codes.exists() else out / f"shift_{tag}.csv"


def _ym_from_name(path: Path) -> tuple[int, int] | None:
    """shift_YYYY-MM(_codes).csv からファイル名の対象月を推定。無ければ None。"""
    m = re.search(r"(\d{4})-(\d{2})", path.name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def main():
    data_dir = Path(__file__).resolve().parent.parent / "data"
    # 引数のCSVを先に確定し、その月で入力を読む。省略時は month.yaml の月。
    # 検証対象が7月CSVでも8月設定で照合してしまう不一致を防ぐ（要 requests/carryover の月一致）。
    arg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    ym = _ym_from_name(arg_path) if arg_path else None
    inp = load_inputs(data_dir, ym)
    csv_path = arg_path if arg_path else _default_target(inp)

    print("=" * 70)
    print(f"手修正CSVの再検証: {csv_path}")
    print("=" * 70)
    if not csv_path.exists():
        print("ファイルが見つかりません。")
        sys.exit(2)

    assign, part_assign, part_ids, blanks, errors, approx_leader = read_edited_csv(csv_path, inp)

    if errors:
        print("\n■ 読み込みエラー")
        for e in errors:
            print(f"  - {e}")

    violations = validate(inp, assign)
    print("\n■ ハード制約違反（設計4章）")
    if violations:
        for v in violations:
            print(f"  ✗ {v}")
    else:
        print("  なし（すべてのハード制約を満たしています）")

    wishes = wish_warnings(inp, assign)
    print("\n■ 警告: 通らなくなった希望（wish・違反ではない）")
    if wishes:
        for w in wishes:
            print(f"  ⚠ {w}")
    else:
        print("  なし（wish 希望はすべて維持）")

    part_warns, part_note = part_warnings(inp, part_assign, part_ids)
    print("\n■ 警告: パート人数が0〜1人の日（日曜除外・設計6.2）")
    if part_warns is None:
        print("  " + part_note)
    elif part_warns:
        print("\n".join(f"  ⚠{w}" for w in part_warns))
    else:
        print("  なし（全日でパート2人以上）")

    if blanks:
        print(f"\n■ 未割当セル（要確認）: {len(blanks)}件")
        print("  " + "、".join(blanks[:20]) + (" …" if len(blanks) > 20 else ""))

    cov = coverage_info(inp, assign)
    print("\n■ 参考: 必要枠の未充足（未配置・ハードではない）")
    print("\n".join(cov) if cov else "  なし")

    if approx_leader:
        print(f"\n※ リーダー区別は帳票の「Ａ」から can_lead で推定した箇所が {approx_leader} 件あります。"
              f"\n  正確に検証するにはコード表記CSV（shift_YYYY-MM_codes.csv）を渡してください。")

    n_hard = len(violations) + len(errors)
    print("\n" + "=" * 70)
    print(f"判定: {'NG（ハード制約違反あり）' if n_hard else 'OK（ハード制約違反なし）'}"
          f"  違反{len(violations)} / 読込エラー{len(errors)} / "
          f"希望警告{len(wishes)} / 未割当{len(blanks)}")
    sys.exit(1 if n_hard else 0)


if __name__ == "__main__":
    main()
