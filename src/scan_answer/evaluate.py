"""把识别结果与模拟测试集的 truth.json 对比，输出指标。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Metrics:
    sheets: int = 0
    physical_expected: int = 0
    physical_caught: int = 0
    physical_false: int = 0  # 不该报物理异常却报了（不含 fold，fold 两种结果都允许）
    objective_total: int = 0
    objective_correct: int = 0  # 答案与状态都符合预期
    silent_errors: int = 0  # 状态 ok 但答案错
    normal_total: int = 0
    normal_correct: int = 0
    exam_digits_total: int = 0
    exam_digits_correct: int = 0
    barcode_total: int = 0
    barcode_correct: int = 0
    details: list[str] = field(default_factory=list)

    def report(self) -> dict:
        def r(a, b):
            return round(a / b, 5) if b else None
        return {
            "sheets": self.sheets,
            "physical_detect_rate": r(self.physical_caught, self.physical_expected),
            "physical_false": self.physical_false,
            "objective_accuracy": r(self.objective_correct, self.objective_total),
            "normal_accuracy": r(self.normal_correct, self.normal_total),
            "silent_errors": self.silent_errors,
            "exam_digit_accuracy": r(self.exam_digits_correct, self.exam_digits_total),
            "barcode_accuracy": r(self.barcode_correct, self.barcode_total),
        }


def compare(truth: dict, result: dict | None, physical: str | None, m: Metrics, tag: str) -> None:
    """result 为 result.json 内容；physical 为物理异常代码（此时 result 为 None）。"""
    m.sheets += 1
    if truth.get("physical"):
        m.physical_expected += 1
        if physical == truth["physical"]:
            m.physical_caught += 1
        else:
            m.details.append(f"{tag}: 期望物理异常 {truth['physical']}，实际 {physical}")
        return
    if physical:
        if truth.get("special") != "fold":
            m.physical_false += 1
            m.details.append(f"{tag}: 误报物理异常 {physical}")
        return
    got = {o["no"]: o for o in result["objective"]}
    for exp in truth["objective"]:
        o = got[exp["no"]]
        m.objective_total += 1
        ok_answer = o["answer"] == exp["answer"]
        # 浅涂：判为 uncertain 或（阈值偏低时）判为 ok 都可以，但答案必须对
        status_ok = o["status"] == exp["status"] or (exp["case"] == "light" and o["status"] == "ok")
        if ok_answer and status_ok:
            m.objective_correct += 1
        else:
            m.details.append(f"{tag} 第{exp['no']}题 {exp['case']}: 期望 {exp['answer']}/{exp['status']}，"
                             f"实际 {o['answer']}/{o['status']} fill={o['fill']}")
        if o["status"] == "ok" and not ok_answer:
            m.silent_errors += 1
        if exp["case"] == "normal":
            m.normal_total += 1
            m.normal_correct += int(ok_answer and o["status"] == "ok")
    te, re_ = truth.get("exam_no"), result["student"].get("exam_no")
    if te and re_:
        for a, b in zip(te["value"], re_["value"]):
            m.exam_digits_total += 1
            m.exam_digits_correct += int(a == b)
    if truth.get("barcode"):
        m.barcode_total += 1
        got_bc = result["student"]["barcodes"].get("student_barcode", {}).get("value")
        m.barcode_correct += int(got_bc == truth["barcode"])


def evaluate_dirs(truth_dir: str | Path, batch_dir: str | Path) -> Metrics:
    """truth_dir 为模拟测试集目录；batch_dir 为 recognize 输出的批次目录（含 manifest.json）。"""
    truth_dir, batch_dir = Path(truth_dir), Path(batch_dir)
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    m = Metrics()
    for item in manifest["sheets"]:
        truth = json.loads((truth_dir / f"{item['source']}.truth.json").read_text(encoding="utf-8"))
        result = None
        if item.get("result"):
            result = json.loads((batch_dir / item["result"]).read_text(encoding="utf-8"))
        compare(truth, result, item.get("physical"), m, item["source"])
    return m
