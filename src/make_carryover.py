"""前月の実績から翌月の繰越を作る（設計メモ 3.4 / 5.5）

当月のシフトCSVを読み、翌月の `carryover_YYYY-MM.yaml` を書き出す。
手で書いていた3項目をそのまま自動化したもの。

    night_out_day1          翌月1日を「明」に固定する職員（当月末日が夜勤入）
    prior_consecutive_days  当月末から続く連続勤務日数
    prev_off_delta          当月の（休日数 − 目標）。月またぎの公平性に使う

使い方:
    python src/make_carryover.py actual_2026-09.csv
    python src/make_carryover.py out/shift_2026-09_codes.csv --force
    python src/make_carryover.py actual_2026-09.csv --paid=S13=2

`--paid` は、記号「有」で書き分けていなかった月の有給日数を後から補うためのもの。
公休と有給が区別できていないと、有給で休んだぶんまで「前月に優遇された」と
数えてしまい、翌月の公平性（設計5.5）が不当に働く。
2026年10月以降は帳票に「有」と書くので、この指定は要らない。

**「実際に採用したシフト」を渡すこと。** ソルバーの出力をそのまま採用したなら
`out/shift_YYYY-MM_codes.csv`、手で作った月や手修正した月は実績CSVのほう。
渡したCSVが翌月の前提になるので、採用していない案を渡すと以降が全部ずれる。

対象月はファイル名の YYYY-MM から判定する（validate_shift.py と同じ規約）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from shift_solver import NIGHT_IN, NONWORK, OFF, PAID, load_inputs
from validate_shift import _ym_from_name, read_edited_csv


def next_ym(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def parse_paid(argv) -> dict[str, int]:
    """--paid=S13=2（複数可、カンマ区切りも可）を {職員ID: 有給日数} に。"""
    out: dict[str, int] = {}
    for a in argv:
        if not a.startswith("--paid="):
            continue
        for part in a.split("=", 1)[1].split(","):
            if "=" in part:
                sid, n = part.split("=", 1)
                out[sid.strip()] = int(n)
    return out


def build(assign: dict, inp, paid_override: dict[str, int] | None = None) -> dict:
    """当月の割当から繰越3項目を組み立てる。プール職員のみを対象とする。"""
    paid_override = paid_override or {}
    ND = len(inp.days)
    last = ND - 1
    pool_ids = [s.staff_id for s in inp.staff.values() if s.in_pool]

    night_out_day1, prior, delta = [], {}, {}
    for sid in pool_ids:
        if assign.get((sid, last)) == NIGHT_IN:
            night_out_day1.append(sid)

        # 末日から遡って休み（公休・有給）に当たるまでの日数。
        # 入・明もそれぞれ1日と数える（設計4.1）。
        run = 0
        for d in range(last, -1, -1):
            code = assign.get((sid, d))
            if code is None or code in NONWORK:
                break
            run += 1
        if run:
            prior[sid] = run

        # 公休のみを数える。有給は別枠なので公平性の判定には入れない（設計3.5/5.5）。
        # 記号「有」がある月は PAID として読めるが、書き分けていなかった月は
        # --paid で日数を補う。
        offs = sum(1 for d in range(ND) if assign.get((sid, d)) == OFF)
        offs -= paid_override.get(sid, 0)
        diff = offs - inp.off_target[sid]
        if diff:
            delta[sid] = diff

    return {"night_out_day1": night_out_day1,
            "prior_consecutive_days": prior,
            "prev_off_delta": delta}


def render(carry: dict, src: Path, year: int, month: int, ny: int, nm: int) -> str:
    """YAMLを組み立てる。手で書いていたものと同じ体裁・同じ注釈を残す。"""
    L = [f"# 前月繰越（設計メモ 3.4 / 5.5）— {ny}年{nm}月",
         f"# {year}年{month}月の実績 {src.as_posix()} から自動生成（src/make_carryover.py）。",
         "# ※職員IDは匿名。実名との対応表は手元の Excel で持つ（設計メモ 0）。",
         "",
         f"# 当月1日を「明」に固定する職員（前月末日 {month}/{_last_day(year, month)} が夜勤入だった人）",
         "# 明→翌日休（ハード制約3）も自動で効く。"]
    if carry["night_out_day1"]:
        L += ["night_out_day1:"] + [f"  - {s}" for s in carry["night_out_day1"]]
    else:
        L += ["night_out_day1: []"]

    L += ["",
          "# 前月末日から続く連続勤務日数（当月頭の連続勤務判定に加算）",
          "# 前月末から遡って「休」に当たるまでの日数。記載のない職員は 0。"]
    if carry["prior_consecutive_days"]:
        L += ["prior_consecutive_days:"] + [
            f"  {s}: {v}" for s, v in sorted(carry["prior_consecutive_days"].items())]
    else:
        L += ["prior_consecutive_days: {}"]

    L += ["",
          f"# 前月（{month}月）の休日数と目標の差（実績 − 目標）。月またぎの公平性に使う（設計5.5）。",
          "# +1 = 前月に1日多く休んだ／-1 = 前月は目標に届かなかった。0の職員は記載を省略。"]
    if carry["prev_off_delta"]:
        L += ["prev_off_delta:"] + [
            f"  {s}: {v}" for s, v in sorted(carry["prev_off_delta"].items())]
    else:
        L += ["# 全員が目標どおりだったため、差のある職員はいない。", "prev_off_delta: {}"]
    return "\n".join(L) + "\n"


def _last_day(year: int, month: int) -> int:
    import calendar
    return calendar.monthrange(year, month)[1]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(2)

    src = Path(args[0])
    if not src.exists():
        print(f"ファイルが見つかりません: {src}")
        sys.exit(2)
    ym = _ym_from_name(src)
    if ym is None:
        print(f"ファイル名から対象月を判定できません（YYYY-MM を含めること）: {src.name}")
        sys.exit(2)
    year, month = ym

    data_dir = Path(__file__).resolve().parent.parent / "data"
    inp = load_inputs(data_dir, ym)
    assign, _, _, blanks, errors, approx = read_edited_csv(src, inp)

    print("=" * 70)
    print(f"前月繰越の作成: {src}  （{year}年{month}月の実績）")
    print("=" * 70)
    if errors:
        print("\n■ 読み込みエラー")
        for e in errors:
            print(f"  - {e}")
        print("\n読み込めなかったため中止します。")
        sys.exit(1)
    if blanks:
        # 空欄は末尾の連勤・休日数の集計を狂わせるので、黙って進めない
        print(f"\n■ 未割当セルが {len(blanks)}件 あります: "
              + "、".join(blanks[:10]) + (" …" if len(blanks) > 10 else ""))
        if not force:
            print("  空欄は連勤と休日数の集計を狂わせます。埋めてから実行するか、"
                  "承知のうえなら --force を付けてください。")
            sys.exit(1)
        print("  --force が指定されたため、空欄を「勤務なし」として続行します。")

    paid_override = parse_paid(sys.argv[1:])
    ND = len(inp.days)
    paid_in_csv = {sid: sum(1 for d in range(ND) if assign.get((sid, d)) == PAID)
                   for sid in inp.staff if inp.staff[sid].in_pool}
    paid_in_csv = {k: v for k, v in paid_in_csv.items() if v}
    if paid_in_csv:
        print()
        print(f"■ CSV上の有給（記号「有」）: {paid_in_csv}")
    # CSV に「有」がある職員は、そちらが正なので --paid を使わない
    for sid in list(paid_override):
        if sid in paid_in_csv:
            print(f"  {sid} は CSV に「有」があるため --paid を無視します。")
            del paid_override[sid]
    if paid_override:
        print()
        print(f"■ --paid で補った有給日数: {paid_override}")
        print("  （記号「有」で書き分けていなかった月の補正）")

    carry = build(assign, inp, paid_override)
    ny, nm = next_ym(year, month)
    # 出力先はプロジェクト直下。requests_YYYY-MM.csv と同じ場所に揃える。
    out = data_dir.parent / f"carryover_{ny}-{nm:02d}.yaml"

    print(f"\n■ {ny}年{nm}月の繰越")
    print(f"  {nm}/1 を「明」に固定  : {carry['night_out_day1'] or 'なし'}")
    print(f"  {month}月末からの連勤  : {carry['prior_consecutive_days'] or 'なし（全員0）'}")
    print(f"  {month}月の休日数の差  : {carry['prev_off_delta'] or 'なし（全員が目標どおり）'}")
    if approx:
        print(f"\n※ 帳票の「Ａ」を can_lead から推定した箇所が {approx} 件あります。"
              "\n  繰越の3項目はリーダーの区別に依存しないため、結果には影響しません。")

    if out.exists() and not force:
        print(f"\n既にあります: {out}"
              "\n上書きするなら --force を付けてください。")
        sys.exit(1)
    out.write_text(render(carry, src, year, month, ny, nm), encoding="utf-8")
    print(f"\n出力: {out}")


if __name__ == "__main__":
    main()
