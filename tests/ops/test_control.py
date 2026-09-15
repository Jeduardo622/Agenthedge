from datetime import datetime, timezone

import pytest

from ops.control import ControlResult, _aware


def test_control_result_accepts_only_declared_states():
    assert ControlResult("cmd", "HALTING", (), ()).state == "HALTING"
    with pytest.raises(ValueError, match="invalid control state"):
        ControlResult("cmd", "DONE", (), ())


def test_control_clock_requires_timezone():
    with pytest.raises(ValueError, match="timezone-aware"):
        _aware(datetime(2026, 1, 1))
    value = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _aware(value) == value
