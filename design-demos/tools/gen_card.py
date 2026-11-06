"""按 199 管综答题卡版式程序化绘制 A3 横版答题卡 SVG（空白卡 + 已填涂卡），供三个设计方向共用。

坐标单位 mm，画布 420x297。
用法: python3 gen_card.py <输出目录>
"""
import sys
from pathlib import Path

W, H = 420, 297
INK = "#1d1d1d"
GRID = "#8a8a8a"
HAND = "#2b3a67"  # 考生笔迹（蓝黑色签字笔）
PENCIL = "#262626"  # 2B 铅笔涂卡

EXAM_NO = "100015000123456"
ANSWERS = {}
_seq = "BDACE" * 11
for i in range(1, 56):
    ANSWERS[i] = _seq[(i * 7) % 55]
ANSWERS[12] = "AC"  # 多涂
ANSWERS[37] = "~C"  # 涂得浅
ANSWERS[44] = ""  # 未作答
ESSAY = (
    "管理的本质在于激发人的善意与潜能。企业在快速扩张的过程中，往往把注意力放在规模与速度上，"
    "却忽略了组织内部的信任与沟通。材料中的公司之所以陷入困境，并非缺少资源，而是决策链条过长，"
    "一线员工的判断无法及时转化为行动。由此可见，管理者应当把权力下放到最接近问题的地方，"
    "同时建立清晰的目标与反馈机制，使每个人都明白自己的工作如何影响整体。"
)


def rect(x, y, w, h, fill="none", stroke=INK, sw=0.25, extra=""):
    return f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {extra}/>'


def text(x, y, s, size=2.6, anchor="start", weight="normal", fill=INK, family="'Songti SC','STSong','SimSun',serif", extra=""):
    return f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" text-anchor="{anchor}" font-weight="{weight}" fill="{fill}" font-family="{family}" {extra}>{s}</text>'


def bubble(x, y, label, filled=0.0, w=4.6, h=2.6):
    """[A] 形式的选项框，filled 为涂黑程度 0~1。"""
    out = [text(x, y + h * 0.78, "[", 2.4, fill=GRID), text(x + w - 0.8, y + h * 0.78, "]", 2.4, fill=GRID)]
    out.append(text(x + w / 2, y + h * 0.8, label, 2.0, "middle", fill=GRID, family="Arial,Helvetica,sans-serif"))
    if filled > 0:
        op = 0.25 + 0.7 * filled
        out.append(rect(x + 0.35, y + 0.15, w - 0.7, h - 0.3, fill=PENCIL, stroke="none", extra=f'opacity="{op:.2f}" rx="0.3"'))
    return "".join(out)


