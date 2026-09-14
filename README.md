# UC3.2 Industry SG VRE Cost Optimizer

Domino pieces repository. Register `filipchrvala/industry_sg_vre_cost_optimizer` at version `0.1.46`, then import `UC3.2.customization`.

## Vstupy v Dominu

Nie je to JSON z lokálneho formulára. `UserInputPiece` v Dominu číta **dva súbory**:

- `onedata:///SCDI/UC3.2_COST_OPTIMIZER/inputs/load_and_prices.csv` — odber (datetime + výkon; ceny voliteľné)
- `onedata:///SCDI/UC3.2_COST_OPTIMIZER/inputs/scenario.yaml` — všetky polia z formulára (lokalita, CAPEX, krok batérie, MRK, …)

`prices_csv` ostáva prázdne. `user_input_summary_json` je **výstup** UserInputPiece (zdroj cien, pokrytie OKTE, časový rozsah), nie vstup z formulára.

Ako dostať to, čo ste vyplnili lokálne:

1. Spustite výpočet na webe, alebo aspoň uložte formulár.
2. Súbory sú v `.local_web/inputs/` (`load_and_prices.csv`, `scenario.yaml`). Hotový YAML je aj `.local_run/UserInputPiece/scenario_resolved.yaml`.
3. Tie dva súbory nahrajte na OneData cesty vyššie (priečinok `inputs` vytvorte, ak chýba).
4. V Dominu naštartujte importovaný workflow — `UserInputPiece` už na tieto cesty ukazuje.

Vzory sú v `examples/demo_site/`. Lokálny súbor môže byť `load.csv`, import hľadá **`load_and_prices.csv`**.

Ceny sú voliteľné. Ak `load_csv` nemá použiteľný `price_eur_per_kwh` a `prices_csv` je prázdne, UserInputPiece stiahne OKTE day-ahead ceny pre rovnaký dátumový rozsah ako odber.

## Lokálne cez web (bez Domina)

Celý výpočet ide spustiť na tomto počítači. Otvorí sa vstupná stránka, po dokončení dashboard.

V PowerShell, z koreňa repozitára:

```powershell
python -m pip install -r dependencies/requirements_0.txt
powershell -ExecutionPolicy Bypass -File scripts\start_local_web.ps1
```

Alebo dvojklik na `scripts\start_local_web.cmd`.

Stránka beží na [http://127.0.0.1:8088/](http://127.0.0.1:8088/). Nahrajte odber (CSV alebo Excel). Ceny sú voliteľné — ak ich súbor nemá, stiahne sa OKTE ISOT DAM. Iný port: `python scripts\start_local_web.py --port 8090`.

Bez prehliadača, rovnaký výpočet z príkazového riadka:

```powershell
python scripts\make_demo_inputs.py
python scripts\run_local.py --inputs examples\demo_site --out .local_run
```

Dashboard je potom v `.local_run\DashboardPiece\dashboard.html`.
