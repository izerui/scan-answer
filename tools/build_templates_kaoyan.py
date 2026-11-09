"""从考研统考答题卡空白卡 PDF 生成模板（英语一、英语二、数学一、政治、计算机 408）。

PDF 每张卡两页（正面、背面），A3 横版。英语一、二版式相同，坐标按“相对左上角定位块中心”的 mm 记录、
每页按自己的定位块平移；其余卡用该卡底图上的绝对 mm。考号区和客观题块都按印刷括号框 [X] 的竖笔画检测，
再做按轴缩放平移的网格拟合（这批扫描件印得浅，基于墨迹的吸附找不到格子）。

用法: python tools/build_templates_kaoyan.py samples/raw/kaoyan-5cards.pdf templates [--overlay 叠图目录] [--only eng2 ...]
（输出到 templates/kaoyan_<卡名>/；计算机 408 的 PDF 只有第 1 张纸，第 2 张（44–47 题）需要多张纸支持，属于后期功能）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

from scan_answer.template import (AlignSpec, BarcodeSpec, ExamNoSpec, Face, FieldSpec, ObjectiveBlock, OmrGrid,
                                  Region, SubjectiveSpec, Template, save_template)
from scan_answer.engine.align import detect_frames
from scan_answer.template.auto import auto_anchors, auto_frames, auto_sync, import_base, load_pages
from scan_answer.template.layout import grid_cells
from scan_answer.template.validate import check_alignment, validate_template

DPI = 300
S = DPI / 25.4


# ---------- 括号框检测与网格拟合（考号区） ----------
def bracket_boxes(img: np.ndarray, rect: tuple[float, float, float, float], width: float) -> list[tuple[float, float]]:
    """在 mm 区域内找 [X] 式框：成对的竖笔画，间距约等于框宽。返回框中心 (x, y) mm。"""
    x0, y0, x1, y1 = rect
    sub = img[int(y0 * S):int(y1 * S), int(x0 * S):int(x1 * S)]
    bw = cv2.adaptiveThreshold(sub, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 12)
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(1.4 * S))))
    n, _, st, _ = cv2.connectedComponentsWithStats(v, 8)
    strokes = []
    for i in range(1, n):
        x, y, w, h, _ = st[i]
        if 1.4 * S <= h <= 3.6 * S and w <= 1.0 * S:
            strokes.append((x0 + (x + w / 2) / S, y0 + (y + h / 2) / S, h / S))
    strokes.sort()
    centers = []
    for a in strokes:
        for b in strokes:
            dx = b[0] - a[0]
            if abs(dx - width) < 0.35 and abs(b[1] - a[1]) < 0.5 and abs(b[2] - a[2]) < 0.8:
                centers.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
    return centers


def _axis_fit(src: np.ndarray, dst: np.ndarray) -> tuple[float, float]:
    """单轴线性拟合 dst = a·src + b；该方向上点几乎没有展开（如单行题块）时只拟合平移。"""
    if np.ptp(src) < 5.0:
        return 1.0, float(np.mean(dst - src))
    a, b = np.polyfit(src, dst, 1)
    return float(a), float(b)


def fit_lattice(cells: list[list[tuple]], detections: list[tuple[float, float]], tol: float = 1.2):
    """把网格拟合到检测到的框中心：最近邻配对 + 按轴缩放平移的最小二乘，迭代 4 次。返回 (新格子, 配对数, 95% 分位残差)。"""
    cur = [list(r) for r in cells]
    det = np.array(detections, dtype=np.float64)
    if len(det) < 6:
        return cur, 0, float("inf")
    for tol in (tol, 0.8, 0.6, 0.5):  # 逐轮收紧配对容差，剔除数字笔画等误配
        src, dst = [], []
        for row in cur:
            for c in row:
                cx, cy = c[0] + c[2] / 2, c[1] + c[3] / 2
                d = np.hypot(det[:, 0] - cx, det[:, 1] - cy)
                k = int(d.argmin())
                if d[k] < tol:
                    src.append((cx, cy)); dst.append(det[k])
        if len(src) < 6:
            return cur, len(src), float("inf")
        # 底图已纠偏：只拟合 x、y 各自的缩放和平移（不允许旋转、剪切，避免误配点把网格拉斜）
        src_a, dst_a = np.array(src), np.array(dst)
        ax, bx = _axis_fit(src_a[:, 0], dst_a[:, 0])
        ay, by = _axis_fit(src_a[:, 1], dst_a[:, 1])
        pred = np.c_[ax * src_a[:, 0] + bx, ay * src_a[:, 1] + by]
        res = np.linalg.norm(pred - dst_a, axis=1)
        cur = [[(ax * (c[0] + c[2] / 2) + bx - c[2] / 2, ay * (c[1] + c[3] / 2) + by - c[3] / 2, c[2], c[3])
                for c in row] for row in cur]
    cur = [[tuple(round(float(v), 3) for v in c) for c in row] for row in cur]
    return cur, len(src), float(np.percentile(res, 95))


# ---------- 卡面描述 ----------
# relative=True：坐标相对该面左上角定位块中心（英语一、二共用一套）；否则为该卡底图上的绝对 mm。
# 客观题块：起点为第一题 A 格中心，q 为相邻题的位移，o 为相邻选项的位移，每 every 题额外加 gap。
# 主观题区域：("front"/"back", 外框序号) 用自动检测到的外框（内缩 1.5 mm），
#            ("front"/"back", 外框序号, (第 i 份, 共 n 份)) 把外框竖向等分，或 ("front"/"back", (x, y, w, h)) 直接给矩形。
def obj(name, qtype, start, count, opts, first, q, o, every=0, gap=(0.0, 0.0)):
    return dict(name=name, qtype=qtype, start=start, count=count, opts=opts, first=first, q=q, o=o, every=every, gap=gap)


ENGLISH = {
    "relative": True, "box_w": 3.65,
    "exam": dict(first=(106.85, 41.45), digits=15, col=5.02, row=3.31, cell=(3.6, 2.1), area=(102.0, 38.9, 181.5, 75.9)),
    "fields": [("unit", "报考单位", (36.65, 26.95, 63.2, 21.6)), ("name", "考生姓名", (36.65, 51.75, 63.2, 21.9))],
    "absent": (34.25, 126.25, 4.8, 1.4),
    "barcodes": [("student_barcode", (236.05, 3.15, 50.2, 20.4)), ("paper_barcode", (323.15, 3.15, 40.2, 20.4))],
    "obj": [obj("1-10", "single", 1, 10, "ABCD", (20.04, 158.15), (0, 4.12), (5.09, 0), 5, (0, 2.40)),
            obj("11-20", "single", 11, 10, "ABCD", (48.75, 158.15), (0, 4.12), (5.09, 0), 5, (0, 2.40)),
            obj("21-30", "single", 21, 10, "ABCD", (80.95, 158.15), (0, 4.12), (5.09, 0), 5, (0, 2.40)),
            obj("31-40", "single", 31, 10, "ABCD", (110.65, 158.15), (0, 4.12), (5.09, 0), 5, (0, 2.40)),
            obj("41-45", "single", 41, 5, "ABCDEFG", (140.35, 158.15), (0, 4.12), (5.09, 0))],
    "ignore_front": (7.95, 207.65, 170.5, 57.7),
    "name_back": (32.85, 1.6, 43.0, 9.0),
}
LAYOUTS: dict[str, dict] = {
    "eng2": {**ENGLISH, "name": "考研英语二", "subject": "英语（二）",
             "subjective": [("46", "short", "en", [("front", 0)]), ("47", "essay", "en", [("back", 0)]),
                            ("48", "essay", "en", [("back", 1)])]},
    "eng1": {**ENGLISH, "name": "考研英语一", "subject": "英语（一）",
             "subjective": [(str(46 + i), "short", "en", [("front", 0, (i, 5))]) for i in range(5)]
                           + [("51", "essay", "en", [("back", 0)]), ("52", "essay", "en", [("back", 1)])]},
    "math1": {
        "name": "考研数学一", "subject": "数学（一）", "relative": False, "box_w": 3.5,
        "exam": dict(first=(76.2, 52.9), digits=15, col=5.0, row=3.31, cell=(3.5, 2.1), area=(72, 49, 150, 86), box_w=3.55),
        "fields": [("unit", "报考单位", (20.4, 48.9, 51.3, 13.4)), ("name", "考生姓名", (20.4, 73.4, 51.3, 13.3))],
        "absent": (47.1, 132.4, 5.0, 1.6),
        "barcodes": [("student_barcode", (187.2, 16.6, 50.4, 20.0)), ("paper_barcode", (317.8, 16.9, 40.1, 20.0))],
        "obj": [obj("1-5", "single", 1, 5, "ABCD", (30.5, 153.4), (24.45, 0), (4.27, 0)),
                obj("6-10", "single", 6, 5, "ABCD", (30.5, 158.5), (24.45, 0), (4.27, 0))],
        "subjective": [("11", "fill", "math", [("front", (26.0, 171.8, 58.0, 11.0))]),
                       ("12", "fill", "math", [("front", (85.0, 171.8, 57.0, 11.0))]),
                       ("13", "fill", "math", [("front", (26.0, 183.0, 58.0, 11.0))]),
                       ("14", "fill", "math", [("front", (85.0, 183.0, 57.0, 11.0))]),
                       ("15", "fill", "math", [("front", (26.0, 194.2, 58.0, 11.0))]),
                       ("16", "fill", "math", [("front", (85.0, 194.2, 57.0, 11.0))]),
                       ("17", "short", "math", [("front", (22.5, 207.5, 124.0, 63.0)),
                                                ("front", (153.6, 41.5, 119.3, 67.3))]),
                       ("18", "short", "math", [("front", (153.6, 110.8, 119.3, 164.9))]),
                       ("19", "short", "math", [("front", 1)]),
                       ("20", "short", "math", [("back", 0)]), ("21", "short", "math", [("back", 1)]),
                       ("22", "short", "math", [("back", 2)])],
        "name_back": (47.0, 13.0, 43.0, 9.0),
    },
    "politics": {
        "name": "考研思想政治理论", "subject": "思想政治理论", "relative": False, "box_w": 3.65,
        "exam": dict(first=(119.4, 54.7), digits=15, col=5.02, row=3.29, cell=(3.6, 2.1), area=(115, 51, 195, 87)),
        "fields": [("unit", "报考单位", (50.7, 40.4, 63.0, 21.5)), ("name", "考生姓名", (50.7, 65.2, 63.0, 21.7))],
        "absent": (48.4, 139.1, 4.7, 2.0),
        "barcodes": [("student_barcode", (251.0, 15.0, 50.5, 19.9)), ("paper_barcode", (339.0, 15.3, 40.2, 19.9))],
        "obj": [obj("1-16", "single", 1, 16, "ABCD", (57.0, 159.3), (5.1, 0), (0, 4.17), 4, (10.0, 0)),
                obj("17-33", "multiple", 17, 17, "ABCD", (57.0, 187.6), (5.1, 0), (0, 4.17), 4, (10.0, 0))],
        "ignore_front": (22.7, 209.6, 171.0, 66.8),
        "subjective": [("34", "short", "zh", [("front", 0)]), ("35", "short", "zh", [("back", 0)]),
                       ("36", "short", "zh", [("back", 1)])],
        "name_back": (43.0, 16.0, 42.0, 9.0),
    },
    "cs408": {
        "name": "考研计算机408（第1张）", "subject": "计算机学科专业基础", "relative": False, "box_w": 3.35,
        "exam": dict(first=(119.0, 61.8), digits=15, col=5.08, row=2.98, cell=(3.4, 2.0), area=(115, 58, 195, 92), box_w=3.45),
        "fields": [("unit", "报考单位", (48.7, 49.9, 64.3, 19.0)), ("name", "考生姓名", (48.7, 72.0, 64.3, 18.9))],
        "absent": (56.1, 133.0, 3.9, 1.9),
        "barcodes": [("student_barcode", (248.4, 26.3, 44.7, 16.8)), ("paper_barcode", (330.5, 26.4, 34.1, 16.6))],
        "obj": [obj("1-20", "single", 1, 20, "ABCD", (37.4, 160.9), (5.17, 0), (0, 3.37), 5, (10.1, 0)),
                obj("21-40", "single", 21, 20, "ABCD", (37.4, 184.5), (5.17, 0), (0, 3.37), 5, (10.1, 0))],
        "ignore_front": (19.0, 206.2, 175.0, 64.6),
        "subjective": [("41", "short", "zh", [("front", 0)]), ("42", "short", "zh", [("back", 0)]),
                       ("43", "short", "zh", [("back", 1)])],
        "name_back": (185.0, 15.0, 52.0, 9.0),
    },
}


def lattice(first, count, n_opts, q, o, every, gap, cell):
    w, h = cell
    out = []
    for k in range(count):
        extra = (k // every) if every else 0
        row = []
        for j in range(n_opts):
            cx = first[0] + k * q[0] + j * o[0] + extra * gap[0]
            cy = first[1] + k * q[1] + j * o[1] + extra * gap[1]
            row.append((round(cx - w / 2, 3), round(cy - h / 2, 3), w, h))
        out.append(row)
    return out


def _best_frames(img: np.ndarray, frames: list) -> list:
    """扫描件有轻微不均匀变形时，多个外框可能无法用同一个透视变换对准：
    在外框组合里选自检残差 ≤ 0.5 mm 且外框最多的一组（同样多时取残差最小）。"""
    from itertools import combinations
    from scan_answer.engine.align import align_primary
    best = None
    for k in range(len(frames), 0, -1):
        for combo in combinations(frames, k):
            spec = AlignSpec(method="frames", frames=[tuple(round(v, 2) for v in r) for r in combo])
            res = align_primary(img, spec, DPI)
            if res.ok and res.residual_mm <= 0.5 and (best is None or res.residual_mm < best[0]):
                best = (res.residual_mm, spec.frames)
        if best:
            return best[1]
    return [tuple(round(v, 2) for v in r) for r in frames]


def offset_rect(r, o):
    return (round(r[0] + o[0], 2), round(r[1] + o[1], 2), r[2], r[3])


def anchor_origin(anchors):
    x, y, w, h = anchors[0]
    return (x + w / 2, y + h / 2)


def build(card: str, layout: dict, front: np.ndarray, back: np.ndarray, paper, log) -> tuple[Template, dict]:
    bases = {"front": front, "back": back}
    faces, origin, frames_by_face = {}, {}, {}
    for f, img in bases.items():
        an = auto_anchors(img, DPI, paper)
        fr = sorted(auto_frames(img, DPI, paper, max_n=8), key=lambda r: (r[0], r[1]))
        frames_by_face[f] = fr
        if an:
            faces[f] = Face(image=f"{f}.png", align=AlignSpec(method="anchors", anchors=[tuple(round(v, 2) for v in a) for a in an]))
            origin[f] = anchor_origin(an)
        else:
            big = [r for r in fr if r[2] * r[3] > 0.1 * paper[0] * paper[1]]
            # 用定位函数自己在底图上找到的轮廓记录外框，保证识别时取到的是同一条线（内线 / 外线）
            quads = detect_frames(img, big, DPI)
            if quads is not None:
                big = [(float(q[:, 0].min() / S), float(q[:, 1].min() / S),
                        float((q[:, 0].max() - q[:, 0].min()) / S), float((q[:, 1].max() - q[:, 1].min()) / S)) for q in quads]
            faces[f] = Face(image=f"{f}.png", align=AlignSpec(method="frames", frames=_best_frames(img, big)))
            origin[f] = None
            log(f"  {f}：没有找到 4 个定位块，改用 {len(big)} 个外框定位")
    if layout.get("relative"):
        of = origin["front"]
        if of is None:
            raise SystemExit(f"{card}: 正面没有找到定位块")
        ob = origin["back"] or of
    else:
        of = ob = (0.0, 0.0)
    box_w = layout.get("box_w", 3.65)

    # 考号
    e = layout["exam"]
    fx, fy = e["first"][0] + of[0], e["first"][1] + of[1]
    cells = grid_cells((fx, fy), n_groups=e["digits"], n_options=10, option_pitch=e["row"], group_pitch=e["col"],
                       cell_size=e["cell"], options_horizontal=False)
    a = e["area"]
    det = bracket_boxes(front, (a[0] + of[0], a[1] + of[1], a[2] + of[0], a[3] + of[1]), e.get("box_w", box_w))
    cells, matched, res = fit_lattice(cells, det)
    ok = matched >= 0.2 * e["digits"] * 10 and res < 0.45
    log(f"  考号：配对 {matched}/{e['digits'] * 10}，残差（95% 分位）{res:.2f} mm{'' if ok else '  ⚠ 拟合不可靠，请看叠图'}")
    exam_no = ExamNoSpec(grid=OmrGrid(face="front", labels=[str(d) for d in range(10)], cells=cells))

    # 客观题
    blocks = []
    for b in layout["obj"]:
        cell = (round(box_w - 0.05, 2), 2.3)
        c0 = lattice((b["first"][0] + of[0], b["first"][1] + of[1]), b["count"], len(b["opts"]), b["q"], b["o"],
                     b["every"], b["gap"], cell)
        xs = [c[0] for row in c0 for c in row] + [c[0] + c[2] for row in c0 for c in row]
        ys = [c[1] for row in c0 for c in row] + [c[1] + c[3] for row in c0 for c in row]
        det = bracket_boxes(front, (min(xs) - 3, min(ys) - 3, max(xs) + 3, max(ys) + 3), box_w)
        cells, matched, res = fit_lattice(c0, det)
        total = b["count"] * len(b["opts"])
        ok = matched >= 0.5 * total and res < 0.45
        log(f"  客观题 {b['name']}：配对 {matched}/{total}，残差（95% 分位）{res:.2f} mm{'' if ok else '  ⚠ 拟合不可靠，保留估计位置'}")
        blocks.append(ObjectiveBlock(name=b["name"], qtype=b["qtype"], start=b["start"], group=b["every"],
                                     grid=OmrGrid(face="front", labels=list(b["opts"]), cells=cells if ok else c0)))

    # 主观题
    front_answer = [r for r in frames_by_face["front"] if r[0] > paper[0] * 0.33 and r[2] * r[3] > 0.1 * paper[0] * paper[1] / 2]
    back_answer = [r for r in frames_by_face["back"] if r[2] * r[3] > 0.08 * paper[0] * paper[1]]
    pools = {"front": front_answer, "back": back_answer}
    subjective = []
    for no, qtype, content, refs in layout["subjective"]:
        regions = []
        for ref in refs:
            f = ref[0]
            if isinstance(ref[1], tuple):
                o = of if f == "front" else ob
                regions.append(Region(face=f, rect=offset_rect(ref[1], o)))
                continue
            x, y, w, h = pools[f][ref[1]]
            x, y, w, h = x + 1.5, y + 1.5, w - 3, h - 3
            if len(ref) > 2:
                i, n = ref[2]
                h = h / n
                y = y + i * h
            regions.append(Region(face=f, rect=(round(x, 2), round(y, 2), round(w, 2), round(h, 2))))
        subjective.append(SubjectiveSpec(no=no, qtype=qtype, content=content, regions=regions))

    fields = [FieldSpec(key=k, label=lab, face="front", rect=offset_rect(r, of)) for k, lab, r in layout["fields"]]
    if layout.get("name_back"):
        fields.append(FieldSpec(key="name_back", label="考生姓名（背面）", face="back", rect=offset_rect(layout["name_back"], ob)))
    sync = []
    for sy in auto_sync(front, DPI):
        sy.face = "front"
        sync.append(sy)
    t = Template(
        schema_version="1.0", name=layout["name"], version=1, paper=paper, dpi=DPI, faces=faces,
        exam_no=exam_no, absent=Region(face="front", rect=offset_rect(layout["absent"], of)),
        barcodes=[BarcodeSpec(key=k, face="front", rect=offset_rect(r, of)) for k, r in layout["barcodes"]],
        fields=fields, objective=blocks, subjective=subjective,
        ignore=[Region(face="front", rect=offset_rect(layout["ignore_front"], of))] if layout.get("ignore_front") else [],
        sync=sync,
    )
    return t, bases


def overlay(t: Template, bases: dict, out: Path) -> None:
    colors = {"exam": (191, 91, 29), "obj": (61, 128, 21), "sub": (9, 83, 180), "bar": (176, 63, 107), "info": (102, 90, 78)}
    for f, img in bases.items():
        c = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

        def box(r, col, th=2):
            x, y, w, h = r
            cv2.rectangle(c, (int(x * S), int(y * S)), (int((x + w) * S), int((y + h) * S)), col, th)
        if t.exam_no and t.exam_no.grid.face == f:
            for row in t.exam_no.grid.cells:
                for r in row:
                    box(r, colors["exam"])
        for b in t.objective:
            if b.grid.face == f:
                for row in b.grid.cells:
                    for r in row:
                        box(r, colors["obj"])
        for s in t.subjective:
            for reg in s.regions:
                if reg.face == f:
                    box(reg.rect, colors["sub"], 4)
        for b in t.barcodes:
            if b.face == f:
                box(b.rect, colors["bar"], 4)
        for fd in t.fields:
            if fd.face == f:
                box(fd.rect, colors["info"], 3)
        if t.absent and t.absent.face == f:
            box(t.absent.rect, colors["info"], 3)
        cv2.imwrite(str(out / f"{t.name}_{f}.jpg"), cv2.resize(c, None, fx=0.5, fy=0.5), [cv2.IMWRITE_JPEG_QUALITY, 85])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("out")
    ap.add_argument("--overlay")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    order = ["eng2", "eng1", "math1", "politics", "cs408"]
    pages = load_pages(args.pdf)
    for i, card in enumerate(order):
        if card not in LAYOUTS or (args.only and card not in args.only):
            continue
        fb = import_base(*pages[2 * i])
        bb = import_base(*pages[2 * i + 1])
        print(f"{card}（{LAYOUTS[card]['subject']}）")
        t, bases = build(card, LAYOUTS[card], fb.image, bb.image, fb.paper, print)
        problems = validate_template(t) + check_alignment(t, bases)
        if problems:
            print("  模板问题：\n   " + "\n   ".join(problems))
        out = Path(args.out) / f"kaoyan_{card}"
        save_template(t, out, bases)
        print(f"  已保存 {out}")
        if args.overlay:
            Path(args.overlay).mkdir(parents=True, exist_ok=True)
            overlay(t, bases, Path(args.overlay))


if __name__ == "__main__":
    sys.exit(main())
