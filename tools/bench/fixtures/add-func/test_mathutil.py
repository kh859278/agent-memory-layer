import pytest

from mathutil import clamp, mean


def test_clamp():
    assert clamp(5, 0, 3) == 3
    assert clamp(-1, 0, 3) == 0


def test_mean():
    assert mean([1, 2, 3]) == 2
    assert mean([2.5, 2.5]) == 2.5


def test_mean_empty():
    with pytest.raises(ValueError):
        mean([])
