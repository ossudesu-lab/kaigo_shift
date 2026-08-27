"""
シフト自動作成システム — 読み込み + ハード制約 + ソフト制約 + 出力

設計メモ shift_solver_design.md の実装順序 1〜4 に対応する。
  1. 職員マスタ・月次パラメータ・希望・前月繰越の読み込み
  2. CP-SAT モデル（ハード制約）
  3. ソフト制約と目的関数（5章）。日遅は職員別 daylate_cost（5.3）
  4. CSV 整形出力とサマリ（6.1 / 6.2）

手修正CSVの再検証（実装順序5・6.3）は src/validate_shift.py に分離。
Web（6）・LLM説明（7）は未実装。
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import datetime as dt
import io
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from ortools.sat.python import cp_model

# --- 勤務区分（内部コード） ---------------------------------------------------
DAY_L = "DAY_L"          # Ａ 日勤（当日リーダー）
DAY = "DAY"              # Ａ 日勤（リーダーなし）
EARLY = "EARLY"          # Ｂ 早出
LATE = "LATE"            # Ｃ 遅出
DAY_LATE = "DAY_LATE"    # ＡC 日遅（日勤リーダー＋遅出兼務）
NIGHT_IN = "NIGHT_IN"    # 入 夜勤入
NIGHT_OUT = "NIGHT_OUT"  # 明 夜勤明
OFF = "OFF"              # 休 公休
PAID = "PAID"            # 有 年次有給休暇（公休とは別枠。設計3.5）

ALL_SHIFTS = [DAY_L, DAY, EARLY, LATE, DAY_LATE, NIGHT_IN, NIGHT_OUT, OFF, PAID]

# 出勤しない勤務区分。連続勤務を切り、明の翌日の要件も満たす。
NONWORK = (OFF, PAID)

# 終わりが遅い勤務。翌日の早出は勤務間が短くなりすぎるので禁じる（設計4.2）。
# ＡC は日勤リーダーと遅出の兼務なので、終業は Ｃ と同じ。
LATE_END = (LATE, DAY_LATE)

# 表示記号。CSV は実帳票に合わせ DAY_L / DAY をどちらも「Ａ」で出す。
# コンソールのグリッドだけ DAY を「a」で区別する（GRID_SYMBOL）。
SYMBOL = {
    DAY_L: "Ａ", DAY: "Ａ", EARLY: "Ｂ", LATE: "Ｃ",
    DAY_LATE: "ＡC", NIGHT_IN: "入", NIGHT_OUT: "明", OFF: "休", PAID: "有",
}
GRID_SYMBOL = dict(SYMBOL, DAY="a")
SYMBOL_TO_CODE = {
    "Ａ": DAY_L, "Ｂ": EARLY, "Ｃ": LATE, "ＡC": DAY_LATE,
    "入": NIGHT_IN, "明": NIGHT_OUT, "休": OFF, "有": PAID, "a": DAY,
}
SYMBOL_TO_CODE.update({k: k for k in ALL_SHIFTS})

WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

# --- ソフト制約の重み（設計メモ 5 章） ---------------------------------------
W = {
    "unfilled_night_in": 1_000_000,
    "unfilled_early":       100_000,
    "unfilled_late":         10_000,
    "leader_absent":          5_000,
    "wish_unmet":             1_000,
    "manager_leader":           100,
    "off_below_target":          30,
    "off_above_target":           5,   # 目標より多く休むこと自体（小さく。使う必要はある）
    "day_to_early":              50,   # Ａの翌日にＢ（設計4.2。避けたいが不可ではない）
    "off_fairness":             200,   # 前月に有利だった職員が今月も有利になることへの追加減点
    "night_spread":              20,
    "consecutive5":             300,
    "day_nonleader":              1,
    # 日遅（day_late）は職員別 daylate_cost。ここには固定重みを置かない。
}
DAYLATE_COST_DEFAULT = 5


# --- データ構造 ---------------------------------------------------------------
@dataclass
class Staff:
    staff_id: str
    role: str
    in_pool: bool
    can_lead: bool
    allowed_shifts: set[str]
    night_max: int | None
    night_min: int | None
    fixed_off_weekdays: set[str]
    off_on_holidays: bool
    max_consecutive: int | None
    weekly_days: int | None
    monthly_days: int | None
    off_target: int | None
    off_max: int | None
    daylate_threshold: int | None   # T_i: この回数までは unit、超えると over_unit（5.3）
    daylate_unit: int | None        # c1_i: 閾値までの日遅1回あたり単価
    daylate_over_unit: int | None   # c2: 閾値超過分の日遅1回あたり単価
    late_to_day_unit: int | None    # 遅出の翌日に日勤（設計4.2）。職員別の単価


@dataclass
class Request:
    staff_id: str
    date: dt.date
    allowed: set[str]
    strength: str


@dataclass
class Inputs:
    staff: dict[str, Staff]
    year: int
    month: int
    days: list[dt.date]
    required: dict[str, int]
    off_target: dict[str, int]
    off_min: dict[str, int]
    off_max: dict[str, int]
    requests: list[Request]
    requests_path: Path | None
    month_path: Path | None
    weights: dict[str, int]
    night_out_day1: list[str]
    prior_consecutive_days: dict[str, int]
    prev_off_delta: dict[str, int]      # 前月の（休日数 − 目標）。+1=多く休んだ／-1=足りなかった
    manager_id: str | None
    holidays: list[dt.date] = field(default_factory=list)
    manager_leader_max: int | None = None   # 管理者がリーダーを担える上限日数（None=上限なし）


# --- 読み込み -----------------------------------------------------------------
def _to_bool(s: str) -> bool:
    return str(s).strip().lower() in ("true", "1", "yes", "○", "o")


def _to_int_or_none(s) -> int | None:
    s = ("" if s is None else str(s)).strip()
    return int(s) if s else None


def _parse_shift_list(s: str) -> set[str]:
    s = (s or "").strip()
    if not s:
        return set()
    codes = set()
    for tok in s.replace("/", "|").split("|"):
        tok = tok.strip()
        if not tok:
            continue
        if tok not in SYMBOL_TO_CODE:
            raise ValueError(f"未知の勤務区分: {tok!r}")
        codes.add(SYMBOL_TO_CODE[tok])
    return codes


def load_staff(path: Path) -> dict[str, Staff]:
    staff: dict[str, Staff] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sid = row["staff_id"].strip()
            staff[sid] = Staff(
                staff_id=sid,
                role=row["role"].strip(),
                in_pool=_to_bool(row["in_pool"]),
                can_lead=_to_bool(row["can_lead"]),
                allowed_shifts=_parse_shift_list(row["allowed_shifts"]),
                night_max=_to_int_or_none(row.get("night_max")),
                night_min=_to_int_or_none(row.get("night_min")),
                fixed_off_weekdays=set(
                    x.strip() for x in (row.get("fixed_off_weekdays") or "").split("|") if x.strip()
                ),
                off_on_holidays=_to_bool(row.get("off_on_holidays") or "false"),
                max_consecutive=_to_int_or_none(row.get("max_consecutive")),
                weekly_days=_to_int_or_none(row.get("weekly_days")),
                monthly_days=_to_int_or_none(row.get("monthly_days")),
                off_target=_to_int_or_none(row.get("off_target")),
                off_max=_to_int_or_none(row.get("off_max")),
                daylate_threshold=_to_int_or_none(row.get("daylate_threshold")),
                daylate_unit=_to_int_or_none(row.get("daylate_unit")),
                daylate_over_unit=_to_int_or_none(row.get("daylate_over_unit")),
                late_to_day_unit=_to_int_or_none(row.get("late_to_day_unit")),
            )
    return staff


def load_month(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_requests(path: Path) -> list[Request]:
    """希望CSV。列は staff_id,date,allowed,strength（symbol/note 等の追加列は無視）。"""
    reqs: list[Request] = []
    if path is None or not path.exists():
        return reqs
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not (row.get("staff_id") or "").strip():
                continue
            reqs.append(Request(
                staff_id=row["staff_id"].strip(),
                date=dt.date.fromisoformat(row["date"].strip()),
                allowed=_parse_shift_list(row["allowed"]),
                strength=(row.get("strength") or "wish").strip().lower(),
            ))
    return reqs


def load_carryover(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_requests_path(base_dir: Path, data_dir: Path, year: int, month: int) -> Path | None:
    """月別ファイル requests_YYYY-MM.csv を優先。プロジェクト直下 → data/ → 汎用 requests.csv。"""
    name = f"requests_{year}-{month:02d}.csv"
    for cand in (base_dir / name, data_dir / name, data_dir / "requests.csv"):
        if cand.exists():
            return cand
    return None


def _resolve_month_path(base_dir: Path, data_dir: Path, year: int, month: int) -> Path | None:
    """月別ファイル month_YYYY-MM.yaml を探す。requests / carryover と同じ規約で
    プロジェクト直下 → data/ の順。無ければ None（共通の month.yaml だけを使う）。
    祝日や管理者リーダーの上限のように月ごとに変わる項目をここに置く（設計3.2）。"""
    name = f"month_{year}-{month:02d}.yaml"
    for cand in (base_dir / name, data_dir / name):
        if cand.exists():
            return cand
    return None


def _merge_month(base: dict, over: dict) -> dict:
    """月別ファイルを共通 month.yaml に重ねる。
    辞書の値は1階層だけマージする（weights の1項目だけ差し替える等ができる）。
    スカラーとリストは丸ごと置き換える（holidays は月ごとに総入れ替えになる）。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


