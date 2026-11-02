# Smart TOU för Huawei LUNA2000 (pyscript)

Ersätter Node-RED-flödena. Planerar batteriet över "resten av idag + imorgon" utifrån
kvartspriser, Solcast, SoC och en förbrukningsprofil från HA:s långtidsstatistik.

## Filer
```
/config/pyscript/smart_tou.py                      <- koppling mot HA (triggers, läsa, skriva)
/config/pyscript/modules/smart_tou_planner.py      <- själva planeraren (ren Python)
tests/test_planner.py                              <- enhetstester (pytest, körs på din dator)
```

## Installation
1. Kopiera filerna enligt ovan (mappen `modules` ska ligga i `pyscript/`).
2. `configuration.yaml`:
```yaml
pyscript:
  allow_all_imports: true
  hass_is_global: true
recorder:
  exclude:
    entities:
      - pyscript.smart_tou_plan     # attributen ändras ofta, onödigt att spara
      - pyscript.smart_tou_sched
      - sensor.smart_tou_battery_charge
      - sensor.smart_tou_battery_on
      - sensor.smart_tou_price
```
3. Öppna `smart_tou.py` och kontrollera `PLANNER_PARAMS` (kapacitet, effekt, exportfaktor).
4. Ladda om pyscript. `READONLY = True` är standard: inget skrivs till växelriktaren.

## Så fungerar det
Varje kvart väljer planeraren ett av fyra lägen (dynamisk programmering över batterinivån):

| Läge | Vad som skrivs |
|---|---|
| CHARGE | TOU-period `+` (sol + nät, full effekt) |
| DISCHARGE | TOU-period `-` (batteriet täcker husets underskott, ingen export) |
| IDLE_SAVE | ingen period, `excess_pv = charge` (solöverskott laddar batteriet) |
| IDLE_SELL | ingen period, `excess_pv = fed_to_grid` (solöverskott exporteras) |

* Sammanhängande kvartar blir **ett block** (kan börja 02:45). Straff per ny period håller blocken långa; max 14 perioder.
* **TOU eller MSC** avgörs av beräknad besparing i kronor mot MSC, inte av en fast prisskillnad.
  Slår på TOU vid >= 2 kr. Står du redan i TOU ligger det kvar så länge planen har perioder och TOU inte är sämre än MSC (`tou_off_threshold = 0`), så att sparad energi inte tas av MSC före dyra timmar.
* **Schemat byts bara** om den nya planen är >= 1 kr bättre än det som redan ligger inne
  (det befintliga schemat utvärderas med samma modell). Det är det som stoppar fram-och-tillbaka.
* **Kallt väder:** under 5 °C (`sensor.temp_average`, 1 °C hysteres) väljs alltid TOU, aldrig MSC, så länge planen har några perioder. Gränsen ändras med `cold_threshold_c`.
* `excess_pv` sätts av en kvartstrigger enligt planen, bara i TOU-läge, och bara när värdet ska ändras.
* Körs vid uppstart, var 3:e timme, 14:05 och när Nordpool-sensorn får `tomorrow_valid = true`.
  Varje körning startar från verklig SoC, så "kvälls-/morgonkoll" ingår automatiskt.
* Schemat innehåller bara **resten av idag + imorgon**. Perioder för andra veckodagar rensas, så gamla priser aldrig återanvänds veckan efter.
* Solprognos: halva P10 + halva P50 (justera `pv_w10/pv_w50/pv_w90`).
* Förbrukning: medel per veckodag och timme, senaste 6 veckorna, från statistiken för `sensor.house_consumption_power_excl_ev`.

## Bil (Tesla)
Bilen är **bara ett tidsfönster** där husbatteriet är låst: max 2 kW in och 0 kW ut (`ev_battery_charge_kw`, `ev_battery_discharge_kw`). Bilens kWh ingår **inte** i planen eller kostnaden; de används bara för att uppskatta hur länge fönstret varar (kWh kvar / 6 kW). Planen lägger urladdningen före eller efter fönstret, och laddar husbatteriet i förväg om det lönar sig.
Du laddar själv; skriptet styr inte bilen eller laddboxen, och det är inte skriptet som spärrar batteriet till 2/0 kW (det antas något annat hos dig göra).

