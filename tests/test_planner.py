"""Kör: pytest -q   (från katalogen med smart_tou_planner.py)"""
import os, random, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pyscript", "modules"))
import smart_tou_planner as sp

TZ = ZoneInfo("Europe/Stockholm")


def mk_prices(day_values, start=datetime(2026, 10, 28, 0, 0, tzinfo=TZ)):
    """day_values: lista med kvartspriser."""
    return [{"start": (start + timedelta(minutes=15 * i)).isoformat(), "value": v}
            for i, v in enumerate(day_values)]


def two_day_prices(night=0.2, day=1.0, peak=3.0):
    one = []
    for i in range(96):
        h = i // 4
        one.append(night if h < 5 else peak if 16 <= h < 20 else day)
    return mk_prices(one + one)


def flat_profile(kw):
    p = {"all": kw}
    for h in range(24):
        p["h%d" % h] = kw
    return p


def plan(prices, now=datetime(2026, 10, 28, 0, 5, tzinfo=TZ), soc=30, **kw):
    inp = dict(prices=prices, pv=[], load_profile=flat_profile(0.8), now=now, soc_pct=soc,
               work_mode=sp.TOU_MODE, excess_pv="charge", current_periods=[], params={})
    inp.update(kw)
    return sp.make_plan(inp)


def test_flow_energy_balance():
    c = sp._Cfg()
    rnd = random.Random(1)
    for _ in range(2000):
        m = rnd.choice([0, 1, 2, 3])
        E = rnd.uniform(c.e_min, c.e_max)
        net = rnd.uniform(-5, 5) * sp.H
        D, S = max(net, 0), max(-net, 0)
        gi, ex, dE, z = sp._flow(m, E, D, S, c)
        assert gi >= -1e-9 and ex >= -1e-9
        # in = ut:  sol+nät+urladdning = last+export+laddning;  (pv - last) = -net
        charge_in = dE / c.eta if dE > 0 else 0.0
        assert abs((-net) + gi + z - ex - charge_in) < 1e-9
        assert c.e_min - 1e-9 <= E + dE <= c.e_max + 1e-9


def test_soc_bounds_and_cheap_night_charge():
    r = plan(two_day_prices())
    assert r["ok"]
    assert all(10 - 0.2 <= q["soc_pct"] <= 100.001 for q in r["quarters"])
    # laddning ska ligga inom det billiga nattfönstret (00-05) och dag 3 (onsdag)
    night = [p for p in r["periods"] if p.endswith("/3/+")]
    assert night, r["periods"]
    s0, e0 = night[0].split("/")[0].split("-")
    assert "00:00" <= s0 and e0 <= "05:00", night
    assert any(p.endswith("/-") for p in r["periods"])
    assert r["decision"] == "tou"


def test_flat_prices_choose_msc():
    r = plan(mk_prices([1.0] * 192))
    assert r["decision"] == "msc"
    assert r["periods"] == []


def test_no_grid_charge_when_sun_fills_battery():
    # billig natt, men kraftig sol imorgon -> ska inte ladda mycket från nätet
    pv = []
    d = datetime(2026, 10, 29, 0, 0, tzinfo=TZ)
    for i in range(48):
        h = i / 2
        kw = 6.0 if 9 <= h < 15 else 0.0
        pv.append({"period_start": (d + timedelta(minutes=30 * i)).isoformat(),
                   "pv_estimate": kw, "pv_estimate10": kw, "pv_estimate90": kw})
    r_sun = plan(two_day_prices(night=0.5, day=1.0, peak=2.0), pv=pv, soc=20,
                 now=datetime(2026, 10, 28, 22, 0, tzinfo=TZ))
    r_nosun = plan(two_day_prices(night=0.5, day=1.0, peak=2.0), pv=[], soc=20,
                   now=datetime(2026, 10, 28, 22, 0, tzinfo=TZ))
    grid_sun = sum(q["grid_in"] for q in r_sun["quarters"])
    grid_nosun = sum(q["grid_in"] for q in r_nosun["quarters"])
    assert r_sun["cost_plan"] < r_nosun["cost_plan"]
    assert grid_sun < grid_nosun


