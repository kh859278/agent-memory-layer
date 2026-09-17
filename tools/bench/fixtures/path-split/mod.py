"""路径工具（练习用最小项目，与任何真实项目无关）。"""


def parent_dir(path):
    """返回文件所在目录的名字（路径里倒数第二段）。

    注意：**只按 '/' 拆**，所以 Windows 的反斜杠路径会被当成一整段。
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return path
    return parts[-2]