def _resolve_carryover_path(base_dir: Path, data_dir: Path, year: int, month: int) -> Path | None:
    """月別ファイル carryover_YYYY-MM.yaml を優先。プロジェクト直下 → data/ → 汎用 data/carryover.yaml。
    requests と同じ規約。前月繰越は月ごとに異なるため月別ファイルで持つ。"""
    name = f"carryover_{year}-{month:02d}.yaml"
    for cand in (base_dir / name, data_dir / name, data_dir / "carryover.yaml"):
        if cand.exists():
            return cand
    return None


def load_inputs(data_dir: Path, ym: tuple[int, int] | None = None,
                night_max_override: dict[str, int] | None = None) -> Inputs:
    """ym を渡すと month.yaml の year/month を上書きする（重み等はそのまま）。
    月をまたいだ比較実行で、weights を固定したまま対象月だけ差し替えるために使う。

    night_max_override は {staff_id: 夜勤上限} を実行時に一時上書きする（staff.csv は不変）。
    実績の夜勤回数に条件を揃えて比較する等、その場限りの検証に使う。"""
    base_dir = data_dir.parent
    staff = load_staff(data_dir / "staff.csv")
    month = load_month(data_dir / "month.yaml")

    for sid, nm in (night_max_override or {}).items():
        if sid in staff:
            staff[sid].night_max = nm

    year, mon = (ym if ym is not None else (int(month["year"]), int(month["month"])))

    # 月別 month_YYYY-MM.yaml があれば共通 month.yaml に重ねる（設計3.2）。
    # 対象月が確定してからでないと探せないので、year/mon を決めた直後に行う。
    month_path = _resolve_month_path(base_dir, data_dir, year, mon)
    if month_path is not None:
        month = _merge_month(month, load_month(month_path))

    carry = load_carryover(_resolve_carryover_path(base_dir, data_dir, year, mon) or Path("/nonexistent"))
    ndays = calendar.monthrange(year, mon)[1]
    days = [dt.date(year, mon, d) for d in range(1, ndays + 1)]

    req_path = _resolve_requests_path(base_dir, data_dir, year, mon)
    requests = load_requests(req_path)

    required = {k: int(month["required"][k]) for k in ("early", "late", "night_in", "leader")}

    otd = int(month.get("off_target_default", 10))
    omind = int(month.get("off_min_default", 9))
    omaxd = int(month.get("off_max_default", 10))
    off_target = {s: otd for s in staff}
    off_target.update({k: int(v) for k, v in (month.get("off_target") or {}).items()})
    off_min = {s: omind for s in staff}
    off_min.update({k: int(v) for k, v in (month.get("off_min") or {}).items()})
    off_max = {s: omaxd for s in staff}
    off_max.update({k: int(v) for k, v in (month.get("off_max") or {}).items()})
    # off_max を明示指定した職員を控えておく（下の「1日の余裕」を適用しない）。
    explicit_off_max = set(month.get("off_max") or {})

    # staff.csv の off_target / off_max 列があればそちらを優先（設計3.1/3.2）。
    for sid, st in staff.items():
        if st.off_target is not None:
            off_target[sid] = st.off_target
        if st.off_max is not None:
            off_max[sid] = st.off_max
            explicit_off_max.add(sid)

    # monthly_days（月あたり勤務日数）があれば、月の日数から休日数を導出して最優先で使う。
    # 固定されているのは休日数ではなく出勤日数のほう（設計3.2/7.6）。
    # 出勤21日なら 31日月→休10日、30日月→休9日。off_target/off_max 列より優先する。
    for sid, st in staff.items():
        if st.monthly_days is not None:
            off_target[sid] = ndays - st.monthly_days
            off_min[sid] = min(off_min[sid], off_target[sid])
            explicit_off_max.discard(sid)

    # 休日数の上限には1日の余裕を持たせる（設計4.0）。全員をぴったり off_target に
    # 収められない月があり、夜勤回数の多い職員が1日多く休むのは実績でも起きている。
    # off_below_target（既定200）の減点は残るので、ソルバーは必要なとき以外は使わない。
    slack = int(month.get("off_slack_default", 1))
    for sid in staff:
        if sid not in explicit_off_max:
            off_max[sid] = off_target[sid] + slack

    holidays = [dt.date.fromisoformat(str(h)) for h in (month.get("holidays") or [])]
    managers = [sid for sid, st in staff.items() if st.role == "manager" and st.can_lead]

    # 管理者がリーダーを担える上限日数。月ごとの事情（他事業所への出張・評価業務等）を
    # 入力として渡すための項目。未指定なら上限なし＝重み100の減点のみ（設計7.6/9章）。
    mlm = month.get("manager_leader_max")
    mlm = int(mlm) if mlm is not None else None

    # 既定重みはコード(W)。month.yaml の weights: で上書き可（設計5.4）。
    weights = dict(W)
    weights.update({k: int(v) for k, v in (month.get("weights") or {}).items()})

    return Inputs(
        staff=staff, year=year, month=mon, days=days, required=required,
        off_target=off_target, off_min=off_min, off_max=off_max,
        requests=requests, requests_path=req_path, month_path=month_path, weights=weights,
        night_out_day1=list(carry.get("night_out_day1") or []),
        prior_consecutive_days={k: int(v) for k, v in (carry.get("prior_consecutive_days") or {}).items()},
        prev_off_delta={k: int(v) for k, v in (carry.get("prev_off_delta") or {}).items()},
        manager_id=managers[0] if managers else None,
        holidays=holidays,
        manager_leader_max=mlm,
    )


