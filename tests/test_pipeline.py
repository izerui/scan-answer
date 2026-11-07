import json
import threading

import numpy as np
import pytest

from scan_answer.engine.omr import Thresholds
from scan_answer.output.jsonfile import read_json, update_json
from scan_answer.pipeline import session as session_mod
from scan_answer.pipeline.events import DeviceNotice, FinalizedEvent, SheetEvent, ThresholdEvent, VoidEvent
from scan_answer.pipeline import runner as runner_mod
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.source.base import DeviceEvent, SheetImages
from scan_answer.source.folder import FolderSource
from scan_answer.synth.generate import generate_dataset, make_scans, make_sheet

from conftest import TEMPLATE_DIR


class ListSource:
    def __init__(self, items):
        self.items = items

    def sheets(self):
        yield from self.items


def _sheet(ctx, seed, special=None, source="s"):
    rng = np.random.default_rng(seed)
    paper, truth = make_sheet(ctx.tpl, ctx.bases, rng)
    scans = make_scans(ctx.tpl, paper, truth, rng, 300, special)
    return SheetImages(source=source, names=[f"{source}_{i + 1}.jpg" for i in range(len(scans))],
                       dpis=[300.0] * len(scans), images=scans), truth


def test_run_inline_records_events_and_manifest(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 5, seed=31)
    events = []
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b", exam={"name": "测试"})
    with BatchPipeline(s, listener=events.append) as p:
        p.run(FolderSource(tmp_path / "ds", s.faces))
    sheet_events = [e for e in events if isinstance(e, SheetEvent)]
    assert [e.seq for e in sheet_events] == [1, 2, 3, 4, 5]
    m = read_json(s.batch_dir / "manifest.json")
    assert m["counts"]["total"] == 5 and m["counts"]["ok"] + m["counts"]["physical"] == 5
    assert m["finalized"] is False and m["exam"] == {"name": "测试"}
    ok = [x for x in m["sheets"] if x["status"] == "ok"]
    assert ok and all((s.batch_dir / x["result"]).exists() for x in ok)
    assert all((s.batch_dir / "state" / f"{x['seq']:04d}.json").exists() for x in ok)
    assert len(list((s.batch_dir / "raw").iterdir())) == 10


def test_process_pool_matches_inline(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 4, seed=32)
    answers = {}
    for workers in (0, 2):
        s = BatchSession.create(tmp_path / f"out{workers}", TEMPLATE_DIR, batch_id="b")
        with BatchPipeline(s, workers=workers) as p:
            p.run(FolderSource(tmp_path / "ds", s.faces))
        m = read_json(s.batch_dir / "manifest.json")
        answers[workers] = {x["seq"]: [(o["no"], o["answer"], o["status"]) for o in
                                       read_json(s.batch_dir / x["result"])["objective"]]
                            for x in m["sheets"] if x["status"] == "ok"}
    assert answers[0] == answers[2] and answers[0]


def test_refit_threshold_event(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "REFIT_MIN", 2)
    monkeypatch.setattr(session_mod, "REFIT_EVERY", 2)
    events = []
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s, listener=events.append) as p:
        p.run(ListSource([_sheet(ctx, 40 + i, source=f"s{i}")[0] for i in range(4)]))
    th = [e for e in events if isinstance(e, ThresholdEvent)]
    assert [e.sheets for e in th] == [2, 4]
    assert 0.3 <= th[-1].mark <= 0.6


def test_device_event_and_stop(ctx, tmp_path):
    events = []
    stop = threading.Event()
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")

    def gen():
        yield _sheet(ctx, 50, source="a")[0]
        yield DeviceEvent("double_feed", "检测到双张")
        stop.set()
        yield _sheet(ctx, 51, source="b")[0]

    class GenSource:
        def sheets(self):
            return gen()

    with BatchPipeline(s, listener=events.append) as p:
        p.run(GenSource(), stop=stop)
    notices = [e for e in events if isinstance(e, DeviceNotice)]
    assert notices == [DeviceNotice("double_feed", "检测到双张", 1)]
    m = read_json(s.batch_dir / "manifest.json")
    assert [x["seq"] for x in m["sheets"]] == [1]  # stop 之后的卡没有处理
    assert m["events"][0]["kind"] == "double_feed" and m["events"][0]["after_seq"] == 1


