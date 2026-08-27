"""日勤のみ職員（プール外）の配置（設計メモ 5.6 / 実装順序6）

管理者・相談員・看護・パートなど、**Ａ（日勤）と休しかしない職員**を組む。

この人たちは早出・遅出・夜勤・リーダーを担わないため、
**変則プール10名と枠を奪い合わない。** したがってプール側の求解とは
切り離して別に解ける。19分かかるプール側に上乗せせず、数秒で終わる。

決めるのは「誰がどの日に出るか」だけ。条件は次のとおり。

    固定休の曜日      staff.csv の fixed_off_weekdays（土日・水など）
    祝日休            staff.csv の off_on_holidays（祝日は固定で休む職員）
    月の出勤日数      monthly_days（例 14日）／ weekly_days（例 週5）
    希望休            requests_YYYY-MM.csv
    管理者のリーダー日 プール側の求解結果。その日は必ず出勤

ソフト制約は**日ごとの人数をならす**こと。現場が意識している調整
（「特定の日に偏らないように」）をそのまま写している。
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from shift_solver import DAY, DAY_L, OFF, WEEKDAY_JP

# 日ごとの人数をならす重み。人数差1あたりの減点。
W_SPREAD = 10
# 希望休を通せなかったときの減点。人数の平準化より重くする。
W_WISH = 200


def daystaff_ids(inp) -> list[str]:
    """日勤のみで組む職員（プール外・Ａと休しかしない）を返す。"""
    out = []
    for sid, s in inp.staff.items():
        if s.in_pool:
            continue
        if s.allowed_shifts and s.allowed_shifts <= {DAY, DAY_L, OFF}:
            out.append(sid)
    return out


def target_workdays(s, ndays: int) -> int | None:
    """月の出勤日数の目標。指定が無ければ None（固定休以外は全部出勤）。"""
    if s.monthly_days is not None:
        return s.monthly_days
    if s.weekly_days is not None:
        return round(s.weekly_days * ndays / 7)
    return None


def solve_daystaff(inp, must_work: dict[str, set[int]] | None = None,
                   verbose: bool = True) -> dict:
    """{(職員ID, 日index): DAY or OFF} を返す。解けなければ空 dict。

    must_work は「その日は必ず出勤」の指定。管理者がリーダーを担う日に使う。"""
    ids = daystaff_ids(inp)
    if not ids:
        return {}
    must_work = must_work or {}
    ND = len(inp.days)
    holidays = set(inp.holidays)

    # 希望休（プール外ぶん）。fixed / wish の別を持っておく。
    wish_off, fixed_off = {}, {}
    for r in inp.requests:
        if r.staff_id not in ids or r.date not in inp.days:
            continue
        d = (r.date - inp.days[0]).days
        if OFF in r.allowed:
            (fixed_off if r.strength == "fixed" else wish_off)[(r.staff_id, d)] = True

    model = cp_model.CpModel()
    w = {(sid, d): model.NewBoolVar(f"w_{sid}_{d}") for sid in ids for d in range(ND)}

    unmet = []
    for sid in ids:
        s = inp.staff[sid]
        forced_off = []
        for d in range(ND):
            day = inp.days[d]
            off_by_rule = (
                WEEKDAY_JP[day.weekday()] in s.fixed_off_weekdays
                or (s.off_on_holidays and day in holidays)
                or (sid, d) in fixed_off
            )
            if off_by_rule and d not in must_work.get(sid, set()):
                model.Add(w[(sid, d)] == 0)
                forced_off.append(d)
            if d in must_work.get(sid, set()):
                model.Add(w[(sid, d)] == 1)   # 管理者のリーダー日は必ず出勤

        # 連続勤務の上限（staff.csv の max_consecutive。実績から決めた値）。
        # これが無いと、日ごとの人数をならすために長い連勤が組まれてしまう。
        run = s.max_consecutive
        if run is not None:
            for d in range(ND - run):
                model.Add(sum(w[(sid, dd)] for dd in range(d, d + run + 1)) <= run)

        tgt = target_workdays(s, ND)
        if tgt is None:
            # 出勤日数の指定が無い人は、固定休と希望休以外はすべて出勤。
            for d in range(ND):
                if d not in forced_off and (sid, d) not in wish_off:
                    model.Add(w[(sid, d)] == 1)
        else:
            model.Add(sum(w[(sid, d)] for d in range(ND)) == tgt)

        # 希望休は通す方向に（出勤日数の指定がある人は競合しうるのでソフト）
        for (s2, d) in list(wish_off):
            if s2 != sid:
                continue
            v = model.NewBoolVar(f"wishmiss_{sid}_{d}")
            model.Add(v >= w[(sid, d)])
            unmet.append(v)

    # 日ごとの人数をならす（最大と最小の差を詰める）
    counts = []
    for d in range(ND):
        c = model.NewIntVar(0, len(ids), f"cnt_{d}")
        model.Add(c == sum(w[(sid, d)] for sid in ids))
        counts.append(c)
    hi = model.NewIntVar(0, len(ids), "hi")
    lo = model.NewIntVar(0, len(ids), "lo")
    model.AddMaxEquality(hi, counts)
    model.AddMinEquality(lo, counts)

    model.Minimize(W_SPREAD * (hi - lo) + W_WISH * sum(unmet))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 60.0
    solver.parameters.num_search_workers = 1     # 再現性（設計1.1）
    solver.parameters.random_seed = 20260901
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if verbose:
            print(f"  日勤のみ職員の配置に失敗（{solver.StatusName(status)}）")
        return {}

    if verbose:
        n_unmet = sum(solver.Value(v) for v in unmet)
        per = [solver.Value(c) for c in counts]
        print(f"  日勤のみ職員 {len(ids)}名: 日中人数 {min(per)}〜{max(per)}人"
              + (f" ／ 通らなかった希望休 {n_unmet}件" if n_unmet else ""))

    return {(sid, d): (DAY if solver.Value(w[(sid, d)]) else OFF)
            for sid in ids for d in range(ND)}