def test_hysteresis_own_schedule_is_kept():
    r1 = plan(two_day_prices())
    r2 = plan(two_day_prices(), current_periods=r1["periods"])
    assert r2["decision"] == "tou" and r2["apply"] is False
    assert abs(r2["cost_current"] - r2["cost_plan"]) < 0.5


def test_bad_current_schedule_is_replaced():
    r = plan(two_day_prices(), current_periods=["10:00-12:00/3/+"])  # laddar på dagpris
    assert r["apply"] is True and r["cost_current"] > r["cost_plan"]


def test_period_format_midnight_and_gap():
    now = datetime(2026, 10, 28, 22, 0, tzinfo=TZ)
    q = [dict(dt=now + timedelta(minutes=15 * i)) for i in range(16)]  # 22:00 -> 02:00
    modes = [sp.CHARGE] * 6 + [sp.DISCHARGE] * 4 + [sp.CHARGE] * 6
    ps = sp.modes_to_periods(q, modes)
    # 22:00-23:30 laddar, 23:30-00:30 laddar ur (delas vid midnatt), 00:30-02:00 laddar
    assert ps == ["22:00-23:29/3/+", "23:30-23:59/3/-", "00:00-00:29/4/-", "00:30-02:00/4/+"], ps


def test_parse_and_forced_roundtrip():
    assert sp.parse_period("00:00-05:00/3/+") == (0, 300, {3}, 1)
    assert sp.parse_period("14:00-23:59/12/-") == (840, 1440, {1, 2}, 2)
    assert sp.parse_period("skräp") is None
    now = datetime(2026, 10, 28, 4, 30, tzinfo=TZ)
    q = [dict(dt=now + timedelta(minutes=15 * i)) for i in range(4)]
    assert sp.forced_from_periods(["00:00-05:00/3/+"], q) == [1, 1, 0, 0]