def test_rescan_replaces_physical_error_and_void(ctx, tmp_path):
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    bad, _ = _sheet(ctx, 60, "blank", source="x")
    bad2, _ = _sheet(ctx, 61, "blank", source="y")
    with BatchPipeline(s) as p:
        p.run(ListSource([bad, bad2]))
        assert s.sheets[1]["status"] == "physical" and (s.batch_dir / "errors" / "0001_BLANK_PAGE").exists()
        good, truth = _sheet(ctx, 62, source="x-rescan")
        ev = p.rescan(1, good)
        assert ev.seq == 1 and ev.ok and ev.rescan
        assert not (s.batch_dir / "errors" / "0001_BLANK_PAGE").exists()
        assert sorted(x.name for x in (s.batch_dir / "raw").glob("0001_*")) == ["0001_x-rescan_1.jpg", "0001_x-rescan_2.jpg"]
        r = read_json(s.batch_dir / s.sheets[1]["result"])
        assert r["seq"] == 1 and r["student"]["exam_no"]["value"] == truth["exam_no"]["value"]
        events = []
        p.listener = events.append
        p.void(2)
        assert events == [VoidEvent(2)]
        with pytest.raises(ValueError):
            p.void(1)
    m = read_json(s.batch_dir / "manifest.json")
    assert m["counts"] == {"total": 2, "ok": 1, "physical": 0, "voided": 1, "warnings": m["counts"]["warnings"]}


def test_finalize_redecides_and_keeps_ocr_text(ctx, tmp_path):
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    s.thresholds = Thresholds(mark=0.97, uncertain=0.5)  # 扫描时阈值极端偏高 → 大量题目 uncertain
    events = []
    with BatchPipeline(s, listener=events.append) as p:
        p.run(ListSource([_sheet(ctx, 70 + i, source=f"s{i}")[0] for i in range(3)]))
        path = s.batch_dir / s.sheets[1]["result"]
        before = read_json(path)
        assert sum(o["status"] == "uncertain" for o in before["objective"]) > 20
        update_json(path, lambda r: r["subjective"][0].update(text="已转写", ocr_status="done"))
        fe = p.finalize()
    assert isinstance(fe, FinalizedEvent) and fe.changed == 3 and events[-1] == fe
    after = read_json(path)
    assert sum(o["status"] == "uncertain" for o in after["objective"]) < 5
    assert after["subjective"][0]["text"] == "已转写" and after["subjective"][0]["ocr_status"] == "done"
    assert after["scan"]["time"] == before["scan"]["time"]
    bad_now = {o["no"] for o in after["objective"] if o["status"] != "ok"}
    imgs = {p.stem.split("_")[1] for p in (path.parent / "images").glob("obj_*.jpg")}
    assert imgs == bad_now
    m = read_json(s.batch_dir / "manifest.json")
    assert m["finalized"] is True and abs(m["thresholds"]["mark"] - fe.mark) < 1e-3
    # 再次结束批次：阈值不变 → 不再改写
    with BatchPipeline(BatchSession.open(s.batch_dir, TEMPLATE_DIR)) as p2:
        assert p2.finalize().changed == 0


def test_open_continues_numbering(ctx, tmp_path):
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s) as p:
        p.run(ListSource([_sheet(ctx, 80, source="a")[0]]))
    s2 = BatchSession.open(s.batch_dir, TEMPLATE_DIR)
    assert s2.next_seq() == 2 and len(s2.fills) == 1
    with BatchPipeline(s2) as p:
        p.run(ListSource([_sheet(ctx, 81, source="b")[0]]))
    assert [x["seq"] for x in read_json(s.batch_dir / "manifest.json")["sheets"]] == [1, 2]


def test_create_refuses_non_empty_dir(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x").write_text("1")
    with pytest.raises(FileExistsError):
        BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")


def test_worker_crash_records_physical_error(ctx, tmp_path, monkeypatch):
    """process_sheet 抛异常时，该卡记录为物理异常，其余卡正常处理。"""
    real_process = runner_mod.process_sheet

    def boom(job):
        if job.seq == 2:
            raise RuntimeError("模拟崩溃")
        return real_process(job)

    monkeypatch.setattr(runner_mod, "process_sheet", boom)
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    events = []
    with BatchPipeline(s, listener=events.append) as p:
        p.run(ListSource([_sheet(ctx, 90 + i, source=f"s{i}")[0] for i in range(3)]))
    m = read_json(s.batch_dir / "manifest.json")
    seq2 = next(x for x in m["sheets"] if x["seq"] == 2)
    assert seq2["status"] == "physical" and seq2["physical"] == "ALIGN_FAILED"
    assert "内部错误" in seq2["message"]
    ok_seqs = sorted(x["seq"] for x in m["sheets"] if x["status"] == "ok")
    assert ok_seqs == [1, 3]


def test_finalize_crash_records_event(ctx, tmp_path, monkeypatch):
    """finalize_sheet 抛异常时，finalize 仍完成，manifest 记录错误事件。"""
    real_finalize = runner_mod.finalize_sheet

    def boom(job):
        if job.seq == 1:
            raise RuntimeError("模拟重新判定崩溃")
        return real_finalize(job)

    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s) as p:
        p.run(ListSource([_sheet(ctx, 95 + i, source=f"s{i}")[0] for i in range(2)]))
    monkeypatch.setattr(runner_mod, "finalize_sheet", boom)
    with BatchPipeline(s) as p:
        fe = p.finalize()
    m = read_json(s.batch_dir / "manifest.json")
    assert m["finalized"] is True
    error_events = [e for e in m["events"] if e["kind"] == "error"]
    assert any("重新判定失败 #1" in e["message"] for e in error_events)