# 求解パラメータ。
# 1段目: 並列で最適値を出す。速いが同点解のどれを返すかは非決定的。
# 2段目: その最適値を固定し、目的関数を外した充足問題を単一ワーカーで解き直す。
#        目的関数が無いので「最初に見つけた解」を返して即終了し、単一ワーカーの
#        探索は決定的なので、同じ入力なら必ず同じ解になる（設計1.1・9章）。
# 時間制限はどちらも「保険」であって、通常はそこまで使わずに終わる。
# 実測: 9月=32秒（1段目10秒＋2段目20秒） / 8月=616秒（1段目7秒＋2段目610秒）。
# 手作業より速ければ十分なので、途中で打ち切って品質や再現性を落とすより
# 待つほうを選ぶ。1段目が時間切れになると2段目も行えず再現性を失うので、
# 1段目にも余裕を持たせてある。
SOLVER_MAX_SECONDS = 600.0
SOLVER_WORKERS = 8
SOLVER_SEED = 20260901

# 2段目（同点解の一意化）の上限秒。既定は 0 = 行わない。
# 制約が増えてから1時間かけても解けなくなったため、再現性は
# 「同じ入力なら保存済みの結果を使い回す」方式に切り替えた（設計1.1）。
CANONICAL_MAX_SECONDS = 0.0


