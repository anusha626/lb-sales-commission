"""Stale-session detection: objects built before a deploy added a field."""

from commission.incentive import IncentiveHistory
from commission.settings import is_stale, load_all


def test_freshly_loaded_settings_are_not_stale():
    assert not is_stale(load_all())
    assert not is_stale(IncentiveHistory())


def test_object_missing_a_field_is_stale():
    settings = load_all()
    # What an object built by the previous version of the class looks like.
    del settings.incentive.__dict__["event_sales_weight"]
    assert is_stale(settings.incentive)
    # Found through the parent, and inside containers.
    assert is_stale(settings)
    assert is_stale([settings])
    assert is_stale({"a": settings})


def test_plain_values_are_not_stale():
    assert not is_stale(None)
    assert not is_stale([1, "x", {"k": 2.0}])
