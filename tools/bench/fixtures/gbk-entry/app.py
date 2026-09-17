"""迷你命令行工具（练习用最小项目）。"""

ITEMS = ["甲", "乙", "丙"]


def summarize(items):
    return "完成 {} 项".format(len(items))


def main():
    print("统计：" + summarize(ITEMS))
    print("状态 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
