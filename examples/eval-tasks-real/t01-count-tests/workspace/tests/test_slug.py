from app import slugify


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_single():
    assert slugify("x") == "x"


def test_spaces():
    assert slugify("  a   b ") == "a-b"