# --- モデル構築 ---------------------------------------------------------------
def solve(inp: Inputs, verbose: bool = True):
    model = cp_model.CpModel()
    pool = [s for s in inp.staff.values() if s.in_pool]
    pool_ids = [s.staff_id for s in pool]
    ND = len(inp.days)
    last = ND - 1

    x = {(s.staff_id, d, sh): model.NewBoolVar(f"x_{s.staff_id}_{d}_{sh}")
         for s in pool for d in range(ND) for sh in ALL_SHIFTS}

    def X(sid, d, sh):
        return x[(sid, d, sh)]

    fixed_req, wish_reqs = {}, []
    pool_set = set(pool_ids)
    for r in inp.requests:
        # 希望ファイルにはプール外（管理者・相談員・看護・パート）の希望も入る。
        # そちらは fill_daystaff.py が別に扱うので、ここでは無視する（設計5.6）。
        if r.staff_id not in pool_set:
            continue
        if r.date not in inp.days:
            continue
        d = (r.date - inp.days[0]).days
        if r.strength == "fixed":
            fixed_req.setdefault((r.staff_id, d), set()).update(r.allowed)
        else:
            wish_reqs.append((r.staff_id, d, r.allowed))

    work = {}
    for s in pool:
        sid = s.staff_id
        for d in range(ND):  # C1
            model.AddExactlyOne(X(sid, d, sh) for sh in ALL_SHIFTS)
        for d in range(ND):  # C5
            for sh in ALL_SHIFTS:
                # 有給は職務能力ではなく権利なので allowed_shifts の対象外。
                # 入る日は C14（本人の申請）で決まる。
                if sh != PAID and sh not in s.allowed_shifts:
                    model.Add(X(sid, d, sh) == 0)
        if not s.can_lead:  # C6
            for d in range(ND):
                model.Add(X(sid, d, DAY_L) == 0)
                model.Add(X(sid, d, DAY_LATE) == 0)
        for d in range(ND):  # C2/C3/C4
            if d < last:
                model.Add(X(sid, d + 1, NIGHT_OUT) >= X(sid, d, NIGHT_IN))
                # C3: 明の翌日は出勤しない（公休でも有給でもよい）
                model.Add(sum(X(sid, d + 1, sh) for sh in NONWORK)
                          >= X(sid, d, NIGHT_OUT))
            if d >= 1:
                model.Add(X(sid, d, NIGHT_OUT) <= X(sid, d - 1, NIGHT_IN))
        model.Add(X(sid, 0, NIGHT_OUT) == (1 if sid in inp.night_out_day1 else 0))
        for d in range(ND):  # C7
            if (sid, d) in fixed_req:
                for sh in ALL_SHIFTS:
                    if sh not in fixed_req[(sid, d)]:
                        model.Add(X(sid, d, sh) == 0)
        if s.fixed_off_weekdays:  # C12
            for d in range(ND):
                if WEEKDAY_JP[inp.days[d].weekday()] in s.fixed_off_weekdays:
                    model.Add(X(sid, d, OFF) == 1)  # 固定休は公休で埋める
        # C8/C9 は**公休のみ**を数える。有給は別枠で上乗せ（設計3.5）。
        # 「公休7＋有給2」は成り立たない。公休は必ず所定日数を取る。
        off_sum = sum(X(sid, d, OFF) for d in range(ND))
        model.Add(off_sum >= inp.off_min[sid])   # C8
        model.Add(off_sum <= inp.off_max[sid])   # C9

        # C15: 遅出（Ｃ・ＡC）の翌日に早出（Ｂ）を入れない（設計4.2）。
        # 終業が遅い日の翌朝に早出は勤務間が短すぎる。現場では「絶対なし」。
        for d in range(ND - 1):
            for sh in LATE_END:
                model.Add(X(sid, d, sh) + X(sid, d + 1, EARLY) <= 1)

        # C14: 有給は本人の申請どおりの日にのみ入る。ソルバーは勝手に付けない。
        for d in range(ND):
            if PAID not in fixed_req.get((sid, d), ()):
                model.Add(X(sid, d, PAID) == 0)
        night_sum = sum(X(sid, d, NIGHT_IN) for d in range(ND))
        if s.night_max is not None:              # C11
            model.Add(night_sum <= s.night_max)
        if s.night_min is not None:
            model.Add(night_sum >= s.night_min)
        for d in range(ND):                      # C10 の work 変数
            w = model.NewBoolVar(f"work_{sid}_{d}")
            model.Add(w == 1 - sum(X(sid, d, sh) for sh in NONWORK))
            work[(sid, d)] = w
        p = inp.prior_consecutive_days.get(sid, 0)
        for start in range(-p, ND - 5):
            pre = sum(1 for _ in range(start, min(0, start + 6)))
            in_month = [work[(sid, d)] for d in range(max(0, start), start + 6) if d < ND]
            model.Add(sum(in_month) + pre <= 5)

    # ---------------- 目的関数（ソフト制約） ----------------
    wt = inp.weights  # 有効な重み（既定W + month.yaml上書き）
    terms, count_expr, subtotal = [], {}, {}

    def add(name, weight, expr):
        count_expr[name] = expr
        subtotal[name] = weight  # 係数（day_late は下で個別処理）
        terms.append((weight, expr))

    def add_shortfall(name, per, day_fn, req):
        total = []
        for d in range(ND):
            sh = model.NewIntVar(0, len(pool_ids), f"short_{name}_{d}")
            model.Add(sh >= req - day_fn(d))
            total.append(sh)
        add(name, per, sum(total))

    add_shortfall("unfilled_night_in", wt["unfilled_night_in"],
                  lambda d: sum(X(s, d, NIGHT_IN) for s in pool_ids), inp.required["night_in"])
    add_shortfall("unfilled_early", wt["unfilled_early"],
                  lambda d: sum(X(s, d, EARLY) for s in pool_ids), inp.required["early"])
    add_shortfall("unfilled_late", wt["unfilled_late"],
                  lambda d: sum(X(s, d, LATE) + X(s, d, DAY_LATE) for s in pool_ids), inp.required["late"])

    # リーダー: プール → 管理者(100) → 不在(5000)。管理者は固定休の曜日は不可。
    mgr_vars, absent_vars = [], []
    mgr_off_wd = inp.staff[inp.manager_id].fixed_off_weekdays if inp.manager_id else set()
    for d in range(ND):
        pool_leader = sum(X(s, d, DAY_L) + X(s, d, DAY_LATE) for s in pool_ids)
        mgr = model.NewBoolVar(f"mgr_leader_{d}")
        if WEEKDAY_JP[inp.days[d].weekday()] in mgr_off_wd:
            model.Add(mgr == 0)  # 管理者は週末不可 → 不在(5000)へ
        absent = model.NewIntVar(0, 1, f"leader_absent_{d}")
        model.Add(pool_leader + mgr + absent >= inp.required["leader"])
        mgr_vars.append(mgr)
        absent_vars.append(absent)
    if inp.manager_leader_max is not None:
        model.Add(sum(mgr_vars) <= inp.manager_leader_max)   # ハード制約13（設計7.6）
    add("manager_leader", wt["manager_leader"], sum(mgr_vars))
    add("leader_absent", wt["leader_absent"], sum(absent_vars))

    # 希望(wish)の未達
    wish_viol, wish_index = [], []
    for (sid, d, allowed) in wish_reqs:
        got = sum(X(sid, d, sh) for sh in allowed
                  if sh == PAID or sh in inp.staff[sid].allowed_shifts)
        wish_viol.append(1 - got)
        wish_index.append((sid, d, allowed))
    if wish_viol:
        add("wish_unmet", wt["wish_unmet"], sum(wish_viol))

    # 休日数 off_target 未満
    # 休日数が目標を下回る／上回る（設計4.0・5.5）
    #   下回る = 働きすぎ。上回る = 1日多く休めた（上限は off_target + off_slack）。
    # どちらも「誰がそれを引き受けるか」が月ごとに偏らないよう、前月の実績で重みを変える。
    off_below, off_above, fair_below, fair_above = [], [], [], []
    for s in pool:
        sid = s.staff_id
        off_sum = sum(X(sid, d, OFF) for d in range(ND))
        below = model.NewIntVar(0, ND, f"off_below_{sid}")
        above = model.NewIntVar(0, ND, f"off_above_{sid}")
        model.Add(below >= inp.off_target[sid] - off_sum)
        model.Add(above >= off_sum - inp.off_target[sid])
        off_below.append(below)
        off_above.append(above)
        # 月またぎの公平性（設計5.5）: 前月に不利だった人は今月また不利になりにくく、
        # 前月に有利だった人は今月また有利になりにくくする。
        delta = inp.prev_off_delta.get(sid, 0)
        if delta < 0:      # 前月は目標に届かなかった → 今月また削られるのを重くする
            fair_below.append(below)
        elif delta > 0:    # 前月は1日多く休めた → 今月また多く休むのを重くする
            fair_above.append(above)
    add("off_below_target", wt["off_below_target"], sum(off_below))
    add("off_above_target", wt["off_above_target"], sum(off_above))
    add("off_fairness", wt["off_fairness"],
        (sum(fair_below) if fair_below else 0) + (sum(fair_above) if fair_above else 0))

    # 夜勤均等化（上限なし職員のみ、差1超）
    uncapped = [s.staff_id for s in pool if s.night_max is None]
    if len(uncapped) >= 2:
        nmax = model.NewIntVar(0, ND, "nmax"); nmin = model.NewIntVar(0, ND, "nmin")
        for sid in uncapped:
            ns = sum(X(sid, d, NIGHT_IN) for d in range(ND))
            model.Add(nmax >= ns); model.Add(nmin <= ns)
        spread = model.NewIntVar(0, ND, "night_spread")
        model.Add(spread >= nmax - nmin - 1)
        add("night_spread", wt["night_spread"], spread)

    # 連続勤務5日
    cons5 = []
    for s in pool:
        for d in range(ND - 4):
            r5 = model.NewIntVar(0, 1, f"run5_{s.staff_id}_{d}")
            model.Add(r5 >= sum(work[(s.staff_id, dd)] for dd in range(d, d + 5)) - 4)
            cons5.append(r5)
    add("consecutive5", wt["consecutive5"], sum(cons5))

    # 日遅（職員ごとの階段状の減点・5.3）
    #   n_i    = 職員 i の月間日遅回数
    #   over_i >= n_i - T_i （over_i >= 0）→ 最小化で自動的に max(0, n_i - T_i)
    #   penalty += c1_i * n_i + (c2_i - c1_i) * over_i
    daylate_obj = []
    daylate_count = []
    for s in pool:
        if DAY_LATE not in s.allowed_shifts:
            continue  # 日遅を取れない職員は n_i=0 で寄与なし
        n_i = sum(X(s.staff_id, d, DAY_LATE) for d in range(ND))
        daylate_count.append(n_i)
        T = s.daylate_threshold if s.daylate_threshold is not None else 0
        c1 = s.daylate_unit if s.daylate_unit is not None else DAYLATE_COST_DEFAULT
        c2 = s.daylate_over_unit if s.daylate_over_unit is not None else c1
        over = model.NewIntVar(0, ND, f"daylate_over_{s.staff_id}")
        model.Add(over >= n_i - T)
        daylate_obj.append(c1 * n_i + (c2 - c1) * over)
    count_expr["day_late"] = sum(daylate_count) if daylate_count else 0
    daylate_obj_expr = sum(daylate_obj) if daylate_obj else 0
    terms.append((1, daylate_obj_expr))  # 係数は式内に織り込み済み

    # 日勤（Ａ）の翌日の早出（Ｂ）。遅出ほどではないが負担なので減点する（設計4.2）。
    d2e = []
    for s in pool:
        for d in range(ND - 1):
            v = model.NewIntVar(0, 1, f"d2e_{s.staff_id}_{d}")
            model.Add(v >= X(s.staff_id, d, DAY_L) + X(s.staff_id, d + 1, EARLY) - 1)
            model.Add(v >= X(s.staff_id, d, DAY) + X(s.staff_id, d + 1, EARLY) - 1)
            d2e.append(v)
    add("day_to_early", wt["day_to_early"], sum(d2e))

    # 遅出（Ｃ・ＡC）の翌日に日勤（Ａ・ＡC）。終業が遅い翌日の朝からの勤務は
    # 早出ほどではないが負担なので減点する（設計4.2）。単価は職員別で、
    # 現場が「最悪あってもいい」とする職員は安く、他は高くしてある。
    l2d, l2d_cnt = [], []
    for s in pool:
        unit = s.late_to_day_unit
        if not unit:
            continue
        for d in range(ND - 1):
            v = model.NewIntVar(0, 1, f"l2d_{s.staff_id}_{d}")
            for a in LATE_END:
                for b in (DAY_L, DAY, DAY_LATE):
                    model.Add(v >= X(s.staff_id, d, a) + X(s.staff_id, d + 1, b) - 1)
            l2d.append(unit * v)
            l2d_cnt.append(v)
    count_expr["late_to_day"] = sum(l2d_cnt) if l2d_cnt else 0
    terms.append((1, sum(l2d) if l2d else 0))   # 係数は式内に織り込み済み

    # 非リーダー日勤（can_lead=true のみ）
    day_nl = sum(X(s.staff_id, d, DAY) for s in pool if s.can_lead for d in range(ND))
    add("day_nonleader", wt["day_nonleader"], day_nl)

    obj_expr = sum(coef * expr for coef, expr in terms)
    model.Minimize(obj_expr)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVER_MAX_SECONDS
    solver.parameters.num_search_workers = SOLVER_WORKERS
    solver.parameters.random_seed = SOLVER_SEED
    status = solver.Solve(model)
    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    status_name = solver.StatusName(status)
    real_obj = solver.ObjectiveValue() if feasible else None
    wall = solver.WallTime()
    canonical = False

    # --- 2段目: 同点の最適解から常に同じものを選ぶ（設計1.1 再現性・9章） ---
    # 1段目の最適値を等式で固定し、目的関数を外して充足問題にする。
    # 目的関数が無いと最初に見つけた解を返して即終了するため、
    # 単一ワーカー（探索が決定的）なら同じ入力から必ず同じ解が出る。
    # 目的値は固定してあるので最適性も保たれる。
    # 1段目が時間切れ（FEASIBLE）のときは最適値を固定できないのでスキップする。
    if status == cp_model.OPTIMAL and CANONICAL_MAX_SECONDS > 0:
        model.Add(obj_expr == round(real_obj))
        model.Proto().clear_objective()
        # 探索順序を固定する。順序が決まっていれば単一ワーカーは一直線に潜るので、
        # 解を見つけるのが速く、結果も一意になる。順序を与えずに任せると、
        # 制約が増えた月では1時間かけても見つけられなかった。
        ordered = [x[(s.staff_id, d, sh)]
                   for s in pool for d in range(ND) for sh in ALL_SHIFTS]
        model.AddDecisionStrategy(ordered, cp_model.CHOOSE_FIRST,
                                  cp_model.SELECT_MAX_VALUE)
        solver2 = cp_model.CpSolver()
        solver2.parameters.max_time_in_seconds = CANONICAL_MAX_SECONDS
        solver2.parameters.num_search_workers = 1
        solver2.parameters.random_seed = SOLVER_SEED
        # solver2.parameters.search_branching = cp_model.FIXED_SEARCH
        status2 = solver2.Solve(model)
        wall += solver2.WallTime()      # 失敗しても待った時間は時間
        if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            solver, canonical = solver2, True
        elif verbose:
            print(f"  ※ 再現性の確定に失敗（{solver2.StatusName(status2)}）。"
                  f"解は最適だが実行ごとに変わりうる")

    result = {
        "status": status_name,      # 1段目のもの（2段目は同点解の選択にすぎない）
        "feasible": feasible,
        "objective": real_obj,
        "wall_time": wall,
        "canonical": canonical,     # 2段目まで通って解が一意に確定したか
        "weights": inp.weights,
    }
    if not result["feasible"]:
        if verbose:
            print(f"  → 解なし（status={result['status']}）")
        return result

    assign = {}
    for s in pool:
        for d in range(ND):
            for sh in ALL_SHIFTS:
                if solver.Value(x[(s.staff_id, d, sh)]) == 1:
                    assign[(s.staff_id, d)] = sh
                    break
    result.update({
        "assign": assign, "pool_ids": pool_ids,
        "penalty_count": {k: solver.Value(v) for k, v in count_expr.items()},
        "daylate_subtotal": solver.Value(daylate_obj_expr) if daylate_obj else 0,
        "late_to_day_subtotal": solver.Value(sum(l2d)) if l2d else 0,
        "mgr_days": [inp.days[d] for d in range(ND) if solver.Value(mgr_vars[d]) == 1],
        "absent_days": [inp.days[d] for d in range(ND) if solver.Value(absent_vars[d]) == 1],
        "wish_unmet_list": [(sid, inp.days[d], allowed)
                            for (sid, d, allowed) in wish_index
                            if assign.get((sid, d)) not in allowed],
    })
    return result


