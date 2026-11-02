"""
smart_tou_planner.py  -  ren Python-planerare för Huawei LUNA2000 (TOU).

Inga Home Assistant-importer här, så filen kan testas utanför HA och senare
återanvändas i en riktig custom integration.

Modell (per kvart, 15 min):
  IDLE_SAVE  : batteriet står still, solöverskott laddar batteriet
               (select.battery_excess_pv_energy_use_in_tou = charge)
  IDLE_SELL  : batteriet står still, solöverskott exporteras
               (... = fed_to_grid)
  CHARGE     : TOU-period "+"  (laddar från sol + nät, full effekt)
  DISCHARGE  : TOU-period "-"  (batteriet täcker husets underskott, ingen export)

Planeringen görs med dynamisk programmering (bakåt) över batteriets
energinivå med linjär interpolation, så avrundningsfel ackumuleras inte.
Kostnad = nätinköp*pris - export*exportpris + slitage*urladdad energi
          + små straff för nya TOU-perioder / byte av solrouting.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

H = 0.25  # timmar per kvart

IDLE_SAVE, CHARGE, DISCHARGE, IDLE_SELL = 0, 1, 2, 3
MODE_NAME = {0: "idle_save", 1: "charge", 2: "discharge", 3: "idle_sell"}
ROUTE_OF = {IDLE_SAVE: "charge", IDLE_SELL: "fed_to_grid"}

TOU_MODE = "time_of_use_luna2000"
MSC_MODE = "maximise_self_consumption"

DEFAULTS = dict(
    capacity_kwh=15.0,        # nyttig kapacitet
    min_soc_pct=10.0,         # planeringsgolv
    max_soc_pct=100.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    round_trip_eff=0.90,
    wear_cost=0.30,           # SEK per kWh som batteriet levererar
    grid_cost=0.90,           # SEK/kWh som läggs på Nordpool-priset vid KÖP (nät, skatt, ev. moms)
    export_factor=0.80,       # exportpris = sensorpris * faktor + bonus - avgift  (0.80 = momsen bort)
    export_bonus=0.02,        # SEK/kWh extra per såld kWh
    export_fee=0.0,
    period_penalty=0.15,      # SEK per ny TOU-period (håller blocken långa)
    route_penalty=0.02,       # SEK per byte sälja/spara solöverskott
    idle_eps=0.002,           # SEK per kvart en CHARGE/DISCHARGE-period inte gör något (fullt/tomt batteri)
    edge_trim_quarters=12,    # korta block (<=30 min) inom så många kvartar från horisontens slut tas bort
    soc_step_kwh=0.25,
    pv_w10=0.5, pv_w50=0.5, pv_w90=0.0,   # vikter på Solcast P10/P50/P90
    load_factor=1.0,
    terminal_percentile=0.25, # värdering av energi kvar i batteriet vid horisontens slut
    max_periods=14,
    tou_on_threshold=2.0,     # SEK: så mycket bättre än MSC måste TOU vara för att slå på
    tou_off_threshold=0.0,    # SEK: redan i TOU går vi bara tillbaka till MSC om TOU inte är bättre (0) eller planen saknar perioder
    cold_threshold_c=5.0,     # under denna temperatur: alltid TOU (aldrig MSC) om planen har perioder
    cold_hysteresis_c=1.0,    # redan i TOU: håll kvar tills temperaturen är gräns + detta
    ev_kw=6.0,                # snittladdning bil (för att räkna ut hur länge den laddar)
    ev_battery_charge_kw=2.0, # husbatteriet får laddas max så här mycket medan bilen laddar
    ev_battery_discharge_kw=0.0,  # ...och laddas inte ur alls
    replan_min_gain=1.0,      # SEK: så mycket bättre måste ny plan vara för att byta schema
    tz="Europe/Stockholm",
    fallback_load_kw=1.0,
)


class _Cfg:
    def __init__(self, overrides=None):
        d = dict(DEFAULTS)
        d.update(overrides or {})
        self.__dict__.update(d)
        self.eta = math.sqrt(self.round_trip_eff)
        self.e_min = self.capacity_kwh * self.min_soc_pct / 100.0
        self.e_max = self.capacity_kwh * self.max_soc_pct / 100.0


# --------------------------------------------------------------------------
# Tid och indata
# --------------------------------------------------------------------------
def _to_dt(x, tz):
    if isinstance(x, datetime):
        d = x
    else:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz)
    return d.astimezone(tz)


def _quarters(prices, now, tz):
    """Sorterade, framtida kvartar (inkl. pågående) utan luckor."""
    items = {}
    for p in prices or []:
        try:
            st = _to_dt(p.get("start"), tz)
            items[st] = float(p.get("value"))
        except Exception:
            continue
    out = []
    prev = None
    for st in sorted(items):
        if st + timedelta(minutes=15) <= now:
            continue
        if prev is not None and st - prev != timedelta(minutes=15):
            break  # lucka -> sluta här
        out.append((st, items[st]))
        prev = st
    return out


def _pv_map(entries, c, tz):
    w = [c.pv_w10, c.pv_w50, c.pv_w90]
    s = sum(w) or 1.0
    out = {}
    for e in entries or []:
        try:
            st = _to_dt(e.get("period_start"), tz)
            p50 = float(e.get("pv_estimate") or 0.0)
            p10 = e.get("pv_estimate10")
            p90 = e.get("pv_estimate90")
            p10 = p50 if p10 is None else float(p10)
            p90 = p50 if p90 is None else float(p90)
            ts = int(st.timestamp())
            out[ts - ts % 1800] = (w[0] * p10 + w[1] * p50 + w[2] * p90) / s
        except Exception:
            continue
    return out


def build_load_profile(rows, tz_name="Europe/Stockholm", scale=1.0, shrink_k=2.0):
    """
    rows: [(unix_ts_start_of_hour, mean_power_in_sensor_unit), ...]
    scale: 0.001 om sensorn är i W, 1.0 om kW.
    Returnerar {"<veckodag>-<timme>": kW, "h<timme>": kW, "all": kW, "_samples": n}
    Veckodag+timme-cellen dras mot timmedelvärdet (shrinkage) när få prov finns.
    """
    tz = ZoneInfo(tz_name)
    cells, hours, allv = {}, {}, []
    for ts, m in rows or []:
        try:
            v = float(m) * scale
        except Exception:
            continue
        if v != v or v < 0:
            continue
        dt = datetime.fromtimestamp(float(ts), tz)
        cells.setdefault((dt.isoweekday(), dt.hour), []).append(v)
        hours.setdefault(dt.hour, []).append(v)
        allv.append(v)
    if not allv:
        return {}
    overall = sum(allv) / len(allv)
    prof = {"all": overall, "_samples": len(allv)}
    for h in range(24):
        hv = hours.get(h)
        hmean = sum(hv) / len(hv) if hv else overall
        prof["h%d" % h] = hmean
        for wd in range(1, 8):
            cv = cells.get((wd, h), [])
            prof["%d-%d" % (wd, h)] = (sum(cv) + shrink_k * hmean) / (len(cv) + shrink_k)
    return prof


def _ev_profile(dts, ev, now, tz, c):
    """Medeleffekt (kW) för bilen per kvart. ev = {active, start, remaining_kwh}."""
    out = [0.0] * len(dts)
    rem = float((ev or {}).get("remaining_kwh") or 0.0)
    if rem < 0.2:
        return out
    if ev.get("active"):
        start = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    elif ev.get("start"):
        start = _to_dt(ev["start"], tz)
    else:
        return out
    for i, dt in enumerate(dts):
        end = dt + timedelta(minutes=15)
        if end <= start:
            continue
        frac = 1.0 if dt >= start else (end - start).total_seconds() / 900.0
        e = min(c.ev_kw * H * frac, rem)
        out[i] = e / H
        rem -= e
        if rem <= 1e-9:
            break
    return out


def _load_kw(profile, dt, c):
    if profile:
        v = profile.get("%d-%d" % (dt.isoweekday(), dt.hour))
        if v is None:
            v = profile.get("h%d" % dt.hour, profile.get("all", c.fallback_load_kw))
    else:
        v = c.fallback_load_kw
    return float(v) * c.load_factor


# --------------------------------------------------------------------------
# Fysik per kvart
# --------------------------------------------------------------------------
def _flow(mode, E, D, S, c, cmax=None, dmax=None):
    """Returnerar (nätinköp kWh, export kWh, dE lagrad kWh, levererat från batteri kWh)."""
    room = (c.e_max - E) / c.eta
    if room < 0.0:
        room = 0.0
    max_in = (c.max_charge_kw if cmax is None else cmax) * H
    if mode == IDLE_SELL:
        return D, S, 0.0, 0.0
    if mode == IDLE_SAVE:
        x = min(S, max_in, room)
        return D, S - x, x * c.eta, 0.0
    if mode == CHARGE:
        y = min(max_in, room)
        used = min(S, y)
        return D + (y - used), S - used, y * c.eta, 0.0
    # DISCHARGE
    avail = (E - c.e_min) * c.eta
    if avail < 0.0:
        avail = 0.0
    z = min(D, (c.max_discharge_kw if dmax is None else dmax) * H, avail)
    return D - z, S, -z / c.eta, z


def _cost(m, qt, gi, ex, z, c):
    cost = qt["p"] * gi - qt["q"] * ex + c.wear_cost * z
    if (m == CHARGE and gi - qt["D"] <= 1e-9) or (m == DISCHARGE and z <= 1e-9):
        cost += c.idle_eps  # perioden gör ingenting -> hellre vila
    return cost


def _penalty(prev, m, c, period_penalty):
    if m == prev:
        return 0.0
    if m in (CHARGE, DISCHARGE):
        return period_penalty
    if prev in (IDLE_SAVE, IDLE_SELL):
        return c.route_penalty
    return 0.0


def _allowed(forced_t):
    if forced_t is None:
        return (IDLE_SAVE, CHARGE, DISCHARGE, IDLE_SELL)
    if forced_t == 1:
        return (CHARGE,)
    if forced_t == 2:
        return (DISCHARGE,)
    return (IDLE_SAVE, IDLE_SELL)


def _interp(arr, E, base, step):
    x = (E - base) / step
    if x <= 0.0:
        return arr[0]
    last = len(arr) - 1
    if x >= last:
        return arr[last]
    i = int(x)
    f = x - i
    return arr[i] * (1.0 - f) + arr[i + 1] * f


def _terminal(E, c, tv):
    e = E - c.e_min
    return -tv * e * c.eta if e > 0 else 0.0


def _solve(q, c, E0, init_mode, forced, tv, period_penalty):
    n = len(q)
    base = min(c.e_min, E0)
    S_n = max(2, int(math.ceil((c.e_max - base) / c.soc_step_kwh - 1e-9)) + 1)
    step = (c.e_max - base) / (S_n - 1)
    grid = [base + i * step for i in range(S_n)]

    term = [_terminal(E, c, tv) for E in grid]
    V = [None] * (n + 1)
    V[n] = [term[:] for _ in range(4)]

    for t in range(n - 1, -1, -1):
        qt = q[t]
        allowed = _allowed(forced[t] if forced else None)
        Vn = V[t + 1]
        G = {}
        for m in allowed:
            Gm = [0.0] * S_n
            Vm = Vn[m]
            for i in range(S_n):
                E = grid[i]
                gi, ex, dE, z = _flow(m, E, qt["D"], qt["S"], c, qt["cmax"], qt["dmax"])
                cost = _cost(m, qt, gi, ex, z, c)
                Gm[i] = cost + _interp(Vm, E + dE, base, step)
            G[m] = Gm
        Vt = []
        for prev in range(4):
            row = [1e18] * S_n
            for m in allowed:
                pen = _penalty(prev, m, c, period_penalty)
                Gm = G[m]
                for i in range(S_n):
                    v = Gm[i] + pen
                    if v < row[i]:
                        row[i] = v
            Vt.append(row)
        V[t] = Vt

    # framåt: välj bästa läge med exakta (kontinuerliga) flöden
    E = E0
    prev = init_mode
    modes, recs = [], []
    total = 0.0
    for t in range(n):
        qt = q[t]
        allowed = list(_allowed(forced[t] if forced else None))
        if prev in allowed:
            allowed.remove(prev)
            allowed.insert(0, prev)
        best = None
        for m in allowed:
            gi, ex, dE, z = _flow(m, E, qt["D"], qt["S"], c, qt["cmax"], qt["dmax"])
            cost = _cost(m, qt, gi, ex, z, c)
            tot = cost + _penalty(prev, m, c, period_penalty) + _interp(V[t + 1][m], E + dE, base, step)
            if best is None or tot < best[0] - 1e-9:
                best = (tot, m, gi, ex, dE, z, cost)
        _, m, gi, ex, dE, z, cost = best
        E = min(max(E + dE, 0.0), c.e_max)
        total += cost + _penalty(prev, m, c, period_penalty)
        modes.append(m)
        recs.append(dict(mode=m, grid_in=gi, export=ex, soc_pct=100.0 * E / c.capacity_kwh, cost=cost))
        prev = m
    net = total + _terminal(E, c, tv)
    return dict(modes=modes, recs=recs, net_cost=net, E_end=E)


def _trim_edge_blocks(modes, c):
    """Ta bort 15-30 min-block nära horisontens slut (omplaneras ändå i morgon)."""
    n = len(modes)
    out = list(modes)
    i = 0
    while i < n:
        if out[i] in (CHARGE, DISCHARGE):
            j = i
            while j < n and out[j] == out[i]:
                j += 1
            if (j - i) <= 2 and i >= n - c.edge_trim_quarters:
                for k in range(i, j):
                    out[k] = IDLE_SAVE
            i = j
        else:
            i += 1
    return out


def _replay(q, c, E0, modes, tv, init_mode, period_penalty):
    """Kör givna lägen med exakta flöden. Ger poster per kvart och nettokostnad."""
    E = E0
    prev = init_mode
    total = 0.0
    recs = []
    for qt, m in zip(q, modes):
        gi, ex, dE, z = _flow(m, E, qt["D"], qt["S"], c, qt["cmax"], qt["dmax"])
        cost = _cost(m, qt, gi, ex, z, c)
        E = min(max(E + dE, 0.0), c.e_max)
        total += cost + _penalty(prev, m, c, period_penalty)
        recs.append(dict(mode=m, grid_in=gi, export=ex, soc_pct=100.0 * E / c.capacity_kwh, cost=cost,
                         batt_in=(dE / c.eta if dE > 0 else 0.0), batt_out=z))
        prev = m
    return dict(modes=list(modes), recs=recs, net_cost=total + _terminal(E, c, tv), E_end=E)


def _simulate_msc(q, c, E0, tv):
    E = E0
    total = 0.0
    for qt in q:
        m = DISCHARGE if qt["D"] > 0 else IDLE_SAVE
        gi, ex, dE, z = _flow(m, E, qt["D"], qt["S"], c, qt["cmax"], qt["dmax"])
        total += qt["p"] * gi - qt["q"] * ex + c.wear_cost * z
        E = min(max(E + dE, 0.0), c.e_max)
    return total + _terminal(E, c, tv)


# --------------------------------------------------------------------------
# TOU <-> kvartslägen
# --------------------------------------------------------------------------
def parse_period(s):
    """'HH:MM-HH:MM/DAGAR/+' -> (start_min, end_min, {dagar}, 1|2) eller None."""
    try:
        rng, days, sign = str(s).strip().split("/")
        a, b = rng.split("-")
        sh, sm = [int(x) for x in a.split(":")]
        eh, em = [int(x) for x in b.split(":")]
        start, end = sh * 60 + sm, eh * 60 + em
        if end == 0 or end >= 1439:
            end = 1440
        return start, end, {int(d) for d in days if d.isdigit()}, (1 if sign.strip() == "+" else 2)
    except Exception:
        return None


def forced_from_periods(period_strings, q):
    parsed = [p for p in (parse_period(s) for s in period_strings or []) if p]
    out = []
    for qt in q:
        dt = qt["dt"]
        mid = dt.hour * 60 + dt.minute + 7.5
        cls = 0
        for start, end, days, sign in parsed:
            if dt.isoweekday() in days and start <= mid < end:
                cls = sign
                break
        out.append(cls)
    return out


def modes_to_periods(q, modes):
    """Sammanhängande CHARGE/DISCHARGE-kvartar -> TOU-strängar (delas vid midnatt)."""
    runs = []
    for qt, m in zip(q, modes):
        if m not in (CHARGE, DISCHARGE):
            continue
        dt = qt["dt"]
        end = dt + timedelta(minutes=15)
        if runs and runs[-1]["mode"] == m and runs[-1]["end"] == dt and runs[-1]["date"] == dt.date():
            runs[-1]["end"] = end
        else:
            runs.append(dict(mode=m, start=dt, end=end, date=dt.date()))
    # Huawei vill troligen inte ha periodslut == nästa periods start
    for a, b in zip(runs, runs[1:]):
        a["gap"] = (a["date"] == b["date"] and a["end"] == b["start"])
    out = []
    for r in runs:
        s = r["start"].strftime("%H:%M")
        if r["end"].date() != r["date"]:
            e = "23:59"
        elif r.get("gap"):
            e = (r["end"] - timedelta(minutes=1)).strftime("%H:%M")
        else:
            e = r["end"].strftime("%H:%M")
        out.append("%s-%s/%d/%s" % (s, e, r["start"].isoweekday(), "+" if r["mode"] == CHARGE else "-"))
    return out


def _route_runs(q, modes):
    runs = []
    for qt, m in zip(q, modes):
        r = ROUTE_OF.get(m)
        if r is None:
            continue
        dt = qt["dt"]
        end = dt + timedelta(minutes=15)
        if runs and runs[-1][2] == r and runs[-1][1] == dt:
            runs[-1][1] = end
        else:
            runs.append([dt, end, r])
    return [[a.isoformat(), b.isoformat(), r] for a, b, r in runs]


def schedule_hours(period_strings, now, tz_name, prev=None, days=2):
    """
    Kvart-för-kvart på/av för idag + imorgon, i samma form som gamla battery_charge/battery_on.
    Passerade kvartar behåller sitt tidigare värde (prev), eftersom passerade perioder skrivs bort ur växelriktaren.
    Returnerar {"hours": [{start, charge, on}], "map": {iso: klass}, "current": {"charge":0|1,"on":0|1}}
    """
    tz = ZoneInfo(tz_name)
    now = _to_dt(now, tz)
    day0 = datetime(now.year, now.month, now.day, tzinfo=tz)
    end_date = day0.date() + timedelta(days=days)
    t = day0.astimezone(timezone.utc)
    q = []
    while True:
        dt = t.astimezone(tz)
        if dt.date() >= end_date:
            break
        q.append(dict(dt=dt))
        t += timedelta(minutes=15)
    cls = forced_from_periods(period_strings, q)
    prev = prev or {}
    cur_start = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    hours, newmap, current = [], {}, {"charge": 0, "on": 0}
    for qt, c in zip(q, cls):
        key = qt["dt"].isoformat()
        if qt["dt"] < cur_start and key in prev:
            c = int(prev[key])
        hours.append(dict(start=key, charge=1 if c == 1 else 0, on=1 if c == 2 else 0))
        newmap[key] = c
        if qt["dt"] == cur_start:
            current = {"charge": 1 if c == 1 else 0, "on": 1 if c == 2 else 0}
    return dict(hours=hours, map=newmap, current=current)


# --------------------------------------------------------------------------
# Förklaring i klartext
# --------------------------------------------------------------------------
_WD = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]


def _day(d, today):
    diff = (d - today).days
    return "idag" if diff == 0 else "imorgon" if diff == 1 else _WD[d.weekday()]


_WD_SHORT = ["", "mån", "tis", "ons", "tors", "fre", "lör", "sön"]


def _pretty_period(s):
    """'16:00-22:45/2/-' -> 'tis 16:00-22:45 laddar ur'"""
    try:
        rng, days, sign = str(s).strip().split("/")
        d = " ".join(_WD_SHORT[int(x)] for x in days if x.isdigit())
        return "%s %s %s" % (d, rng, "laddar" if sign.strip() == "+" else "laddar ur")
    except Exception:
        return str(s)


def _kr(x):
    return ("%.2f" % x).replace(".", ",")


def _kwh(x):
    return ("%.1f" % x).replace(".", ",")


def _cls(m):
    return 1 if m == CHARGE else 2 if m == DISCHARGE else 0


def _explain(q, sol, c, now, decision, reason, advantage, cold, temp, kept=False, proposed=None, ev_info=None):
    today = now.date()
    modes, recs = sol["modes"], sol["recs"]
    lines = []
    head = "**%s** - %s." % ("Batteriet schemaläggs (TOU)" if decision == "tou" else "Batteriet kör MSC", reason)
    if cold and temp is not None and "kallt" not in reason:
        head += " Det är kallt (%s °C) så TOU används oavsett vinst." % _kwh(float(temp))
    lines.append(head)
    if kept:
        lines.append("Texten nedan beskriver schemat som **redan ligger i växelriktaren** (det behålls).")
        if proposed:
            lines.append("Det finns ett något annorlunda förslag, men vinsten är för liten för att byta schema: "
                         + ", ".join(_pretty_period(x) for x in proposed))

    runs = []
    for qt, m, r in zip(q, modes, recs):
        if m not in (CHARGE, DISCHARGE):
            continue
        end = qt["dt"] + timedelta(minutes=15)
        if runs and runs[-1]["m"] == m and runs[-1]["end"] == qt["dt"] and runs[-1]["d"] == qt["dt"].date():
            run = runs[-1]
            run["end"] = end
        else:
            run = dict(m=m, start=qt["dt"], end=end, d=qt["dt"].date(), kwh=0.0, p=[])
            runs.append(run)
        run["p"].append(qt["p"])
        run["kwh"] += (r["grid_in"] - qt["D"]) if m == CHARGE else r["batt_out"]

    if decision == "tou":
        for run in runs:
            if run["kwh"] < 0.2:
                continue
            avg = sum(run["p"]) / len(run["p"])
            when = "%s %s-%s" % (_day(run["d"], today), run["start"].strftime("%H:%M"), run["end"].strftime("%H:%M"))
            if run["m"] == CHARGE:
                lines.append("Laddar från nätet %s: cirka %s kWh, snitt %s kr/kWh inkl. avgifter." % (when, _kwh(run["kwh"]), _kr(avg)))
            else:
                lines.append("Laddar ur %s: cirka %s kWh till huset, ersätter el som kostar snitt %s kr/kWh." % (when, _kwh(run["kwh"]), _kr(avg)))
        if not any(r["kwh"] >= 0.2 for r in runs):
            lines.append("Inga laddningar eller urladdningar behövs i den här planen.")

        sell = [(qt["dt"], r) for qt, m, r in zip(q, modes, recs) if m == IDLE_SELL and r["export"] > 0.05]
        if sell:
            lines.append("Solöverskott **säljs** %s-%s (%s kWh) eftersom det är mer värt än att spara det." % (
                sell[0][0].strftime("%H:%M"), (sell[-1][0] + timedelta(minutes=15)).strftime("%H:%M"),
                _kwh(sum(r["export"] for _, r in sell))))
        else:
            lines.append("Solöverskott **sparas** i batteriet - såld el ger mindre än den el som sparas undan.")
    else:
        lines.append("Batteriet laddar från solen och laddar ur när huset behöver el. Ingen nätladdning planeras.")

    pv = {}
    for qt in q:
        pv[qt["dt"].date()] = pv.get(qt["dt"].date(), 0.0) + qt["pv"] * H
    if pv:
        lines.append("Planerad sol: " + ", ".join("%s %s kWh" % (_day(d, today), _kwh(v)) for d, v in sorted(pv.items()))
                     + " (försiktig blandning av Solcast P10/P50).")
    ev_text = None
    evq = [qt for qt in q if qt["ev"] > 0]
    ev_info = ev_info or {}
    if evq:
        a, b = evq[0]["dt"], evq[-1]["dt"] + timedelta(minutes=15)
        lvl, lim = ev_info.get("level"), ev_info.get("limit")
        lv = " (%d %% -> %d %%)" % (round(lvl), round(lim)) if lvl is not None and lim is not None else ""
        if ev_info.get("active"):
            when = "nu (väntar på ström)" if ev_info.get("waiting") else "nu"
        else:
            when = _day(a.date(), today) + " " + a.strftime("%H:%M")
        ev_text = "Bil%s: laddar %s till ca %s" % (lv, when, b.strftime("%H:%M"))
        lines.append("Bilen laddar %s-%s (uppskattat). Under tiden laddas husbatteriet max %s kW och laddas inte ur, "
                     "så planen lägger urladdningen före eller efter." % (
                         a.strftime("%H:%M"), b.strftime("%H:%M"), _kwh(c.ev_battery_charge_kw)))
    elif ev_info.get("plugged") and (ev_info.get("remaining_kwh") or 0) >= 0.2:
        ev_text = "Bil: inkopplad men ingen laddning schemalagd"
        lines.append("Bilen är inkopplad men ingen laddning är schemalagd, så den ingår inte i planen. Starta/schemalägg laddningen så räknas planen om.")
    lines.append("Batteriet förväntas ligga på %d %% vid planens slut." % round(recs[-1]["soc_pct"]))

    # nuläge och nästa händelse
    names = {IDLE_SAVE: "Vilar - solöverskott sparas", IDLE_SELL: "Vilar - solöverskott säljs",
             CHARGE: "Laddar från nät + sol", DISCHARGE: "Laddar ur till huset"}
    now_state = names[modes[0]] if decision == "tou" else "MSC (batteriet följer huset)"
    nxt = None
    if decision == "tou":
        for i in range(1, len(modes)):
            if _cls(modes[i]) != _cls(modes[0]):
                nxt = (q[i]["dt"], modes[i])
                break
    next_text, next_at = None, None
    if nxt:
        label = {CHARGE: "laddar från nät", DISCHARGE: "laddar ur", IDLE_SAVE: "vilar", IDLE_SELL: "vilar"}[nxt[1]]
        next_text = "%s %s %s" % (label, _day(nxt[0].date(), today), nxt[0].strftime("%H:%M"))
        next_at = nxt[0].isoformat()
    text = "\n".join("- " + ln if i else ln for i, ln in enumerate(lines)).replace("\n- ", "\n\n- ", 1)
    return text, now_state, next_text, next_at, ev_text


# --------------------------------------------------------------------------
# Huvud-API
# --------------------------------------------------------------------------
def make_plan(inp):
    """
    inp = {
      prices: [{start, value}]  (15 min, SEK/kWh)
      pv: [{period_start, pv_estimate, pv_estimate10, pv_estimate90}]  (kW, 30 min)
      load_profile: dict från build_load_profile
      now: datetime | iso
      soc_pct: float
      work_mode: str | None
      excess_pv: 'charge' | 'fed_to_grid' | None
      current_periods: [str] | None
      params: {överstyr DEFAULTS}
    }
    """
    c = _Cfg(inp.get("params"))
    tz = ZoneInfo(c.tz)
    now = _to_dt(inp["now"], tz)

    qraw = _quarters(inp.get("prices"), now, tz)
    if len(qraw) < 4:
        return dict(ok=False, reason="För få priskvartar (%d)" % len(qraw))

    pvm = _pv_map(inp.get("pv"), c, tz)
    prof = inp.get("load_profile") or {}
    ev_info = inp.get("ev") or {}
    ev_kw = _ev_profile([d for d, _ in qraw], ev_info, now, tz, c)
    q = []
    for (dt, p), evk in zip(qraw, ev_kw):
        ts = int(dt.timestamp())
        pv = pvm.get(ts - ts % 1800, 0.0)
        load = _load_kw(prof, dt, c)
        net = (load - pv) * H   # bilens el ingår inte i planen, bara att husbatteriet är låst medan den laddar
        q.append(dict(
            dt=dt, spot=p, p=p + c.grid_cost,
            q=p * c.export_factor + c.export_bonus - c.export_fee, load=load, pv=pv, ev=evk,
            cmax=min(c.max_charge_kw, c.ev_battery_charge_kw) if evk > 0 else c.max_charge_kw,
            dmax=min(c.max_discharge_kw, c.ev_battery_discharge_kw) if evk > 0 else c.max_discharge_kw,
            D=net if net > 0 else 0.0, S=-net if net < 0 else 0.0,
        ))

    E0 = float(inp["soc_pct"]) / 100.0 * c.capacity_kwh
    prices_sorted = sorted(x["p"] for x in q)
    tv = prices_sorted[int(c.terminal_percentile * (len(prices_sorted) - 1))]

    work_mode = inp.get("work_mode")
    current_periods = inp.get("current_periods")
    excess = inp.get("excess_pv")

    forced_cur = forced_from_periods(current_periods, q) if current_periods is not None else None
    idle0 = IDLE_SELL if excess == "fed_to_grid" else IDLE_SAVE
    if work_mode == TOU_MODE and forced_cur:
        init_mode = {0: idle0, 1: CHARGE, 2: DISCHARGE}[forced_cur[0]]
    else:
        init_mode = idle0

    pp = c.period_penalty
    for _ in range(6):
        sol = _solve(q, c, E0, init_mode, None, tv, pp)
        sol = _replay(q, c, E0, _trim_edge_blocks(sol["modes"], c), tv, init_mode, pp)
        periods = modes_to_periods(q, sol["modes"])
        if len(periods) <= c.max_periods:
            break
        pp *= 2.5

    cost_msc = _simulate_msc(q, c, E0, tv)
    cost_cur = None
    sol_cur = None
    if work_mode == TOU_MODE and forced_cur is not None:
        sol_cur = _solve(q, c, E0, init_mode, forced_cur, tv, c.period_penalty)
        cost_cur = sol_cur["net_cost"]
    elif work_mode is not None:
        cost_cur = cost_msc

    advantage = cost_msc - sol["net_cost"]
    on_tou = work_mode == TOU_MODE
    temp = inp.get("temperature_c")
    cold = False
    if temp is not None:
        cold = float(temp) < c.cold_threshold_c + (c.cold_hysteresis_c if on_tou else 0.0)
    want_tou = len(periods) > 0 and (
        cold or advantage >= (c.tou_off_threshold if on_tou else c.tou_on_threshold))

    if want_tou:
        decision = "tou"
        if on_tou and cost_cur is not None:
            gain = cost_cur - sol["net_cost"]
            apply = gain >= c.replan_min_gain
            if cold and advantage < c.tou_off_threshold:
                reason = "kallt (%.1f °C) - håller TOU. " % float(temp)
            else:
                reason = ""
            reason +=  "ny plan %.2f kr bättre än nuvarande schema" % gain if apply else \
                "nuvarande schema är nästan lika bra (%.2f kr) - behåller" % gain
        else:
            apply = True
            reason = "byter till TOU, %.2f kr bättre än MSC" % advantage
            if cold and advantage < c.tou_on_threshold:
                reason = "kallt (%.1f °C) - byter till TOU (%.2f kr mot MSC)" % (float(temp), advantage)
    else:
        decision = "msc"
        apply = on_tou
        reason = "TOU-fördelen är bara %.2f kr - kör MSC" % advantage

    # när nuvarande schema behålls ska allt som visas beskriva DET schemat, inte förslaget
    kept = decision == "tou" and not apply and sol_cur is not None
    shown, shown_periods, proposed = sol, periods, None
    if kept:
        shown = _replay(q, c, E0, sol_cur["modes"], tv, init_mode, c.period_penalty)
        shown_periods = list(current_periods or [])
        proposed = periods

    # solrouting (select) ska följa det schema som faktiskt gäller
    if decision == "msc":
        route_modes = []
    elif apply or sol_cur is None:
        route_modes = sol["modes"]
    else:
        route_modes = sol_cur["modes"]

    quarters = []
    for qt, r in zip(q, shown["recs"]):
        quarters.append(dict(
            start=qt["dt"].isoformat(), price=qt["spot"], buy_price=round(qt["p"], 3), load_kw=round(qt["load"], 3), pv_kw=round(qt["pv"], 3),
            mode=MODE_NAME[r["mode"]], grid_in=round(r["grid_in"], 3), export=round(r["export"], 3),
            soc_pct=round(r["soc_pct"], 1), pv_kwh=round(qt["pv"] * H, 3), load_kwh=round(qt["load"] * H, 3), ev_window=1 if qt["ev"] > 0 else 0,
            batt_in=round(r["batt_in"], 3), batt_out=round(r["batt_out"], 3),
        ))
    text, now_state, next_text, next_at, ev_text = _explain(
        q, shown, c, now, decision, reason, advantage, cold, temp, kept,
        [x for x in (proposed or []) if x not in (current_periods or [])], ev_info)

    return dict(
        ok=True, cold=cold, explanation=text, ev_summary=ev_text, now_state=now_state, next_event=next_text, next_event_at=next_at,
        decision=decision, apply=apply, reason=reason,
        periods=shown_periods, proposed_periods=proposed, route_runs=_route_runs(q[:len(route_modes)], route_modes),
        cost_plan=round(sol["net_cost"], 2), cost_msc=round(cost_msc, 2),
        cost_current=None if cost_cur is None else round(cost_cur, 2),
        advantage_vs_msc=round(advantage, 2),
        horizon_start=q[0]["dt"].isoformat(), horizon_end=(q[-1]["dt"] + timedelta(minutes=15)).isoformat(),
        n_quarters=len(q), terminal_value=round(tv, 3), period_penalty_used=round(pp, 3),
        soc_end_pct=round(shown["recs"][-1]["soc_pct"], 1),
        quarters=quarters,
    )
