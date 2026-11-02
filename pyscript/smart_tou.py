# /config/pyscript/smart_tou.py
"""
Smart TOU för Huawei LUNA2000 - pyscript-lager.

All planering ligger i /config/pyscript/modules/smart_tou_planner.py (ren Python).
Den här filen läser data från HA, anropar planeraren, och (om READONLY = False)
skriver TOU-perioder, arbetsläge och "excess PV"-val.

Starta med READONLY = True och läs pyscript.smart_tou_plan + loggen.
"""
import homeassistant.util.dt as dt_util

# ============================== KONFIGURATION ==============================
READONLY = True          # True = planera och visa, men skriv ALDRIG till växelriktaren

PLANNER_PATH = "/config/pyscript/modules/smart_tou_planner.py"
PLAN_ENTITY = "pyscript.smart_tou_plan"
LEGACY_CHART_ENTITY = "pyscript.smart_tou_chart"   # gammal graf-data, används inte längre (städas bort vid laddning)
SCHED_ENTITY = "pyscript.smart_tou_sched"   # minns schemat för passerade kvartar (till grafen)
# Drop-in-ersättare för gamla battery_charge / battery_on / lowest_price (attributet "hours")
COMPAT_CHARGE = "sensor.smart_tou_battery_charge"
COMPAT_ON = "sensor.smart_tou_battery_on"
COMPAT_PRICE = "sensor.smart_tou_price"
LOG_ENTITY = "pyscript.smart_tou_log"     # ändringslogg: syns i Logbok, sparar senaste 30 händelserna

NORDPOOL = "sensor.nordpool_kwh_se3_sek_3_10_025"
SOC = "sensor.battery_state_of_capacity"
WORK_MODE = "select.battery_working_mode"
TOU_CURRENT = "sensor.batteries_tou_charging_and_discharging_periods"
EXCESS_PV_SELECT = "select.battery_excess_pv_energy_use_in_tou"
TEMP_SENSOR = "sensor.temp_average"   # KONTROLLERA: utomhustemperatur (fanns i ditt gamla flöde)
HOUSE_LOAD = "sensor.house_consumption_power_excl_ev"   # exkl. laddbox
SOLCAST_TODAY = "sensor.solcast_pv_forecast_forecast_today"
SOLCAST_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"
# Bil (Tesla + laddbox). Laddningen sköter du själv; skriptet läser bara av när den väntas ske.
EV_LEVEL = "sensor.tesla_battery_level"
EV_LIMIT = "sensor.tesla_charge_limit"
EV_PLUGGED = "binary_sensor.tesla_plugged_in"
EV_START = "sensor.tesla_scheduled_charging_start_time"
EV_MODE = "sensor.zag019136_charger_mode"    # disconnected | connected_requesting | connected_charging | connected_finished
EV_POWER = "sensor.zag019136_charge_power"   # W; används för att skilja riktig laddning från liten start-laddning vid inkoppling
EV_CAPACITY_KWH = 78.0
EV_ACTIVE_W = 1000                           # under detta räknas connected_charging inte som laddning
EV_WAIT_MIN = 5                              # connected_requesting med schematid längre bort än så = väntar på schema, inte laddar
EV_MEMORY = {"sig": None}                    # bilens situation som senaste plan byggdes på
EV_STOP_GRACE_MIN = 15                       # väntan innan ett OSÄKERT laddstopp räknas som riktigt (t.ex. charging med låg effekt)
EV_START_GRACE_MIN = 10                      # schematiden har passerat men bilen har inte hunnit börja: räkna den som laddande så länge
BUSY = {"on": False}

HUAWEI_DEVICE_ID = "f5446e3ab7205dcbb8a81d6234e4d7ac"

TOU_MODE = "time_of_use_luna2000"
MSC_MODE = "maximise_self_consumption"

LOAD_HISTORY_DAYS = 42   # veckor av timmedelvärden för förbrukningsprofilen

