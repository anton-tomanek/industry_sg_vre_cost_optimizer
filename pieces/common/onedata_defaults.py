"""OneData defaults for the UC3.2 Cost Optimizer pieces.

No credentials are built in: set ``onedata_onezone_host``, ``onedata_token`` and
``onedata_output_dir`` as Domino repository secrets. With the secrets empty,
results stay on local Domino shared storage.
"""

DEFAULT_ONEZONE_HOST = "data.spice-platform.eu"
DEFAULT_INPUT_DIR = "onedata:///SCDI/UC3.2_COST_OPTIMIZER/inputs"
DEFAULT_OUTPUT_DIR = ""
DEFAULT_ONEDATA_TOKEN = ""
