# UC3.2 Industry SG VRE Cost Optimizer

Domino pieces repository. Register `filipchrvala/industry_sg_vre_cost_optimizer` at version `0.1.47`, then import `UC3.2.customization`.

`UserInputPiece` reads two OneData files (create the `inputs` folder if it is missing):

- `onedata:///SCDI/UC3.2_COST_OPTIMIZER/inputs/load_and_prices.csv` — load (datetime + power; prices optional)
- `onedata:///SCDI/UC3.2_COST_OPTIMIZER/inputs/scenario.yaml` — site, CAPEX, battery step, MRK and the rest of the design settings

`prices_csv` stays empty. If the load file has no usable `price_eur_per_kwh`, UserInputPiece pulls OKTE day-ahead prices for the same datetime range.

`user_input_summary_json` is an **output** (price source, OKTE coverage, horizon), not a form dump.