Så avgörs om laddning pågår eller är planerad (`sensor.zag019136_charger_mode` + `sensor.zag019136_charge_power`):
| Läge | Tolkas som |
|---|---|
| `connected_charging` och effekt >= 1000 W | laddar |
| `connected_charging` men effekt < 1000 W | liten start-laddning vid inkoppling, räknas inte |
| `connected_requesting` | laddar (väntar på ström, t.ex. effektvakt) - om inte schematiden ligger >5 min fram, då väntar bilen på sitt schema |
| `connected_finished`, nivå under mål, schematid i framtiden | **väntar på schemat**: laddning planerad från `sensor.tesla_scheduled_charging_start_time` |
| `connected_finished`, schematiden passerade för <10 min sedan | bilen håller på att börja: räknas som laddande |
| `connected_finished`, nivå = mål (eller ingen schematid) | **klar** - ingen laddning |
| `disconnected` / inte inkopplad / nivå >= mål | ingen laddning |
Saknas laddstatus-sensorn används bara effekten. Nivå och mål kommer från `sensor.tesla_battery_level` och `sensor.tesla_charge_limit` (78 kWh).

**Omräkning för bilen** sker bara när situationen ändrats på riktigt: bilen kopplas in/ur, laddning startar, schematiden eller laddgränsen ändras, eller laddningen **har tagit slut**. Nivån och effekten i sig utlöser ingenting.
* Effektvaktens pausar (`connected_charging` <-> `connected_requesting`, effekt 0 W) räknas som pågående laddning och ger ingen omräkning.
* Ett laddstopp godkänns först efter 15 minuters väntan (`EV_STOP_GRACE_MIN`). Är laddningen tillbaka då sker ingen omräkning.
* Återstående tid uppdateras ändå var 3:e timme, då nivån räknas om.

## Dashboard
`dashboard/smart_tou_dashboard.yaml` - kräver HACS-korten **apexcharts-card** och **power-flow-card-plus**. Skapa en ny tom dashboard, öppna Rå konfigurationseditor och klistra in filen.
Innehåll: läge/nästa händelse/bil, planen i klartext, schema (växelriktaren vs plan, visas bara dubbelt när de skiljer sig), din ApexCharts-graf (pris, förbrukning, schema grön/röd, SOC), power-flow-card-plus och händelselogg.

Grafen läser tre sensorer som skriptet skapar, med attributet `hours: [{start, onOff|price}]`, en post per kvart för idag + imorgon:
`sensor.smart_tou_battery_charge` (grön), `sensor.smart_tou_battery_on` (röd) och `sensor.smart_tou_price` (kvartspriser, ersätter `sensor.lowest_price`).
I READONLY visar sensorerna *planen* (attributet `source` säger det); när du kör skarpt visar de det som ligger i växelriktaren.
Passerade kvartar minns skriptet själv (`pyscript.smart_tou_sched`), eftersom passerade perioder skrivs bort ur växelriktaren.
Sensorerna är inte registrerade entiteter och försvinner vid omstart tills nästa körning (sker vid start).
Förbrukningsserien (`sensor.hourly_power_consumption`) är din egen sensor och måste finnas kvar om du tar bort Node-RED.

## Logg
Varje gång något ändras (schema, arbetsläge, solöverskott) skrivs en rad till HA-loggen, **Logbok** (Smart TOU) och `pyscript.smart_tou_log` (attributet `history` = senaste 30). I READONLY står det `[READONLY] Skulle ...`, annars `SKREV ...` / `ÄNDRADE ...` / `FEL ...`.

## Testa (gör i den här ordningen)
1. `READONLY = True`. Kör `pyscript.smart_tou_replan` från Utvecklarverktyg > Åtgärder.
2. Titta på `pyscript.smart_tou_plan` (attribut: `periods`, `route_runs`, `reason`, `load_source`) och loggen.
   `load_source` ska säga "statistik (… timmar)". Säger den "schablon" saknar sensorn långtidsstatistik.
3. Jämför `periods` med vad du själv hade valt. Låt det gå en dag eller två.
4. Sätt `READONLY = False`, ladda om. Ta en HA-backup först.

## Att verifiera (antaganden jag inte kan se härifrån)
* **Batteri:** 15 kWh nyttig kapacitet, 5 kW laddning/urladdning.
* **Periodslut:** blocket som slutar vid midnatt skrivs `23:59`, och ett block som slutar där nästa börjar kortas med en minut (`04:59`). Antagande om hur Huawei validerar överlapp.
* **excess_pv:** jag antar att valet gäller utanför laddperioder, och att solöverskott exporteras under urladdningsperioder.
* **Priser:** köpt el = Nordpool-sensorn + `grid_cost` (0,90 kr: nät, skatt m.m. - kontrollera om beloppet gäller inkl. moms). Såld el = sensorpris × `export_factor` (0,80 = momsen bort) + `export_bonus` (0,02 kr).
* **Förbrukningssensorn** ska inte räkna med batteriladdning från nät, annars blir prognosen för hög.
* `statistics_during_period` och pyscript-dekoratorerna är testade mot en attrapp, inte mot riktiga HA. Läs loggen vid första körningen.

## Ännu inte med
Temperaturberoende förbrukning, kalibrering mot Solcasts historiska fel, elbilsladdning, effekttariff.
