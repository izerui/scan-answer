"""命令行入口：scan-answer synth | recognize | ocr | ocr-skip | eval | check | deliver"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from scan_answer.batch import ocr_progress_listener, result_ocr_progress, run_batch
from scan_answer.evaluate import evaluate_dirs
from scan_answer.ocr.config import load_ocr_config
from scan_answer.ocr.queue import OcrQueue, enable_ocr, reset_failed, skip_pending
from scan_answer.pipeline.events import FinalizedEvent, OcrProgress, SheetEvent
from scan_answer.pipeline.session import BatchSession
from scan_answer.output.audit import audit_batch
from scan_answer.output.delivery import export_delivery
from scan_answer.synth.generate import generate_dataset
from scan_answer.template import load_template

GATES = {"normal_accuracy": 0.999, "objective_accuracy": 0.999, "exam_digit_accuracy": 0.999,
         "physical_detect_rate": 1.0}


def default_workers() -> int:
    return max(0, min(4, (os.cpu_count() or 2) - 1))


def _print_event(e: object) -> None:
    if isinstance(e, SheetEvent):
        state = "成功" if e.ok else f"物理异常 {e.physical}"
        print(f"#{e.seq:04d} {e.source} {state} 警告 {e.warnings}", file=sys.stderr)
    elif isinstance(e, FinalizedEvent):
        label = "识别结算完成" if e.completed else "识别结算未完成"
        print(f"{label}：最终阈值 {e.mark:.3f}，重新判定后变化 {e.changed} 张", file=sys.stderr)
        for issue in e.issues:
            print(f"{'阈值提示' if e.completed else '未完成原因'}：{issue['msg']}", file=sys.stderr)
    elif isinstance(e, OcrProgress) and e.done + e.failed == e.total:
        print(f"手写转写：完成 {e.done}，失败 {e.failed}，共 {e.total}", file=sys.stderr)


def _run_ocr(batch: Path, cfg, retry_failed: bool, enable: bool) -> int:
    try:
        session = BatchSession.open(batch)  # 转写进度写入 manifest 的 ocr_progress
    except FileNotFoundError:
        session = None  # 没有模板快照的旧批次：只转写，不记录进度
    manifest = session.manifest if session else json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    paths = [batch / s["result"] for s in manifest["sheets"] if s.get("status") == "ok" and s.get("result")]
    q = OcrQueue(cfg, ocr_progress_listener(session, _print_event))
    try:
        for p in paths:
            if retry_failed:
                reset_failed(p)
            if enable:
                enable_ocr(p)
            q.enqueue(p)
        q.wait()
    finally:
        q.close()
    if session:
        progress = result_ocr_progress(session.batch_dir, list(session.sheets.values()))
        session.set_ocr_progress(progress.done, progress.failed, progress.total, force=True)
    print(f"手写转写：完成 {q.done}，失败 {q.failed}，共 {q.total}")
    return 1 if q.failed else 0


def _skip_ocr(batch: Path) -> int:
    """把批次中“等待转写”的项标为未启用（放弃转写），交付时不再阻断，交付记录逐项注明。"""
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    changed = busy = 0
    errors = []
    for s in manifest.get("sheets", []):
        if s.get("status") != "ok" or not s.get("result"):
            continue
        try:
            n, b = skip_pending(batch / s["result"], "操作员通过命令行放弃转写")
            changed += n
            busy += b
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            errors.append(f"#{int(s.get('seq', 0)):04d}: {error}")
    for line in errors:
        print(f"无法处理 {line}", file=sys.stderr)
    print(f"已把 {changed} 项等待转写标为未启用" + (f"，{busy} 项正在转写未改动" if busy else ""))
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scan-answer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("synth", help="以模板空白底图生成模拟测试集")
    s.add_argument("--template", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--count", type=int, default=50)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--dpi", type=int, default=300)

    r = sub.add_parser("recognize", help="识别图片、填写 PDF 或包含它们的文件夹")
    r.add_argument("--template", required=True)
    r.add_argument("--input", required=True, help="图片、PDF 文件或答卷文件夹")
    r.add_argument("--out", required=True)
    r.add_argument("--batch-id")
    r.add_argument("--exam", help="考试元信息 JSON 字符串")
    r.add_argument("--workers", type=int, default=default_workers(), help="识别进程数，0 表示在当前进程内顺序执行")
    r.add_argument("--ocr-config", help="手写转写配置 JSON 文件；不提供则只切图")

    o = sub.add_parser("ocr", help="对已识别批次执行（或重试）手写转写")
    o.add_argument("--batch", required=True)
    o.add_argument("--config", required=True)
    o.add_argument("--retry-failed", action="store_true", help="把失败的转写项改回待转写后重试")
    o.add_argument("--enable", action="store_true", help="批次识别时未开启转写，现在补做")

    k = sub.add_parser("ocr-skip", help="放弃未完成的转写：把等待转写的项标为未启用，使批次可以交付")
    k.add_argument("--batch", required=True)

    e = sub.add_parser("eval", help="对比识别结果与 truth.json，输出指标；不达标返回 1")
    e.add_argument("--truth", required=True)
    e.add_argument("--batch", required=True)

    check = sub.add_parser("check", help="只读检查批次的 JSON、题图和来源完整性")
    check.add_argument("--batch", required=True)
    deliver = sub.add_parser("deliver", help="生成经核验的固定 JSON＋图片交付目录")
    deliver.add_argument("--batch", required=True)
    deliver.add_argument("--out", required=True, help="新的目标目录，不能覆盖已有目录")

    a = ap.parse_args(argv)
    if a.cmd == "check":
        report = audit_batch(a.batch)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
        return 0 if report.ready else 1
    if a.cmd == "deliver":
        try:
            print(export_delivery(a.batch, a.out))
            return 0
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if a.cmd == "synth":
        t, bases = load_template(a.template)
        generate_dataset(t, bases, a.out, a.count, seed=a.seed, out_dpi=a.dpi)
        print(f"已生成 {a.count} 张模拟卡 -> {a.out}")
        return 0
    if a.cmd == "recognize":
        if a.exam:
            try:
                exam = json.loads(a.exam)
            except json.JSONDecodeError as exc:
                print(f"--exam 不是合法的 JSON 对象: {exc}", file=sys.stderr)
                return 2
            if not isinstance(exam, dict):
                print(f"--exam 不是合法的 JSON 对象: 期望对象，得到 {type(exam).__name__}", file=sys.stderr)
                return 2
        else:
            exam = {}
        cfg = load_ocr_config(a.ocr_config) if a.ocr_config else None
        bd = run_batch(a.template, a.input, a.out, exam=exam, batch_id=a.batch_id, workers=a.workers,
                       ocr_config=cfg, listener=_print_event)
        print(bd)
        manifest = json.loads((bd / "manifest.json").read_text(encoding="utf-8"))
        return 0 if manifest.get("finalized") else 1
    if a.cmd == "ocr-skip":
        try:
            return _skip_ocr(Path(a.batch))
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if a.cmd == "ocr":
        try:
            return _run_ocr(Path(a.batch), load_ocr_config(a.config), a.retry_failed, a.enable)
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
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