# --- コンソール表示 -----------------------------------------------------------
def print_grid(inp, assign, pool_ids):
    print("      " + " ".join(f"{d.day:>2}" for d in inp.days))
    print("      " + " ".join(f"{WEEKDAY_JP[d.weekday()]:>2}" for d in inp.days))
    for sid in pool_ids:
        cells = [f"{GRID_SYMBOL.get(assign.get((sid, d), ''), ''):>2}" for d in range(len(inp.days))]
        print(f"{sid:<5} " + " ".join(cells))


def print_penalties(res):
    pc = res["penalty_count"]
    obj = int(res["objective"])
    print(f"\n  [ペナルティ内訳]  目的値合計 = {obj}")
    for k in ("unfilled_night_in", "unfilled_early", "unfilled_late", "leader_absent",
              "wish_unmet", "manager_leader", "off_below_target", "off_above_target",
              "off_fairness", "day_to_early", "late_to_day", "night_spread",
              "consecutive5", "day_late", "day_nonleader"):
        cnt = pc.get(k, 0)
        if k in ("day_late", "late_to_day"):
            # どちらも職員別の単価。固定の重みが weights に無いので個別に扱う。
            sub = res["daylate_subtotal"] if k == "day_late" else res["late_to_day_subtotal"]
            if cnt or sub:
                print(f"    {k:<20} 件数={cnt:<4} 重み=職員別   小計={sub}")
        else:
            if cnt:
                wk = res["weights"][k]
                print(f"    {k:<20} 件数={cnt:<4} 重み={wk:<9} 小計={wk*cnt}")


