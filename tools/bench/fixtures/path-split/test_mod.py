from mod import parent_dir


def test_parent_dir_posix():
    assert parent_dir("/srv/apps/proj-a/src/main.py") == "src"


def test_parent_dir_windows():
    assert parent_dir("C:\\work\\proj-a\\src\\main.py") == "src"


def test_parent_dir_bare_name():
    assert parent_dir("main.py") == "main.py"