# Överstyr planerarens standardvärden här (se DEFAULTS i smart_tou_planner.py)
PLANNER_PARAMS = dict(
    capacity_kwh=15.0,        # KONTROLLERA: nyttig batterikapacitet
    max_charge_kw=5.0,        # KONTROLLERA: max laddeffekt (nät+sol)
    max_discharge_kw=5.0,     # KONTROLLERA: max urladdningseffekt
    min_soc_pct=10.0,
    round_trip_eff=0.90,
    wear_cost=0.30,           # SEK/kWh batteriet levererar
    grid_cost=0.90,           # KONTROLLERA: kr/kWh som tillkommer på köpt el (nät, skatt, moms) utöver Nordpool
    export_factor=0.80,       # såld el: sensorpris/1,25 (momsen bort) ...
    export_bonus=0.02,        # ... plus 2 öre per såld kWh
    pv_w10=0.5, pv_w50=0.5, pv_w90=0.0,   # försiktig solprognos: halva P10 + halva P50
    tou_on_threshold=2.0,     # kr: TOU måste vara så mycket bättre än MSC för att slå på
    tou_off_threshold=0.0,    # kr: redan i TOU lämnas TOU bara om det inte är bättre än MSC, eller planen saknar perioder
    cold_threshold_c=5.0,     # under detta: alltid TOU, aldrig MSC (värmepump, lite sol)
    cold_hysteresis_c=1.0,
    replan_min_gain=1.0,      # kr: ny plan måste vara så mycket bättre för att byta schema
)

state.persist(PLAN_ENTITY, default_value="ingen plan än", default_attributes={})
try:
    state.delete(LEGACY_CHART_ENTITY)   # städa bort den gamla graf-entiteten om den finns kvar
except Exception:
    pass
state.persist(SCHED_ENTITY, default_value="0", default_attributes={"q": {}})
state.persist(LOG_ENTITY, default_value="inga händelser än", default_attributes={"history": []})


# ============================ HJÄLPFUNKTIONER ==============================
@pyscript_executor
def _planner_call(path, func, args):
    # Körs som vanlig (snabb) Python i en trådpool, inte i pyscripts tolk.
    import importlib.util
    spec = importlib.util.spec_from_file_location("smart_tou_planner", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, func)(*args)


@pyscript_executor
def _fetch_hourly_means(hass_obj, entity_id, days):
    from datetime import datetime, timedelta, timezone
    from homeassistant.components.recorder.statistics import statistics_during_period
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    res = statistics_during_period(hass_obj, start, end, {entity_id}, "hour", None, {"mean"})
    rows = []
    for r in res.get(entity_id, []):
        m = r.get("mean")
        s = r.get("start")
        if m is None or s is None:
            continue
        ts = s.timestamp() if hasattr(s, "timestamp") else float(s)
        rows.append((ts, float(m)))
    return rows


def _num(entity):
    try:
        v = state.get(entity)
        if v in (None, "", "unknown", "unavailable"):
            return None
        return float(v)
    except Exception:
        return None


def _txt(entity):
    try:
        v = state.get(entity)
        return None if v in ("", "unknown", "unavailable") else v
    except Exception:
        return None


def _read_ev(now):
    """
    Bilen är bara ett tidsfönster där husbatteriet är låst (2 kW in, 0 ut) - dess kWh ingår inte i planen.
    remaining_kwh används bara för att uppskatta hur länge den laddar.
      pågår : connected_charging med effekt >= EV_ACTIVE_W, eller connected_requesting (väntar på ström, t.ex. effektvakt)
              - men requesting räknas som "väntar på schema" om schematiden ligger längre fram.
      planerad: inkopplad, schematid i framtiden.
    """
    mode = _txt(EV_MODE)
    power = _num(EV_POWER) or 0.0
    info = dict(plugged=(_txt(EV_PLUGGED) == "on" and mode != "disconnected"), level=_num(EV_LEVEL), limit=_num(EV_LIMIT),
                active=False, waiting=False, start=None, remaining_kwh=0.0, mode=mode)
    if not info["plugged"] or info["level"] is None or info["limit"] is None:
        return info
    rem = (info["limit"] - info["level"]) / 100.0 * EV_CAPACITY_KWH
    if rem < 0.5:
        return info
    info["remaining_kwh"] = rem
    st = _txt(EV_START)
    d = dt_util.parse_datetime(st) if st else None
    future = d is not None and (d - now).total_seconds() > EV_WAIT_MIN * 60
    just_started = d is not None and 0 <= (now - d).total_seconds() <= EV_START_GRACE_MIN * 60
    if mode is None:                                   # ingen laddstatus: fall tillbaka på effekten
        charging, requesting = power >= EV_ACTIVE_W, False
    else:
        charging = mode == "connected_charging" and power >= EV_ACTIVE_W
        requesting = mode == "connected_requesting"
    if charging or (requesting and not future):
        info["active"] = True
        info["waiting"] = requesting and not charging
    elif just_started:
        # connected_finished betyder både "klar" och "väntar på schemat". Schematiden har just passerat
        # och nivån är under målet: bilen håller på att börja, räkna den som laddande (annars blinkar fönstret bort)
        info["active"] = True
        info["waiting"] = True
    elif future:
        info["start"] = d.isoformat()
    return info