def test_register_forces_manifest_save(ctx, tmp_path):
    """登记一张卡后立即写盘（节流期内也写），防止崩溃后序号复用。"""
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    s.register(1, _sheet(ctx, 100, source="a")[0])
    m = read_json(s.batch_dir / "manifest.json")
    assert [(x["seq"], x["status"]) for x in m["sheets"]] == [(1, "processing")]


def test_open_reconciles_after_crash(ctx, tmp_path):
    """崩溃后 manifest 落后于磁盘：open() 依据磁盘恢复条目，序号不复用。"""
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s) as p:
        p.run(ListSource([_sheet(ctx, 101, source="a")[0], _sheet(ctx, 102, "blank", source="b")[0],
                          _sheet(ctx, 103, source="c")[0]]))
    m = read_json(s.batch_dir / "manifest.json")
    ok1 = dict(next(x for x in m["sheets"] if x["seq"] == 1))
    assert ok1["status"] == "ok" and m["sheets"][1]["status"] == "physical"
    # 模拟第 3 张识别中途崩溃：只留下原图
    from scan_answer.pipeline.worker import clear_outputs
    clear_outputs(s.batch_dir, 3)
    # 模拟 manifest 只写到第 1 张登记时（processing），第 2、3 张没有记录
    m["sheets"] = [{"seq": 1, "source": "a", "files": ok1["files"], "status": "processing"}]
    (s.batch_dir / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    s2 = BatchSession.open(s.batch_dir, TEMPLATE_DIR)
    assert s2.next_seq() == 4
    a, b, c = s2.sheets[1], s2.sheets[2], s2.sheets[3]
    assert a["status"] == "ok" and a["result"] == ok1["result"] and a["exam_no"] == ok1["exam_no"]
    assert a["warnings"] == ok1["warnings"] and 1 in s2.fills
    assert b["status"] == "physical" and b["physical"] == "BLANK_PAGE" and b["source"] == "b"
    assert b["files"] == ["b_1.jpg", "b_2.jpg"]
    assert c["status"] == "physical" and c["physical"] == "ALIGN_FAILED" and "识别中断" in c["message"]
    assert c["source"] == "c"
    disk = read_json(s.batch_dir / "manifest.json")
    assert disk["counts"] == {"total": 3, "ok": 1, "physical": 2, "voided": 0, "warnings": ok1["warnings"]}
    with BatchPipeline(s2) as p:
        p.run(ListSource([_sheet(ctx, 104, source="d")[0]]))
    assert [x["seq"] for x in read_json(s.batch_dir / "manifest.json")["sheets"]] == [1, 2, 3, 4]


def test_rescan_only_physical_or_voided(ctx, tmp_path):
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s) as p:
        p.run(ListSource([_sheet(ctx, 110, source="a")[0], _sheet(ctx, 111, "blank", source="b")[0]]))
        assert s.sheets[1]["status"] == "ok"
        with pytest.raises(ValueError, match="只能重扫物理异常或已作废的卡"):
            p.rescan(1, _sheet(ctx, 112, source="a2")[0])
        assert s.sheets[1]["status"] == "ok" and (s.batch_dir / s.sheets[1]["result"]).exists()
        p.void(2)
        ev = p.rescan(2, _sheet(ctx, 113, source="b2")[0])
        assert ev.ok and s.sheets[2]["status"] == "ok" and "voided" not in s.sheets[2]


