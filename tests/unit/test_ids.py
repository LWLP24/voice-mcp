from calltool.calls.ids import new_id


def test_ids_include_prefix_and_are_unique() -> None:
    first = new_id("call")
    second = new_id("call")

    assert first.startswith("call_")
    assert len(first) == 31
    assert first != second