def _read_prices():
    out = []
    a = state.getattr(NORDPOOL)
    for key in ("raw_today", "raw_tomorrow"):
        v = a.get(key)
        if isinstance(v, list):
            out.extend(v)
    return out


def _read_pv():
    out = []
    for ent in (SOLCAST_TODAY, SOLCAST_TOMORROW):
        try:
            out.extend(state.getattr(ent).get("detailedForecast") or [])
        except Exception as e:
            log.warning(f"[SMART_TOU] Solcast {ent} saknas: {e}")
    return out


def _read_periods():
    items = []
    try:
        for k, v in state.getattr(TOU_CURRENT).items():
            if str(k).startswith("Period"):
                try:
                    n = int(str(k).split()[-1])
                except Exception:
                    n = 0
                items.append((n, str(v)))
    except Exception as e:
        log.warning(f"[SMART_TOU] Kunde inte läsa nuvarande TOU: {e}")
        return None
    items.sort()
    return [v for _, v in items]


def _load_profile(tzname):
    scale = 1.0
    try:
        if str(state.getattr(HOUSE_LOAD).get("unit_of_measurement", "")) == "W":
            scale = 0.001
    except Exception:
        pass
    try:
        rows = _fetch_hourly_means(hass, HOUSE_LOAD, LOAD_HISTORY_DAYS)
    except Exception as e:
        log.warning(f"[SMART_TOU] Ingen förbrukningshistorik ({e}) - använder schablon")
        return {}, "schablon (fel vid läsning)"
    if not rows:
        log.warning(f"[SMART_TOU] {HOUSE_LOAD} saknar långtidsstatistik - använder schablon. "
                    "Sensorn behöver state_class: measurement.")
        return {}, "schablon (ingen statistik)"
    prof = _planner_call(PLANNER_PATH, "build_load_profile", [rows, tzname, scale])
    return prof, f"statistik ({prof.get('_samples', 0)} timmar)"


def _event(msg):
    """Skriver en rad i HA-loggen, Logbok och pyscript.smart_tou_log (senaste 30)."""
    log.info(f"[SMART_TOU] {msg}")
    stamp = dt_util.now().strftime("%Y-%m-%d %H:%M")
    try:
        hist = list(state.getattr(LOG_ENTITY).get("history") or [])
    except Exception:
        hist = []
    hist = ([f"{stamp}  {msg}"] + hist)[:30]
    state.set(LOG_ENTITY, value=msg[:250], new_attributes=dict(
        friendly_name="Smart TOU logg", icon="mdi:clipboard-text-clock", history=hist))
    try:
        logbook.log(name="Smart TOU", message=msg, entity_id=LOG_ENTITY)   # direkt syntax: "name" krockar med service.call
    except Exception:
        pass


def _set_select(entity, option, readonly, why):
    label = {WORK_MODE: "Arbetsläge", EXCESS_PV_SELECT: "Solöverskott"}.get(entity, entity)
    before = _txt(entity)
    if readonly:
        _event(f"[READONLY] Skulle ändra {label}: {before} -> {option} ({why})")
        return False
    try:
        service.call("select", "select_option", entity_id=entity, option=option)
        _event(f"ÄNDRADE {label}: {before} -> {option} ({why})")
        return True
    except Exception as e:
        _event(f"FEL: kunde inte ändra {label} till {option}: {e}")
        return False


def _apply(plan, work_mode, readonly):
    if plan["decision"] == "tou":
        text = "\n".join(plan["periods"])
        short = " | ".join(plan["periods"])
        if readonly:
            _event(f"[READONLY] Skulle skriva TOU-schema ({plan['reason']}): {short}")
            if work_mode != TOU_MODE:
                _set_select(WORK_MODE, TOU_MODE, True, "plan: TOU")
            return False
        try:
            service.call("huawei_solar", "set_tou_periods", device_id=HUAWEI_DEVICE_ID, periods=text)
            _event(f"SKREV TOU-schema ({plan['reason']}): {short}")
        except Exception as e:
            _event(f"FEL: set_tou_periods misslyckades: {e}")
            return False
        if work_mode != TOU_MODE:
            return _set_select(WORK_MODE, TOU_MODE, readonly, "plan: TOU")
        return True
    # MSC
    if work_mode == MSC_MODE:
        return False
    return _set_select(WORK_MODE, MSC_MODE, readonly, "plan: MSC")


