from app import total


def test_sums_every_item():
    assert total([1, 2, 3]) == 6


def test_empty_list_returns_zero():
    assert total([]) == 0
