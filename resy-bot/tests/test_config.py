import datetime as dt
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as config_mod  # noqa: E402


def test_example_config_loads(tmp_path):
    path = tmp_path / "config.toml"
    shutil.copy(ROOT / "config.example.toml", path)
    cfg = config_mod.load(path)
    assert cfg.party_size == 2
    assert cfg.dry_run is True and cfg.stop_after_first is True
    assert [r.name for r in cfg.restaurants] == ["Example A", "Example B"]
    assert cfg.restaurants[0].release_days_ahead == 30
    assert cfg.restaurants[0].release_time == dt.time(10)
    assert cfg.restaurants[1].seating == ["dining room"]


def test_expand_dates_keeps_listed_order_then_weekdays():
    dates = config_mod.expand_dates({
        "dates": ["2026-11-21", "2026-11-14"],
        "start": "2026-11-01", "end": "2026-11-15", "weekdays": ["Sat"],
    })
    assert dates == [dt.date(2026, 11, 21), dt.date(2026, 11, 14), dt.date(2026, 11, 7)]


@pytest.mark.parametrize("body, message", [
    ('party_size = 2\n[account]\nemail="a@b.c"\n[when]\ndates=["2026-11-14"]', "restaurants"),
    ('party_size = 2\n[account]\nemail="a@b.c"\n[[restaurants]]\nvenue="x"', "No dates"),
    ('party_size = 2\n[account]\nemail="a@b.c"\n[when]\ndates=["2026-11-14"]\n'
     '[[restaurants]]\nvenue="x"\nrelease_days_ahead=30', "both"),
    ('party_size = 0\n[account]\nemail="a@b.c"', "party_size"),
])
def test_bad_configs_explain_themselves(tmp_path, body, message):
    path = tmp_path / "config.toml"
    path.write_text(body)
    with pytest.raises(config_mod.ConfigError, match=message):
        config_mod.load(path)