def _apply_route(readonly):
    """Sätter excess-PV-valet för nuvarande kvart enligt planen (bara i TOU-läge)."""
    if _txt(WORK_MODE) != TOU_MODE:
        return
    try:
        a = state.getattr(PLAN_ENTITY)
    except Exception:
        return
    now = dt_util.now()
    gen = dt_util.parse_datetime(str(a.get("generated", "")))
    if gen is None or (now - gen).total_seconds() > 30 * 3600:
        return  # gammal plan
    target = None
    for s, e, r in a.get("route_runs") or []:
        ds, de = dt_util.parse_datetime(s), dt_util.parse_datetime(e)
        if ds and de and ds <= now < de:
            target = r
            break
    if target is None or _txt(EXCESS_PV_SELECT) == target:
        return
    _set_select(EXCESS_PV_SELECT, target, readonly, "solöverskott enligt plan")


def _publish(plan, work_mode, applied, readonly, prof_src, now):
    if not plan.get("ok"):
        state.set(PLAN_ENTITY, value=str(plan.get("reason", "fel"))[:250],
                  new_attributes=dict(generated=now.isoformat(), friendly_name="Smart TOU plan"))
        return
    short = (f"{plan['decision'].upper()} | {len(plan['periods'])} perioder | "
             f"{plan['advantage_vs_msc']:.1f} kr bättre än MSC | "
             f"{'ÄNDRAD' if applied else 'oförändrad'}{' (READONLY)' if readonly else ''}")
    state.set(PLAN_ENTITY, value=short[:250], new_attributes=dict(
        friendly_name="Smart TOU plan", icon="mdi:battery-clock",
        generated=now.isoformat(), readonly=readonly, decision=plan["decision"],
        would_apply=plan["apply"], applied=applied, reason=plan["reason"],
        work_mode=work_mode, periods=plan["periods"], route_runs=plan["route_runs"],
        cost_plan=plan["cost_plan"], cost_msc=plan["cost_msc"], cost_current=plan["cost_current"],
        advantage_vs_msc=plan["advantage_vs_msc"], horizon_end=plan["horizon_end"],
        soc_end_pct=plan["soc_end_pct"], load_source=prof_src, cold=plan["cold"],
        explanation=plan["explanation"], now_state=plan["now_state"], ev=plan.get("ev_summary"),
        next_event=plan["next_event"], next_event_at=plan["next_event_at"],
    ))


def _publish_compat(plan, work_mode, applied, readonly, periods_now, prices, tzname):
    """Skapar sensorer med attributet hours=[{start, onOff}] så den gamla ApexCharts-grafen kan återanvändas."""
    if not plan.get("ok"):
        eff = periods_now if (work_mode == TOU_MODE and periods_now) else []
        src = "växelriktaren"
    elif readonly:
        eff = plan["periods"] if plan["decision"] == "tou" else []
        src = "plan (READONLY)"
    elif applied:
        eff = plan["periods"] if plan["decision"] == "tou" else []
        src = "växelriktaren"
    else:
        eff = periods_now if (work_mode == TOU_MODE and periods_now) else []
        src = "växelriktaren"
    try:
        prev = dict(state.getattr(SCHED_ENTITY).get("q") or {})
    except Exception:
        prev = {}
    res = _planner_call(PLANNER_PATH, "schedule_hours", [eff, dt_util.now(), tzname, prev])
    state.set(SCHED_ENTITY, value=str(len(res["map"])), new_attributes=dict(q=res["map"]))
    cur = res["current"]
    state.set(COMPAT_CHARGE, value="on" if cur["charge"] else "off", new_attributes=dict(
        friendly_name="Smart TOU battery charge", icon="mdi:battery-charging", source=src,
        hours=[{"start": h["start"], "onOff": h["charge"]} for h in res["hours"]]))
    state.set(COMPAT_ON, value="on" if cur["on"] else "off", new_attributes=dict(
        friendly_name="Smart TOU battery on", icon="mdi:battery-arrow-down", source=src,
        hours=[{"start": h["start"], "onOff": h["on"]} for h in res["hours"]]))
    rows = []
    for e in prices:
        try:
            st = e.get("start")
            rows.append({"start": st.isoformat() if hasattr(st, "isoformat") else str(st), "price": round(float(e.get("value")), 3)})
        except Exception:
            continue
    rows.sort(key=lambda r: r["start"])
    state.set(COMPAT_PRICE, value=str(_num(NORDPOOL)), new_attributes=dict(
        friendly_name="Smart TOU price", icon="mdi:cash", unit_of_measurement="SEK/kWh", hours=rows))


