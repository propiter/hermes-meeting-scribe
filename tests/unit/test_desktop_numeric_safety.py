"""Untrusted numeric inputs must neither crash SQLite/JSON nor disclose raw values."""
import pytest

from meeting_scribe.config import SPEC, Settings, validate_value
from meeting_scribe.desktop.queries import decode_cursor, encode_cursor
from meeting_scribe.desktop.settings import llm_update
from meeting_scribe.llm_config import view
from tests.unit.test_llm_config import MemStore


@pytest.mark.parametrize("raw", [float('nan'), float('inf'), float('-inf'), 'NaN', 'Infinity', '-Infinity'])
def test_nonfinite_settings_and_timeout_are_rejected(raw):
    for key, option in SPEC.items():
        if option.kind == 'float':
            with pytest.raises(ValueError):
                validate_value(key, raw)
    with pytest.raises(ValueError):
        llm_update(MemStore(), {'timeout': raw})
    assert view(MemStore({'timeout': raw})).timeout is None


@pytest.mark.parametrize('values', [[0.0, 10**100], [0.0, -(2**63)-1], [float('nan'), 1], [float('inf'), 1]])
def test_invalid_numeric_cursor_is_rejected(values):
    with pytest.raises(ValueError):
        decode_cursor(encode_cursor(values), (float, int))


def test_invalid_settings_diagnostics_never_echo_raw_input():
    secret = 'opaque-private-credential'
    settings = Settings.load(lambda key, default=None: secret if key == 'analysis_timeout_seconds' else default)
    assert secret not in str(settings.warnings)
    assert secret not in str(view(MemStore({'timeout': secret})).problems)
