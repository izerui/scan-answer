"""命令行入口：scan-answer synth | recognize | eval"""
from __future__ import annotations

import argparse
import json
import sys

from .batch import run_batch
from .evaluate import evaluate_dirs
from .synth.generate import generate_dataset
from .template import load_template

GATES = {"normal_accuracy": 0.999, "objective_accuracy": 0.999, "exam_digit_accuracy": 0.999,
         "physical_detect_rate": 1.0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scan-answer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("synth", help="以模板空白底图生成模拟测试集")
    s.add_argument("--template", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--count", type=int, default=50)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--dpi", type=int, default=300)

    r = sub.add_parser("recognize", help="识别一个文件夹里的扫描图")
    r.add_argument("--template", required=True)
    r.add_argument("--input", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--batch-id")
    r.add_argument("--exam", help="考试元信息 JSON 字符串")

    e = sub.add_parser("eval", help="对比识别结果与 truth.json，输出指标；不达标返回 1")
    e.add_argument("--truth", required=True)
    e.add_argument("--batch", required=True)

    a = ap.parse_args(argv)
    if a.cmd == "synth":
        t, bases = load_template(a.template)
        generate_dataset(t, bases, a.out, a.count, seed=a.seed, out_dpi=a.dpi)
        print(f"已生成 {a.count} 张模拟卡 -> {a.out}")
        return 0
    if a.cmd == "recognize":
        exam = json.loads(a.exam) if a.exam else {}
        bd = run_batch(a.template, a.input, a.out, exam=exam, batch_id=a.batch_id)
        print(bd)
        return 0
    m = evaluate_dirs(a.truth, a.batch)
    rep = m.report()
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    for line in m.details[:50]:
        print(" -", line)
    failed = [k for k, v in GATES.items() if rep.get(k) is not None and rep[k] < v]
    if rep["silent_errors"] or rep["physical_false"]:
        failed.append("silent_errors/physical_false")
    if failed:
        print("未达标:", ", ".join(failed))
        return 1
    print("全部达标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
