"""从 page.html 里把每个条目的标题和链接提取出来。"""
import re


def items(path="page.html"):
    """返回 [{"title": ..., "url": ...}, ...]，顺序与页面一致。"""
    html = open(path, encoding="utf-8").read()
    # 目前只按可见文字取 —— 标题和链接都取不到
    return [{"title": t.strip(), "url": ""}
            for t in re.findall(r'<span class="t">(.*?)</span>', html)]
