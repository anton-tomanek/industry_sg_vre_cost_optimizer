from domino.base_piece import BasePiece

from .models import InputModel, ModelSpec, OutputModel
from .utils.model_decider import MODEL_TYPES, TrainedModel, train_model

try:
    from common.piece_onedata import onedata_piece
except ModuleNotFoundError:
    from pieces.common.piece_onedata import onedata_piece


class PVOUTErrorCorrectionModelTrainPiece(BasePiece):
    @onedata_piece("PVOUTErrorCorrectionModelTrainPiece")
    def piece_function(self, input_data: InputModel):
        import csv
        import json
        import os
        import pickle
        import tempfile

        self.logger.info("Running PVOUTErrorCorrectionModelTrainPiece.")

        payload = input_data.payload_as_dict()
        if not payload:
            return OutputModel(
                message="PVOUTErrorCorrectionModelTrainPiece template executed (no-op).",
                artifacts={"input_payload": payload},
            )
        import numpy as np  # type: ignore

        model_type = str(payload.get("model_type", "linear_regression")).lower()
        model_params = payload.get("model_params") or {}
        setup = dict(payload.get("model_setup") or {})
        feature_columns = setup.get("feature_columns") or payload.get("feature_columns")
        target_column = setup.get("target_column") or payload.get("target_column", "PVOUT")

        if model_type not in MODEL_TYPES:
            raise ValueError(
                f"Unsupported model_type: {model_type}. "
                f"Available: {sorted(MODEL_TYPES)}"
            )

        if not feature_columns:
            raise ValueError("`payload['model_setup']['feature_columns']` is required.")

        # Normalize: train_model() consumes feature_columns / target_column from
        # `setup`, so propagate the resolved values back in case they were sourced
        # from the top-level payload fields (e.g. typed UI bindings from upstream
        # pieces). Without this the XGB error-correction models receive an empty
        # feature list and xgboost raises "0 feature is supplied".
        setup["feature_columns"] = list(feature_columns)
        setup["target_column"] = str(target_column)

        def _load_rows_from_csv(path: str) -> list[dict]:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                return [row for row in reader]

        # Input can be provided as csv path or inline tabular data.
        rows = None
        data_path = payload.get("data_path") or payload.get("csv_path")
        tabular_data = payload.get("tabular_data") or payload.get("dataframe")

        if data_path:
            rows = _load_rows_from_csv(data_path)
        elif isinstance(tabular_data, list):
            rows = tabular_data
        elif isinstance(tabular_data, dict):
            # Dict of columns -> list(values)
            keys = list(tabular_data.keys())
            n = len(tabular_data[keys[0]]) if keys else 0
            rows = [{k: tabular_data[k][i] for k in keys} for i in range(n)]
        else:
            raise ValueError(
                "Provide either `payload['data_path']`/`payload['csv_path']` or "
                "`payload['tabular_data']`."
            )

        if not rows:
            raise ValueError("No training rows were loaded.")

        # Keep a DataFrame for native models, and arrays for lightweight built-ins.
        try:
            import pandas as pd  # type: ignore
        except ImportError:
            pd = None

        full_df = None
        if pd is not None:
            full_df = pd.DataFrame(rows)
            # Coerce training columns to numeric where possible.
            numeric_columns = set(feature_columns + [target_column])
            pred_column = setup.get("pred_column")
            if pred_column:
                numeric_columns.add(pred_column)
            for col in numeric_columns:
                if col in full_df.columns:
                    full_df[col] = pd.to_numeric(full_df[col], errors="coerce")
            # Drop rows that can't participate in numeric model training.
            required_columns = [c for c in numeric_columns if c in full_df.columns]
            if required_columns:
                full_df = full_df.dropna(subset=required_columns)
        else:
            if model_type not in {"linear_regression", "ridge_regression"}:
                raise ValueError(
                    f"Model `{model_type}` requires pandas. "
                    "Install pandas or use `linear_regression` / `ridge_regression`."
                )

        # Workflow adapter: when an upstream baseline model checkpoint is provided,
        # generate the predicted-PVOUT column the XGB error-correction models need.
        baseline_model_path = payload.get("baseline_model_path")
        if baseline_model_path and full_df is not None:
            from .utils.baseline import load_baseline_model, predict_baseline

            pred_column = setup.get("pred_column") or "PVOUT_PRED"
            setup["pred_column"] = pred_column
            if pred_column not in full_df.columns:
                baseline_model = load_baseline_model(baseline_model_path)
                full_df = full_df.copy()
                full_df[pred_column] = predict_baseline(
                    baseline_model, full_df[feature_columns]
                )

        X_list = []
        y_list = []
        source_rows = rows if full_df is None else full_df.to_dict(orient="records")
        for row in source_rows:
            try:
                X_row = [float(row[col]) for col in feature_columns]
                y_val = float(row[target_column])
            except KeyError as e:
                raise ValueError(f"Missing required column in input data: {e}") from e
            X_list.append(X_row)
            y_list.append(y_val)
        if not X_list:
            raise ValueError("No valid numeric rows available after preprocessing.")
        X = np.asarray(X_list, dtype=float)
        y = np.asarray(y_list, dtype=float)

        trained_model = train_model(
            model_type=model_type,
            X=X,
            y=y,
            setup=setup,
            model_params=model_params,
            full_df=full_df if full_df is not None else rows,
        )

        train_metrics = {}
        if isinstance(trained_model, TrainedModel):
            y_pred = trained_model.predict(X)
            train_metrics = {
                "rmse": float(np.sqrt(np.mean((y - y_pred) ** 2))),
                "mae": float(np.mean(np.abs(y - y_pred))),
            }

        # Save checkpoint
        checkpoint_dir = payload.get("checkpoint_dir")
        if not checkpoint_dir:
            if getattr(self, "results_path", None):
                checkpoint_dir = str(self.results_path)
            else:
                checkpoint_dir = tempfile.gettempdir()
        os.makedirs(checkpoint_dir, exist_ok=True)
        checkpoint_path = os.path.join(
            checkpoint_dir, f"pvout_error_correction_{model_type}.pkl"
        )

        serializable_model = {}
        if isinstance(trained_model, TrainedModel):
            serializable_model = trained_model.to_dict()
            with open(checkpoint_path, "wb") as f:
                pickle.dump(serializable_model, f)
        else:
            # External model classes: persist the same envelope shape the
            # InferencePiece loader expects (`{"metadata": ..., "trained_model_object": ...}`).
            serializable_model = {
                "model_type": model_type,
                "feature_columns": feature_columns,
                "target_column": target_column,
                "params": model_params,
            }
            with open(checkpoint_path, "wb") as f:
                pickle.dump(
                    {
                        "metadata": serializable_model,
                        "trained_model_object": trained_model,
                    },
                    f,
                )

        resolved_data_path = payload.get("data_path") or payload.get("csv_path")

        # Persist the trained-model envelope as preprocessing_metadata.json so
        # downstream Inference can recover `feature_columns_used` / target /
        # params without re-loading the pickle. Mirrors what the price trainer
        # writes ([ElectricityPricePredictionModelTrainPiece/piece.py:156-158]).
        preprocessing_metadata = {
            "model_type": model_type,
            "feature_columns": list(feature_columns),
            "feature_columns_used": list(feature_columns),
            "target_column": str(target_column),
            "params": model_params,
        }
        preprocessing_metadata_path = os.path.join(
            checkpoint_dir, "preprocessing_metadata.json"
        )
        with open(preprocessing_metadata_path, "w", encoding="utf-8") as f:
            json.dump(preprocessing_metadata, f, indent=2)

        # Typed bundle for one-click upstream binding from InferencePiece.pvout_model.
        # `model_id="pvout"` matches the slot name, so forecast filenames stay clean
        # (`pvout.csv` instead of `pvout_correction.csv`).
        model_spec = ModelSpec(
            model_id="pvout",
            mode="pvout_correction",
            model_path=checkpoint_path,
            data_path=resolved_data_path,
            preprocessing_metadata_path=preprocessing_metadata_path,
            feature_columns=list(feature_columns),
            target_column=str(target_column),
            base_forecast_column=str(target_column),
        )

        return OutputModel(
            message="PVOUTErrorCorrectionModelTrainPiece executed.",
            model_path=checkpoint_path,
            feature_columns=list(feature_columns),
            target_column=str(target_column),
            data_path=resolved_data_path,
            preprocessing_metadata_path=preprocessing_metadata_path,
            baseline_model_path=baseline_model_path,
            model_spec=[model_spec],
            artifacts={
                "trained_model": serializable_model,
                "checkpoint_path": checkpoint_path,
                "preprocessing_metadata_path": preprocessing_metadata_path,
                "train_metrics": train_metrics,
            },
        )