def card(filled: bool) -> str:
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W * 4}" height="{H * 4}">',
         rect(0, 0, W, H, fill="#fbfbf8", stroke="none")]
    # 四角定位块
    for x, y in [(10, 9), (402, 9), (10, 283), (402, 283)]:
        p.append(rect(x, y, 8, 5, fill=INK, stroke="none"))
    # 顶部同步头（左栏）
    for i in range(18):
        p.append(rect(40 + i * 8.5, 18, 4.5, 2.2, fill=INK, stroke="none"))
    # 折叠线
    p.append(f'<line x1="210" y1="14" x2="210" y2="290" stroke="{GRID}" stroke-width="0.3" stroke-dasharray="1.5 1.5"/>')
    for i, ch in enumerate("请勿折叠"):
        p.append(text(213, 70 + i * 40, ch, 3, fill=GRID))

    # 标题
    p.append(text(110, 31, "全国硕士研究生招生考试", 5.2, "middle", "bold"))
    p.append(text(110, 39, "管理类综合能力答题卡（199）", 5.2, "middle", "bold"))

    # 报考单位 / 考生姓名
    p.append(rect(20, 44, 80, 12) + text(23, 51.5, "报考单位", 2.6))
    p.append(rect(20, 58, 80, 12) + text(23, 65.5, "考生姓名", 2.6))
    if filled:
        p.append(text(45, 52.5, "北京理工大学", 5, fill=HAND, family="'Kaiti SC','STKaiti','KaiTi',cursive"))
        p.append(text(45, 66.5, "张明远", 5.4, fill=HAND, family="'Kaiti SC','STKaiti','KaiTi',cursive"))

    # 考生编号 15 位填涂
    gx, gy, cw = 105, 44, 6.2
    p.append(rect(gx, gy, cw * 15, 56) + text(gx + cw * 7.5, gy + 4, "考 生 编 号（左对齐）", 2.6, "middle"))
    for c in range(15):
        p.append(rect(gx + c * cw, gy + 5.5, cw, 5, stroke=GRID, sw=0.2))
        if filled:
            p.append(text(gx + c * cw + cw / 2, gy + 9.4, EXAM_NO[c], 3.4, "middle", fill=HAND, family="'Kaiti SC','STKaiti','KaiTi',cursive"))
        for d in range(10):
            f = 0.0
            if filled and int(EXAM_NO[c]) == d and c != 14:
                f = 1.0
            p.append(bubble(gx + c * cw + 0.8, gy + 12 + d * 4.3, str(d), f, w=4.6, h=2.6))

    # 注意事项
    p.append(rect(20, 72, 80, 28))
    notes = ["注意事项", "1. 填(书)写必须使用黑色字迹签字笔。", "2. 选择题必须用 2B 铅笔填涂。", "3. 请勿折叠，保持卡面清洁。", "4. 缺考考生由监考员填涂缺考标记。"]
    for i, n in enumerate(notes):
        p.append(text(23, 77 + i * 5, n, 2.4, weight="bold" if i == 0 else "normal"))
    # 正确涂卡 / 缺考标记
    p.append(rect(20, 103, 185, 9))
    p.append(text(23, 109, "正确涂卡", 2.6) + rect(37, 105.6, 5, 2.8, fill=INK, stroke="none"))
    p.append(text(60, 109, "错误涂卡  ☑ ☒ ⊖ ◐", 2.6, fill=GRID))
    p.append(text(130, 109, "缺考标记", 2.6) + rect(144, 105.6, 5, 2.8, stroke=INK))
    p.append(text(155, 109, "由监考员填涂", 2.2, fill=GRID))

    # 选择题区
    p.append(rect(20, 116, 185, 164))
    p.append(text(112, 123, "选择题答案区域", 3.4, "middle", "bold"))
    col_x = [24, 70, 116, 160]
    for q in range(1, 56):
        g = (q - 1) // 5  # 第几组
        col, row_group = g // 3, g % 3
        r = (q - 1) % 5
        x = col_x[col]
        y = 130 + row_group * 45 + r * 7
        p.append(text(x + 4, y + 2.1, str(q), 2.4, "end", family="Arial,Helvetica,sans-serif"))
        ans = ANSWERS.get(q, "") if filled else ""
        light = ans.startswith("~")
        ans = ans.lstrip("~")
        for k, opt in enumerate("ABCDE"):
            f = (0.35 if light else 1.0) if opt in ans else 0.0
            p.append(bubble(x + 5 + k * 7.6, y, opt, f))
    p.append(rect(20, 282, 185, 0.01, stroke="none"))

    # 右栏：条形码 + 写作
    p.append(rect(330, 22, 70, 18, extra='rx="2"'))
    if not filled:
        p.append(text(365, 33, "考生信息条形码粘贴位置", 2.6, "middle", fill=GRID))
    p.append(rect(250, 22, 70, 18, extra='rx="2"') + text(285, 33, "试题信息条形码", 2.6, "middle", fill=GRID))
    if filled:
        bx = 336
        import random
        rnd = random.Random(7)
        while bx < 394:
            bw = rnd.choice([0.3, 0.3, 0.6, 0.9])
            p.append(rect(bx, 25, bw, 10, fill=INK, stroke="none"))
            bx += bw + rnd.choice([0.3, 0.6, 0.9])
        p.append(text(365, 38.5, EXAM_NO, 2.2, "middle", family="Arial,Helvetica,sans-serif"))
    p.append(rect(225, 46, 180, 236))
    p.append(text(229, 52, "写作 56", 3.2, weight="bold"))
    cols, size = 22, 7.6
    ox, oy = 231, 56
    rows = 29
    chars = list(ESSAY) if filled else []
    for r in range(rows):
        for c in range(cols):
            p.append(rect(ox + c * size, oy + r * (size + 0.4) * 0.92, size, size * 0.92 - 0.6, stroke="#9c9c9c", sw=0.15))
            idx = r * cols + c
            if idx < len(chars):
                p.append(text(ox + c * size + size / 2, oy + r * (size + 0.4) * 0.92 + 5.2, chars[idx], 4.6, "middle", fill=HAND, family="'Kaiti SC','STKaiti','KaiTi',cursive"))
    p.append(text(400, 280, "200", 1.8, "end", fill=GRID))
    p.append("</svg>")
    return "".join(p)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "card-blank.svg").write_text(card(False), encoding="utf-8")
    (out / "card-filled.svg").write_text(card(True), encoding="utf-8")
    print("ok", out)