# --- 実装順序4: CSV 整形出力（6.1） ------------------------------------------
def write_shift_csv(inp, assign, pool_ids, path: Path):
    """1行1職員・1列1日・セルに勤務記号。Excel 貼り付け比較用に BOM 付き UTF-8。
    DAY_L と DAY はどちらも帳票どおり「Ａ」で出す（区別しない）。"""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["staff_id"] + [str(d.day) for d in inp.days])
        w.writerow([""] + [WEEKDAY_JP[d.weekday()] for d in inp.days])
        for sid in pool_ids:
            w.writerow([sid] + [SYMBOL.get(assign.get((sid, d), ""), "") for d in range(len(inp.days))])


def write_codes_csv(inp, assign, pool_ids, path: Path):
    """検証用のコード表記CSV。DAY_L / DAY を区別した内部コードをそのまま書く。
    帳票では「Ａ」に潰れてリーダーが復元できないため、再検証はこちらを使う。"""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["staff_id"] + [str(d.day) for d in inp.days])
        w.writerow([""] + [WEEKDAY_JP[d.weekday()] for d in inp.days])
        for sid in pool_ids:
            w.writerow([sid] + [assign.get((sid, d), "") for d in range(len(inp.days))])


# --- 実装順序4: サマリ（6.2） ------------------------------------------------
def build_summary(inp, res) -> str:
    assign, pool_ids = res["assign"], res["pool_ids"]
    ND = len(inp.days)
    buf = io.StringIO()
    p = lambda *a: print(*a, file=buf)

    p(f"# シフトサマリ {inp.year}年{inp.month}月")
    p(f"希望ファイル: {inp.requests_path}")
    p(f"最適化: {res['status']}  目的値={int(res['objective'])}  ({res['wall_time']:.2f}s)")
    p("※ この表は同点の最適解のひとつです。同じ入力なら保存済みのこの結果を"
      "使い回します（--again で引き直せます）。")

    # 未配置の一覧（日付・枠種別）
    p("\n## 未配置の枠")
    any_unfilled = False
    for d in range(ND):
        e = sum(1 for s in pool_ids if assign.get((s, d)) == EARLY)
        la = sum(1 for s in pool_ids if assign.get((s, d)) in (LATE, DAY_LATE))
        ni = sum(1 for s in pool_ids if assign.get((s, d)) == NIGHT_IN)
        short = []
        if e < inp.required["early"]:
            short.append(f"早出{inp.required['early']-e}")
        if la < inp.required["late"]:
            short.append(f"遅出{inp.required['late']-la}")
        if ni < inp.required["night_in"]:
            short.append(f"夜勤入{inp.required['night_in']-ni}")
        if short:
            any_unfilled = True
            p(f"  {inp.days[d].day:>2}日({WEEKDAY_JP[inp.days[d].weekday()]}): " + " ".join(short))
    if not any_unfilled:
        p("  なし")

    # 通らなかった希望
    p("\n## 通らなかった希望（wish）")
    if res["wish_unmet_list"]:
        for sid, date, allowed in res["wish_unmet_list"]:
            syms = "/".join(SYMBOL.get(a, a) for a in allowed)
            got = SYMBOL.get(assign.get((sid, (date - inp.days[0]).days)), "")
            p(f"  {sid} {date.day:>2}日: 希望[{syms}] → 実際[{got}]")
    else:
        p("  なし（全希望達成）")

    # 職員別
    p("\n## 職員別（夜勤 / 休日 / 連勤最長 / 日遅 / 非リーダーＡ）")
    for sid in pool_ids:
        nights = sum(1 for d in range(ND) if assign.get((sid, d)) == NIGHT_IN)
        offs = sum(1 for d in range(ND) if assign.get((sid, d)) == OFF)
        dl = sum(1 for d in range(ND) if assign.get((sid, d)) == DAY_LATE)
        da = sum(1 for d in range(ND) if assign.get((sid, d)) == DAY)
        c = m = 0
        for d in range(ND):
            if assign.get((sid, d)) != OFF:
                c += 1; m = max(m, c)
            else:
                c = 0
        p(f"  {sid:<5} 夜{nights:>2}  休{offs:>2}  連{m:>2}  日遅{dl:>2}  Ａ{da:>2}")

    # 管理者リーダー日 / リーダー不在日
    p("\n## 管理者がリーダーを担った日")
    p("  " + ("、".join(f"{d.day}日" for d in res["mgr_days"]) if res["mgr_days"] else "なし"))
    if res["absent_days"]:
        p("## リーダー不在の日（要パート増員）")
        p("  " + "、".join(f"{d.day}日" for d in res["absent_days"]))

    # パート人数警告（プール外パート未投入のため対象外）
    p("\n## パート人数0〜1人の日への警告（日曜除外）")
    has_part = any(st.role in ("driver",) or (not st.in_pool and st.role == "care")
                   for st in inp.staff.values())
    p("  パート（プール外）の勤務データが未投入のためスキップ。" if not has_part
      else "  （パート集計は未実装）")
    return buf.getvalue()


