from analyzer import top_words


def test_top_words_basic():
    assert top_words("a b b c c c", 2) == ["c", "b"]


def test_top_words_limit():
    assert len(top_words("a b c d", 2)) == 2


def test_top_words_empty():
    assert top_words("", 3) == []
