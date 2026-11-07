import numpy as np
import pandas as pd
import pytest

from assistant.config import Config, load_config
from assistant.db import connect


@pytest.fixture
def cfg() -> Config:
    return load_config()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "journal.db")
    yield c
    c.close()


def make_bars(closes, start="2024-01-01", opens=None, volume=1_000_000):
    idx = pd.bdate_range(start, periods=len(closes))
    closes = np.asarray(closes, dtype=float)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes), "low": np.minimum(opens, closes),
                         "close": closes, "raw_close": closes, "volume": float(volume)}, index=idx)
