import pytest

from sre_agent.core import db


def test_create_get_roundtrip():
    i = db.create_item("k", {"x": [1, 2]})
    item = db.get_item(i)
    assert item["kind"] == "k" and item["status"] == "new" and item["data"] == {"x": [1, 2]}


def test_get_unknown_returns_none():
    assert db.get_item("missing") is None


def test_update_merges_data_and_status():
    i = db.create_item("k", {"a": 1})
    db.update_item(i, status="done", b=2)
    item = db.get_item(i)
    assert item["status"] == "done" and item["data"] == {"a": 1, "b": 2}


def test_update_without_status_keeps_it():
    i = db.create_item("k", {}, status="s1")
    db.update_item(i, a=1)
    assert db.get_item(i)["status"] == "s1"


def test_update_unknown_raises():
    with pytest.raises(KeyError):
        db.update_item("missing", status="x")


def test_list_filters():
    a = db.create_item("one", {}, status="new")
    db.create_item("two", {}, status="done")
    assert [x["id"] for x in db.list_items(kind="one")] == [a]
    assert [x["kind"] for x in db.list_items(status="done")] == ["two"]
    assert len(db.list_items()) == 2
