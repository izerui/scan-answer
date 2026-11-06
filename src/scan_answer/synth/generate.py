"""生成模拟测试集：每张卡两张扫描图 + 一份标准答案 truth.json。

文件命名：{seq:04d}_1.jpg、{seq:04d}_2.jpg（扫描仪输出顺序），{seq:04d}.truth.json
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ..template.model import Template
from .draw import handwrite, paste_barcode, pencil_mark
from .scan import fold_corner, random_params, scanify, show_through

ESSAY = ("管理的本质在于激发人的善意与潜能。企业在快速扩张的过程中往往把注意力放在规模与速度上，"
         "却忽略了组织内部的信任与沟通。由此可见，管理者应当把权力下放到最接近问题的地方。")
NAMES = ["张明远", "李思琪", "王浩然", "陈雨桐", "刘子墨", "赵一诺"]
UNITS = ["北京理工大学", "复旦大学", "浙江大学", "南京大学"]

# 客观题情形 -> 概率
CASES = [("normal", 0.84), ("blank", 0.04), ("multiple", 0.03), ("light", 0.04), ("erased", 0.05)]


def _pick(rng, items):
    names, probs = zip(*items)
    return names[int(rng.choice(len(names), p=np.array(probs) / sum(probs)))]


def make_sheet(t: Template, bases: dict[str, np.ndarray], rng: np.random.Generator) -> tuple[dict[str, np.ndarray], dict]:
    """在模板坐标系下生成一张填好的卡（纸面图），返回 ({face: img}, truth)。"""
    dpi = t.dpi
    paper = {k: v.copy() for k, v in bases.items()}
    truth: dict = {"objective": [], "exam_no": None, "absent": False, "barcode": None, "physical": None}

    if t.exam_no:
        digits = [str(int(d)) for d in rng.integers(0, 10, len(t.exam_no.grid.cells))]
        # 漏涂 10%、多涂 5%，互斥
        r = float(rng.random())
        blank_pos = int(rng.integers(0, len(digits))) if r < 0.10 else -1
        multi_pos = int(rng.integers(0, len(digits))) if 0.10 <= r < 0.15 else -1
        value = ""
        for pos, (d, cells) in enumerate(zip(digits, t.exam_no.grid.cells)):
            if pos == blank_pos:
                value += "?"
                continue
            pencil_mark(paper[t.exam_no.grid.face], cells[int(d)], dpi, "normal", rng)
            if pos == multi_pos:
                # 同一列再涂一个不同数字
                other = (int(d) + int(rng.integers(1, 10))) % 10
                pencil_mark(paper[t.exam_no.grid.face], cells[other], dpi, "normal", rng)
                value += "?"
            else:
                value += d
        has_anomaly = blank_pos >= 0 or multi_pos >= 0
        truth["exam_no"] = {"value": value, "status": "ok" if not has_anomaly else "partial", "full": "".join(digits)}
    if t.absent and rng.random() < 0.03:
        pencil_mark(paper[t.absent.face], t.absent.rect, dpi, "normal", rng)
        truth["absent"] = True

    for blk in t.objective:
        labels = blk.grid.labels
        for k, cells in enumerate(blk.grid.cells):
            case = _pick(rng, CASES)
            face = paper[blk.grid.face]
            opts = rng.permutation(len(labels))
            if case == "normal":
                chosen = [int(opts[0])]
                exp = ("".join(labels[i] for i in chosen), "ok")
            elif case == "blank":
                chosen = []
                exp = ("", "blank")
            elif case == "multiple":
                chosen = sorted(int(i) for i in opts[:2])
                exp = ("".join(labels[i] for i in chosen), "multiple")
            elif case == "light":
                chosen = []
                pencil_mark(face, cells[int(opts[0])], dpi, "light", rng)
                exp = (labels[int(opts[0])], "uncertain")
            else:  # erased：一个选项擦掉，另一个正常涂
                pencil_mark(face, cells[int(opts[1])], dpi, "erased", rng)
                chosen = [int(opts[0])]
                exp = (labels[int(opts[0])], "ok")
            for i in chosen:
                pencil_mark(face, cells[i], dpi, "normal", rng)
            truth["objective"].append({"no": str(blk.start + k), "case": case, "answer": exp[0], "status": exp[1]})

    for f in t.fields:
        text = str(rng.choice(NAMES)) if f.key.startswith("name") else str(rng.choice(UNITS))
        handwrite(paper[f.face], f.rect, dpi, text, rng, char_mm=min(8.0, f.rect[3] * 0.55))
    for sq in t.subjective:
        for r in sq.regions:
            handwrite(paper[r.face], r.rect, dpi, ESSAY, rng, char_mm=5.6, pitch_mm=7.6, line_mm=7.95)
    for bc in t.barcodes:
        if bc.key == "student_barcode" and truth["exam_no"] and rng.random() < 0.9:
            paste_barcode(paper[bc.face], bc.rect, dpi, truth["exam_no"]["full"])
            truth["barcode"] = truth["exam_no"]["full"]
    return paper, truth


def make_scans(t: Template, paper: dict[str, np.ndarray], truth: dict, rng: np.random.Generator,
               out_dpi: float = 300, special: str | None = None) -> list[np.ndarray]:
    """纸面图 -> 两张扫描图（按扫描仪输出顺序）。special: rot180/swap/fold/blank 之一或 None。"""
    front, back = paper["front"], paper.get("back")
    truth["special"] = special
    if special == "fold":
        front = fold_corner(front, t.dpi)
    if back is not None:
        back = show_through(back, paper["front"], strength=float(rng.uniform(0.05, 0.12)))
    scans = [scanify(front, t.dpi, out_dpi, random_params(rng), rng)]
    if back is not None:
        scans.append(scanify(back, t.dpi, out_dpi, random_params(rng), rng))
    if special == "rot180":
        scans[0] = cv2.rotate(scans[0], cv2.ROTATE_180)
    if special == "swap" and len(scans) == 2:
        scans.reverse()
    if special == "blank":
        scans[-1] = np.clip(np.full_like(scans[-1], 247) + rng.normal(0, 3, scans[-1].shape), 0, 255).astype(np.uint8)
        truth["physical"] = "BLANK_PAGE"
    return scans


SPECIALS = [(None, 0.72), ("rot180", 0.10), ("swap", 0.10), ("fold", 0.05), ("blank", 0.03)]


def generate_dataset(t: Template, bases: dict[str, np.ndarray], out_dir: str | Path, count: int, seed: int = 0,
                     out_dpi: float = 300) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    for seq in range(1, count + 1):
        paper, truth = make_sheet(t, bases, rng)
        scans = make_scans(t, paper, truth, rng, out_dpi, _pick(rng, SPECIALS))
        for i, img in enumerate(scans, start=1):
            Image.fromarray(img).save(out / f"{seq:04d}_{i}.jpg", quality=95, dpi=(out_dpi, out_dpi))
        target = out / f"{seq:04d}.truth.json"
        fd, tmp = tempfile.mkstemp(dir=out, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(truth, f, ensure_ascii=False, indent=1)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