# ================================ TRIGGERS =================================
def _ev_sig(ev):
    """Det som får en omräkning att behövas: inkopplad, laddar/väntar, vald starttid, laddgräns.
    Nivån och effekten ingår INTE (effektvakten växlar charging/requesting och effekt 0 W utan att något ändrats)."""
    return (bool(ev.get("plugged")), bool(ev.get("active")), (ev.get("start") or "")[:16],
            int(round(ev.get("limit") or 0)))


def _replan(readonly):
    if BUSY["on"]:
        return
    BUSY["on"] = True
    try:
        _replan_inner(readonly)
    finally:
        BUSY["on"] = False


def _replan_inner(readonly):
    now = dt_util.now()
    tzname = str(dt_util.DEFAULT_TIME_ZONE)

    soc = _num(SOC)
    if soc is None:
        log.warning("[SMART_TOU] SoC saknas - avbryter")
        return
    try:
        prices = _read_prices()
    except Exception as e:
        log.warning(f"[SMART_TOU] Nordpool saknas: {e}")
        return

    ev = _read_ev(now)
    EV_MEMORY["sig"] = _ev_sig(ev)      # bevakningen jämför mot det planen byggdes på
    work_mode = _txt(WORK_MODE)
    periods_now = _read_periods()
    prof, prof_src = _load_profile(tzname)
    params = dict(PLANNER_PARAMS)
    params["tz"] = tzname

    plan = _planner_call(PLANNER_PATH, "make_plan", [dict(
        prices=prices, pv=_read_pv(), load_profile=prof, now=now, soc_pct=soc,
        work_mode=work_mode, excess_pv=_txt(EXCESS_PV_SELECT),
        current_periods=periods_now, params=params,
        temperature_c=_num(TEMP_SENSOR), ev=ev,
    )])

    applied = False
    if plan.get("ok"):
        log.info(f"[SMART_TOU] {plan['decision'].upper()}: {plan['reason']} "
                 f"(plan {plan['cost_plan']} kr, MSC {plan['cost_msc']} kr, nuvarande {plan['cost_current']} kr, "
                 f"förbrukning: {prof_src})")
        if plan["apply"]:
            applied = _apply(plan, work_mode, readonly)
    else:
        log.warning(f"[SMART_TOU] {plan.get('reason')}")

    _publish(plan, work_mode, applied, readonly, prof_src, now)
    try:
        _publish_compat(plan, work_mode, applied, readonly, periods_now, prices, tzname)
    except Exception as e:
        log.warning(f"[SMART_TOU] Kunde inte skapa graf-sensorerna: {e}")
    if plan.get("ok"):
        _apply_route(readonly)


@service
@time_trigger("startup", "cron(2 */3 * * *)", "cron(5 14 * * *)")
@state_trigger(f"{NORDPOOL}.tomorrow_valid == True")
@task_unique("smart_tou_plan", kill_me=True)
def smart_tou_replan(trigger_type=None, dry_run=False, **kwargs):
    """Planera om. Kan även köras manuellt: pyscript.smart_tou_replan (dry_run: true)."""
    if trigger_type == "state":
        task.sleep(20)   # låt raw_tomorrow hinna uppdateras
    _replan(READONLY or bool(dry_run))


@state_trigger(EV_PLUGGED, EV_MODE, EV_START, EV_LIMIT, EV_POWER)
@task_unique("smart_tou_ev", kill_me=True)
def smart_tou_ev_watch(**kwargs):
    """Räknar bara om när bilens situation ändrats på riktigt - inte när effektvakten pausar laddningen."""
    old = EV_MEMORY.get("sig")
    ev = _read_ev(dt_util.now())
    new = _ev_sig(ev)
    if new == old:
        return
    clear_stop = ev.get("mode") in ("connected_finished", "disconnected")   # tydligt besked från laddaren
    if old is not None and old[1] and not new[1] and not clear_stop:
        # laddningen verkar ha slutat men beskedet är oklart (t.ex. charging med låg effekt): vänta, den kan komma tillbaka
        task.sleep(EV_STOP_GRACE_MIN * 60)
        new = _ev_sig(_read_ev(dt_util.now()))
        if new == EV_MEMORY.get("sig"):
            log.info("[SMART_TOU] Laddningen var bara pausad - ingen omräkning")
            return
    else:
        task.sleep(20)   # låt bilens sensorer hinna landa
    _replan(READONLY)


@time_trigger("cron(*/15 * * * *)")
def smart_tou_route_tick():
    _apply_route(READONLY)