def print_validation(inp, res):
    assign, pool_ids = res["assign"], res["pool_ids"]
    ND = len(inp.days)
    unf = {"early": 0, "late": 0, "night_in": 0}
    for d in range(ND):
        unf["early"] += max(0, inp.required["early"] - sum(1 for s in pool_ids if assign.get((s, d)) == EARLY))
        unf["late"] += max(0, inp.required["late"] - sum(1 for s in pool_ids if assign.get((s, d)) in (LATE, DAY_LATE)))
        unf["night_in"] += max(0, inp.required["night_in"] - sum(1 for s in pool_ids if assign.get((s, d)) == NIGHT_IN))
    offs = {s: sum(1 for d in range(ND) if assign.get((s, d)) == OFF) for s in pool_ids}
    nights = {s: sum(1 for d in range(ND) if assign.get((s, d)) == NIGHT_IN) for s in pool_ids}
    longest = {}
    for s in pool_ids:
        c = m = 0
        for d in range(ND):
            if assign.get((s, d)) != OFF:
                c += 1; m = max(m, c)
            else:
                c = 0
        longest[s] = m
    print("\n  [検証基準（設計7.3）との突き合わせ]")
    print(f"    未配置        : 早{unf['early']} 遅{unf['late']} 入{unf['night_in']}  （合格=0）")
    print(f"    リーダー不在   : {len(res['absent_days'])}日  （合格=0）")
    mlm = inp.manager_leader_max
    print(f"    管理者リーダー : {len(res['mgr_days'])}日  "
          f"（{'合格 ≦' + str(mlm) if mlm is not None else '上限なし'}）")
    print(f"    希望未達      : {len(res['wish_unmet_list'])}件")
    # 目標は月の日数で変わる（30日月なら9、31日月なら10）ので、実数ではなく
    # 職員ごとの off_target との差で出す（設計3.2/4.0）。
    below = sum(1 for s, v in offs.items() if v < inp.off_target[s])
    over = sum(1 for s, v in offs.items() if v > inp.off_target[s])
    ontgt = len(offs) - below - over
    tgts = sorted({inp.off_target[s] for s in offs})
    print(f"    休日数        : 目標どおり{ontgt}名 / 目標+1が{over}名 / 目標未達が{below}名"
          f"  （目標={'・'.join(str(v) + '日' for v in tgts)}・未達0が合格）")
    print(f"    夜勤回数      : {sorted(nights.values(), reverse=True)}  （上限内・差1以内※上限なし勢）")
    print(f"    連続勤務5日    : {sum(1 for v in longest.values() if v==5)}名  （合格 ≦1名）")


