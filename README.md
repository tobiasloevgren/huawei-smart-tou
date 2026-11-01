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
      - pyscript.smart_tou_chart    # graf-data (~11 kB), ska inte in i databasen
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
  Slår på TOU vid >= 2 kr, går tillbaka till MSC under 1 kr (hysteres).
* **Schemat byts bara** om den nya planen är >= 1 kr bättre än det som redan ligger inne
  (det befintliga schemat utvärderas med samma modell). Det är det som stoppar fram-och-tillbaka.
* **Kallt väder:** under 5 °C (`sensor.temp_average`, 1 °C hysteres) väljs alltid TOU, aldrig MSC, så länge planen har några perioder. Gränsen ändras med `cold_threshold_c`.
* `excess_pv` sätts av en kvartstrigger enligt planen, bara i TOU-läge, och bara när värdet ska ändras.
* Körs vid uppstart, var 3:e timme, 14:05 och när Nordpool-sensorn får `tomorrow_valid = true`.
  Varje körning startar från verklig SoC, så "kvälls-/morgonkoll" ingår automatiskt.
* Schemat innehåller bara **resten av idag + imorgon**. Perioder för andra veckodagar rensas, så gamla priser aldrig återanvänds veckan efter.
* Solprognos: halva P10 + halva P50 (justera `pv_w10/pv_w50/pv_w90`).
* Förbrukning: medel per veckodag och timme, senaste 6 veckorna, från statistiken för `sensor.house_consumption_power_excl_ev`.

## Dashboard
`dashboard/smart_tou_dashboard.yaml` - kräver HACS-kortet **apexcharts-card**. Skapa en ny tom dashboard, öppna Rå konfigurationseditor och klistra in filen.
Innehåll: läge/nästa händelse, planen i klartext, schema (i växelriktaren vs senaste plan), plangraf (schema + pris + batteri %),
planerade energiflöden per kvart, faktiskt utfall per timme (dina energisensorer) och händelselogg.
Planens data kommer från `pyscript.smart_tou_chart` (listor per kvart + `t0`).
Sensorerna `battery_total_charge/discharge`, `total_solar_energy`, `power_meter_consumption/exported` ska vara energi i kWh (total_increasing).

## Gamla grafen (battery_charge / battery_on)
`dashboard/din_graf_med_nya_sensorer.yaml` är din ApexCharts-graf med nya sensorer: `sensor.smart_tou_battery_charge` (grön), `sensor.smart_tou_battery_on` (röd) och `sensor.smart_tou_price` (kvartspriser, ersätter `sensor.lowest_price`). De har samma attribut som förut: `hours: [{start, onOff|price}]`, en post per kvart för idag + imorgon.
Passerade kvartar minns skriptet själv (`pyscript.smart_tou_sched`), eftersom passerade perioder skrivs bort ur växelriktaren. I READONLY visar sensorerna *planen* (attributet `source` säger det); när du kör skarpt visar de det som ligger i växelriktaren.
Sensorerna är inte registrerade entiteter och försvinner vid omstart tills nästa körning (sker vid start).

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