def test_max_periods_respected():
    zig = []
    for i in range(192):
        zig.append(0.3 if (i // 2) % 2 == 0 else 3.0)   # hackigt pris -> många block
    r = plan(mk_prices(zig))
    assert len(r["periods"]) <= 14


def test_load_profile_builder():
    tz = TZ
    rows = []
    start = datetime(2026, 9, 1, 0, 0, tzinfo=tz)
    for h in range(24 * 28):
        t = start + timedelta(hours=h)
        rows.append((t.timestamp(), 500.0 if t.hour < 6 else 1500.0))   # W
    prof = sp.build_load_profile(rows, "Europe/Stockholm", scale=0.001)
    assert abs(prof["3-2"] - 0.5) < 1e-6 and abs(prof["3-18"] - 1.5) < 1e-6
    assert sp.build_load_profile([], "Europe/Stockholm") == {}


def test_cold_forces_tou_even_with_flat_prices_when_periods_exist():
    # måttlig spridning: utan kyla -> MSC, med kyla -> TOU (så länge planen har perioder)
    vals = [1.0 + (0.5 if 16 <= (i % 96) // 4 < 20 else 0.0) for i in range(192)]
    warm = plan(mk_prices(vals), temperature_c=12.0, work_mode=sp.MSC_MODE)
    cold = plan(mk_prices(vals), temperature_c=2.0, work_mode=sp.MSC_MODE)
    assert warm["decision"] == "msc"
    if cold["periods"]:
        assert cold["decision"] == "tou" and cold["cold"] is True


def test_cold_hysteresis_and_no_periods_fallback():
    r_on = plan(two_day_prices(), temperature_c=5.5, work_mode=sp.TOU_MODE)   # under 5+1
    r_off = plan(two_day_prices(), temperature_c=5.5, work_mode=sp.MSC_MODE)  # över 5
    assert r_on["cold"] is True and r_off["cold"] is False
    flat = plan(mk_prices([1.0] * 192), temperature_c=-5.0)
    assert flat["decision"] == "msc" and flat["periods"] == []   # inget att schemalägga


def test_schedule_hours_keeps_past_and_marks_now():
    now = datetime(2026, 10, 28, 12, 7, tzinfo=TZ)
    prev = {datetime(2026, 10, 28, 3, 0, tzinfo=TZ).isoformat(): 1}      # laddade 03:00 (passerat, finns ej längre i växelriktaren)
    res = sp.schedule_hours(["15:30-20:45/3/-", "06:30-09:00/4/-"], now, "Europe/Stockholm", prev)
    h = {x["start"][:16]: x for x in res["hours"]}
    assert len(res["hours"]) == 192
    assert h["2026-10-28T03:00"]["charge"] == 1          # historik bevarad
    assert h["2026-10-28T16:00"]["on"] == 1 and h["2026-10-28T12:00"]["on"] == 0
    assert h["2026-10-29T07:00"]["on"] == 1
    assert res["current"] == {"charge": 0, "on": 0}
    res2 = sp.schedule_hours(["12:00-13:00/3/+"], now, "Europe/Stockholm")
    assert res2["current"] == {"charge": 1, "on": 0}


def test_kept_schedule_is_what_is_shown():
    r1 = plan(two_day_prices())
    r2 = plan(two_day_prices(), current_periods=r1["periods"])
    assert r2["apply"] is False
    assert r2["periods"] == r1["periods"]            # visar det som gäller
    assert "redan ligger i växelriktaren" in r2["explanation"]
    r3 = plan(two_day_prices(), current_periods=["10:00-12:00/3/+"])
    assert r3["apply"] is True and "redan ligger" not in r3["explanation"]


def test_ev_session_is_only_a_battery_lock_window():
    ev = {"active": False, "start": datetime(2026, 10, 28, 17, 0, tzinfo=TZ).isoformat(), "remaining_kwh": 28.0,
          "plugged": True, "level": 34, "limit": 70}
    r = plan(two_day_prices(), ev=ev)
    win = [q for q in r["quarters"] if q["ev_window"]]
    assert win[0]["start"][11:16] == "17:00" and win[-1]["start"][11:16] == "21:30"   # 28 kWh / 6 kW = 4,67 h
    assert all(q["batt_out"] == 0 for q in win)                      # ingen urladdning under billaddning
    assert all(q["batt_in"] <= 2.0 * 0.25 + 1e-6 for q in win)       # max 2 kW in
    assert "Bilen laddar 17:00" in r["explanation"] and r["ev_summary"].startswith("Bil (34 % -> 70 %)")
    # bilens kWh ingår INTE som last: husets nätinköp i fönstret = bara husets egen förbrukning
    base = plan(two_day_prices())
    ref = {q["start"]: q for q in base["quarters"]}
    assert all(abs(q["load_kwh"] - ref[q["start"]]["load_kwh"]) < 1e-9 for q in win)
    # men låsningen kostar: batteriet kan inte användas under toppen
    assert base["cost_plan"] < r["cost_plan"]


def test_ev_active_now_and_unplugged_ignored():
    now = datetime(2026, 10, 28, 12, 7, tzinfo=TZ)
    r = plan(two_day_prices(), now=now, ev={"active": True, "remaining_kwh": 6.0, "plugged": True, "level": 60, "limit": 70})
    win = [q for q in r["quarters"] if q["ev_window"]]
    assert win[0]["start"][11:16] == "12:00" and len(win) == 4          # 6 kWh / 6 kW = 1 h
    r2 = plan(two_day_prices(), now=now, ev={"active": False, "start": None, "remaining_kwh": 20.0, "plugged": True, "level": 30, "limit": 70})
    assert all(q["ev_window"] == 0 for q in r2["quarters"]) and "inkopplad men ingen laddning" in r2["explanation"]
