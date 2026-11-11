from app import db


def test_db_name_is_set():
    assert db.DB_NAME
