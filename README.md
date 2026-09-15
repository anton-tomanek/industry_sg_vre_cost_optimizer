# UC3.2 Industry SG VRE Cost Optimizer

Domino pieces repository for **UC3.2 SEED** (strategic investment / MRK cost optimizer).

- GitHub: register `filipchrvala/industry_sg_vre_cost_optimizer` at version `0.1.50`, then import `UC3.2.customization` (GHCR images).
- SPICE GitLab / Harbor: wait for CI image `harbor.testbed.spice-platform.eu/partner/uc3/industry_sg_vre_cost_optimizer:0.1.50-group0`, then import `UC3.2.spice.customization`.

After import, before **Create**:

1. **Settings** — workflow name may contain only letters, numbers and `_` (not `UC3.2`).
2. Shared storage **Local** (Read/Write) on local Docker Domino, so pieces can see each other's files.
3. Register **0.1.50** in that workspace first, or Import will complain about a missing repository version.

`UserInputPiece` reads two OneData files (create the `inputs` folder if it is missing):

- `onedata:///SCDI/UC3.2_COST_OPTIMIZER/inputs/load_and_prices.csv` — load (datetime + power; prices optional)
- `onedata:///SCDI/UC3.2_COST_OPTIMIZER/inputs/scenario.yaml` — site, CAPEX, battery step, MRK and the rest of the design settings

Run outputs go to `onedata:///SCDI/UC3.2_COST_OPTIMIZER/outputs/<run_id>/<piece>/`. Set the `onedata_output_dir` secret to an empty string only if you want to keep results on local Domino storage.

`prices_csv` stays empty. If the load file has no usable `price_eur_per_kwh`, UserInputPiece pulls OKTE day-ahead prices for the same datetime range.

`user_input_summary_json` is an **output** (price source, OKTE coverage, horizon), not a form dump.

## GitLab CI / Harbor

Pipeline on `main` builds and publishes the Domino piece image to Harbor when `config.toml`, `pieces/`, `dependencies/`, or `.gitlab-ci.yml` change.

Set **Settings → CI/CD → Variables** (mask secrets). These were already configured for the previous 0.1.42 Harbor push:

| Variable | Description |
|----------|-------------|
| `CI_PUSH_TOKEN` | Project access token (`write_repository` + `api`) |
| `CI_RELEASE_TOKEN` | Same token (`api`) |
| `CONTAINER_REGISTRY` | `harbor.testbed.spice-platform.eu` |
| `CONTAINER_REGISTRY_USERNAME` | e.g. `partner` |
| `CONTAINER_REGISTRY_PASSWORD` | Harbor password (SPICE vault) |

`config.toml` `REGISTRY_NAME` must stay `harbor.testbed.spice-platform.eu/partner/uc3` so Domino organizes images into Harbor, not a personal GitHub namespace.