def _parse_ym_arg(argv) -> tuple[int, int] | None:
    """位置引数 YYYY-MM を (year, month) に。無ければ month.yaml に従う（None）。
    --night-max=... の値に含まれる '-' は誤検出しないよう除外する。"""
    for a in argv[1:]:
        a = a.strip()
        if a.startswith("-"):
            continue
        if len(a) == 7 and a[4] == "-":
            y, m = a.split("-", 1)
            return int(y), int(m)
    return None


def _parse_night_max_override(argv) -> dict[str, int]:
    """--night-max SID=N（複数可）を {SID: N} に。staff.csv を書き換えずに一時上書き。"""
    ov: dict[str, int] = {}
    it = iter(argv[1:])
    for a in it:
        spec = None
        if a == "--night-max":
            spec = next(it, None)
        elif a.startswith("--night-max="):
            spec = a.split("=", 1)[1]
        if spec and "=" in spec:
            sid, n = spec.split("=", 1)
            ov[sid.strip()] = int(n)
    return ov


def input_digest(inp, data_dir: Path) -> str:
    """入力ひと揃いの指紋。同じ指紋なら同じ答えを使い回してよい（設計1.1）。

    シフトは答えが一つに定まる問題ではない。同点の最適解が多数あり、
    ソルバーはそのどれを返すか保証しない。そこで**入力が変わらないかぎり
    前回の結果をそのまま使う**ことで、実務上の安定を得る。
    引き直したいときは --again を付ける（別の解が出る）。

    ソルバー自身のソースも指紋に含める。制約や重みを変えたら答えも変わるべきで、
    古い結果を使い回してはいけないため。"""
    h = hashlib.sha256()
    h.update(f"{inp.year}-{inp.month:02d}".encode())
    files = [data_dir / "staff.csv", data_dir / "month.yaml",
             Path(__file__).resolve(),
             _resolve_month_path(data_dir.parent, data_dir, inp.year, inp.month),
             inp.requests_path,
             _resolve_carryover_path(data_dir.parent, data_dir, inp.year, inp.month)]
    for f in files:
        h.update(b"|")
        if f and Path(f).exists():
            h.update(Path(f).read_bytes())
    return h.hexdigest()[:16]


def main(argv=None):
    import sys
    argv = sys.argv if argv is None else argv
    data_dir = Path(__file__).resolve().parent.parent / "data"
    out_dir = Path(__file__).resolve().parent.parent / "out"
    nm_ov = _parse_night_max_override(argv)
    inp = load_inputs(data_dir, _parse_ym_arg(argv), nm_ov)
    if nm_ov:
        print(f"※ 一時上書き night_max: {nm_ov}（staff.csv は不変）")

    print("=" * 74)
    print(f"対象: {inp.year}年{inp.month}月 ({len(inp.days)}日)  "
          f"変則プール {sum(1 for s in inp.staff.values() if s.in_pool)}名")
    print(f"月次設定: {inp.month_path or (data_dir / 'month.yaml')}"
          + ("" if inp.month_path is None else "  （共通 month.yaml に重ねて適用）"))
    n_in = sum(1 for r in inp.requests if r.date in set(inp.days))
    print(f"希望ファイル: {inp.requests_path}  ({n_in}件"
          + (f" ／ ファイル内 {len(inp.requests)}件のうち対象月外を除外" if n_in != len(inp.requests) else "")
          + ")")
    # 月別ファイルが無いと汎用 data/requests.csv に落ちる。中身が別の月のものだと
    # 黙って「希望なし」で解いてしまうため、はっきり警告する。
    if inp.requests_path is not None and inp.requests_path.name == "requests.csv":
        print(f"  ※ 月別ファイル requests_{inp.year}-{inp.month:02d}.csv が無いため"
              f"汎用ファイルを読みました。")
        if n_in == 0:
            print("     対象月に該当する希望は0件です。希望なしで解くことになります。")
    if inp.manager_leader_max is not None:
        print(f"管理者リーダー上限: {inp.manager_leader_max}日")
    print("=" * 74)

    # 同じ入力なら前回の結果を使い回す（設計1.1）。
    # シフトは答えが一つに定まる問題ではないので、解き直すたびに違う表が出る。
    # 直した箇所以外まで組み替わるのを防ぐため、入力が変わらないかぎり据え置く。
    tag = f"{inp.year}-{inp.month:02d}"
    digest = input_digest(inp, data_dir)
    stamp = out_dir / f"shift_{tag}.hash"
    again = any(a.strip() == "--again" for a in argv[1:])
    csv_path = out_dir / f"shift_{tag}.csv"
    sum_path = out_dir / f"summary_{tag}.txt"
    if (not again and csv_path.exists() and stamp.exists()
            and stamp.read_text(encoding="utf-8").strip() == digest):
        print()
        print("前回と同じ入力なので、保存済みの結果をそのまま使います。")
        print("  別の組み方を見たいときは --again を付けてください"
              "（同じ入力でも違う表が出ます）。")
        print()
        if sum_path.exists():
            print(sum_path.read_text(encoding="utf-8"))
        print(f"出力: {csv_path}")
        return

    if again:
        print()
        print("--again のため解き直します（前回とは違う表になる可能性があります）。")

    res = solve(inp)
    if not res["feasible"]:
        return
    print_grid(inp, res["assign"], res["pool_ids"])
    print_penalties(res)
    print_validation(inp, res)

    codes_path = out_dir / f"shift_{tag}_codes.csv"
    write_shift_csv(inp, res["assign"], res["pool_ids"], csv_path)
    write_codes_csv(inp, res["assign"], res["pool_ids"], codes_path)
    summary = build_summary(inp, res)
    sum_path.write_text(summary, encoding="utf-8")
    stamp.write_text(digest, encoding="utf-8")   # 次回の使い回し判定用

    print("\n" + "=" * 74)
    print(summary)
    print("=" * 74)
    print(f"出力: {csv_path}  （Excel貼付用・帳票記号）")
    print(f"      {codes_path}  （検証用・コード表記）")
    print(f"      {sum_path}")


if __name__ == "__main__":
    main()