class _BreakingExecutor(runner_mod._InlineExecutor):
    """替身执行器：对 break_seq 的任务返回 BrokenProcessPool，之后的 submit 也抛 BrokenProcessPool。"""

    def __init__(self, break_seq):
        self.break_seq = break_seq
        self.broken = False

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future
        from concurrent.futures.process import BrokenProcessPool
        if self.broken:
            raise BrokenProcessPool("进程池已损坏")
        if args[0].seq == self.break_seq:
            self.broken = True
            f = Future()
            f.set_exception(BrokenProcessPool("识别进程异常退出"))
            return f
        return super().submit(fn, *args, **kwargs)


def _breaking_pipeline(session, break_seq, workers):
    class P(BatchPipeline):
        made = 0

        def _make_executor(self):
            P.made += 1
            return _BreakingExecutor(break_seq) if P.made == 1 else runner_mod._InlineExecutor()

    return P(session, workers=workers), P


def test_broken_process_pool_is_rebuilt(ctx, tmp_path):
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    p, cls = _breaking_pipeline(s, 2, workers=2)  # 在途窗口 4：3 张卡先全部提交再收集
    with p:
        p.run(ListSource([_sheet(ctx, 120 + i, source=f"s{i}")[0] for i in range(3)]))
    assert cls.made == 2  # 只重建一次
    assert [s.sheets[k]["status"] for k in (1, 2, 3)] == ["ok", "ok", "ok"]
    # 结束批次同理
    p2, cls2 = _breaking_pipeline(s, 1, workers=2)
    with p2:
        fe = p2.finalize()
    assert cls2.made == 2 and fe is not None
    m = read_json(s.batch_dir / "manifest.json")
    assert m["finalized"] is True and not [e for e in m["events"] if e["kind"] == "error"]


def test_recognition_uses_batch_template_snapshot(ctx, tmp_path):
    """创建批次时复制整个模板；之后原模板被改坏，识别与重开批次仍使用快照。"""
    import shutil
    tpl = tmp_path / "tpl"
    shutil.copytree(TEMPLATE_DIR, tpl)
    s = BatchSession.create(tmp_path / "out", tpl, batch_id="b")
    snap = s.batch_dir / "template"
    assert s.template_dir == snap and (snap / "template.json").exists()
    assert (snap / "front.png").exists() and (snap / "back.png").exists()
    assert (s.batch_dir / "template.json").exists()
    assert read_json(s.batch_dir / "manifest.json")["template"]["snapshot"] == "template/template.json"
    (tpl / "template.json").write_text("{坏了", encoding="utf-8")
    (tpl / "front.png").unlink()
    with BatchPipeline(s) as p:
        p.run(ListSource([_sheet(ctx, 130, source="a")[0]]))
    assert s.sheets[1]["status"] == "ok"
    s2 = BatchSession.open(s.batch_dir)  # 不传模板目录
    assert s2.template_dir == snap
    with BatchPipeline(s2) as p:
        p.run(ListSource([_sheet(ctx, 131, source="b")[0]]))
        p.finalize()
    assert s2.sheets[2]["status"] == "ok"


def test_open_old_batch_without_snapshot(ctx, tmp_path):
    import shutil
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    shutil.rmtree(s.batch_dir / "template")  # 模拟没有快照的旧批次
    with pytest.raises(FileNotFoundError):
        BatchSession.open(s.batch_dir)
    assert BatchSession.open(s.batch_dir, TEMPLATE_DIR).template_dir == TEMPLATE_DIR


def test_finalize_warnings_exclude_ocr_failed(ctx, tmp_path):
    """结束批次返回的警告数只计识别警告，不含 OCR_FAILED（结果变化与不变两种情况）。"""
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    s.thresholds = Thresholds(mark=0.97, uncertain=0.5)
    with BatchPipeline(s) as p:
        p.run(ListSource([_sheet(ctx, 140, source="a")[0]]))
    path = s.batch_dir / s.sheets[1]["result"]
    ocr_w = [{"code": "OCR_FAILED", "field": f"subjective.{i}", "msg": "x"} for i in range(3)]
    update_json(path, lambda r: r["warnings"].extend(ocr_w))

    def check(sess):
        r = read_json(path)
        assert sum(w["code"] == "OCR_FAILED" for w in r["warnings"]) == 3
        assert sess.sheets[1]["warnings"] == len(r["warnings"]) - 3

    with BatchPipeline(s) as p:
        assert p.finalize().changed == 1
    check(s)
    s2 = BatchSession.open(s.batch_dir)
    with BatchPipeline(s2) as p:
        assert p.finalize().changed == 0
    check(s2)
