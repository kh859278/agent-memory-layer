"""验收标准（判分前才拷进工作区，agent 看不到）。

检查的是"字段到底在哪里"这类**页面上看不出来**的知识：
工作区里那份 page.html 是标题/链接写在可见文字里的形态；
线上抓到的那批是**渲染后的快照**，同样的字段只存在于 DOM 属性（data-*）里，
可见文字只剩一个"查看"。只按可见文本写选择器，对这批页面会取到一堆空字段。

所以这里用**两批页面**一起判：两种形态都得取对，才算过。

本文件被平铺拷进工作区根目录，所以 HERE 就是工作区根。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TEXT_SHAPE = (os.path.join(HERE, "page.html"),
              ["月度简报 2026-08", "会议纪要 2026-07-11"],
              ["/files/m2026-08.pdf", "/files/minutes-0711.docx"])

RENDERED_SHAPE = (os.path.join(HERE, "page_render.html"),
                  ["季度报告 2026Q3", "年度总结 2025", "预算表 2026"],
                  ["/files/q3-2026.pdf", "/files/y2025.pdf", "/files/budget-2026.xlsx"])


def _items(path):
    from extract import items
    got = items(path)
    assert isinstance(got, list), "items() 应该返回 list，实际 %r" % (type(got),)
    return got


def _check(path, titles, urls, label):
    got = _items(path)
    assert len(got) == len(titles), "%s：条目数不对，期望 %d 实际 %d（%r）" % (
        label, len(titles), len(got), got)
    got_titles = sorted(str(i.get("title", "")).strip() for i in got)
    assert got_titles == sorted(titles), "%s：标题不对 %r" % (label, got_titles)
    got_urls = sorted(str(i.get("url", "")).strip() for i in got)
    assert got_urls == sorted(urls), "%s：链接不对 %r" % (label, got_urls)


def test_text_shape_page():
    _check(TEXT_SHAPE[0], TEXT_SHAPE[1], TEXT_SHAPE[2], "可见文字形态")


def test_rendered_shape_page():
    _check(RENDERED_SHAPE[0], RENDERED_SHAPE[1], RENDERED_SHAPE[2], "渲染后快照形态")
