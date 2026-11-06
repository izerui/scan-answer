def test_199_template_content(tpl_bases):
    t, bases = tpl_bases
    assert t.name == "管综199" and t.paper == (390.0, 270.0) and t.dpi == 300
    assert t.faces["front"].align.method == "anchors" and len(t.faces["front"].align.anchors) == 4
    assert t.faces["back"].align.method == "frames" and len(t.faces["back"].align.frames) == 2
    assert len(t.exam_no.grid.cells) == 15 and len(t.exam_no.grid.cells[0]) == 10
    assert [b.name for b in t.objective] == ["1-15", "16-30", "31-45", "46-55"]
    assert sum(len(b.grid.cells) for b in t.objective) == 55
    assert [s.no for s in t.subjective] == ["56", "57"]
    assert {r.face for r in t.subjective[0].regions} == {"front", "back"}
    assert bases["front"].shape == (3189, 4606) and bases["back"].shape == (3189, 4606)
