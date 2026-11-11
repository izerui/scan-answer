// 扫描页三版原型共用的数据与片段：只有按钮 / 功能区的摆放不同，内容一致
const RAIL = `<div class="rail">
  <a><i></i>考试</a><a class="on"><i></i>扫描</a><a><i></i>模板</a><a><i></i>结果</a>
  <span class="grow"></span><a><i></i>设置</a></div>`;

const STATUS = `<div class="status"><span>扫描仪模式</span><span>考试：2026 考研初试 · 管理类联考综合能力（199）</span>
  <span>批次 batch_20261111_093412</span><span>转写 86 / 122</span><span class="sp"></span>
  <span style="color:var(--ink)">使用说明（F1）</span><span>v0.3.1</span></div>`;

const sw = c => `<span class="sw" style="background:${c}"></span>`;

function tiles() {
  const t = (k, v, n, cls = "", key = "", unit = "") =>
    `<div class="tile ${cls}"><div class="k">${key}${k}</div><div class="v">${v}${unit ? `<small>${unit}</small>` : ""}</div><div class="n">${n}</div></div>`;
  return `<div class="tiles">
    ${t("已扫描", 126, "张，正反面已配对")}${t("成功", 122, "可输出")}
    ${t("物理异常", 3, "待处理", "err", sw("var(--err)"))}${t("数据警告", 6, "已标记 · 5 张卡", "warn", sw("var(--warn)"))}
    ${t("速度", 38, "最近一分钟", "", "", "张/分")}${t("手写转写", 86, "后台进行中", "ocr", sw("#f5b971"), "/ 122")}</div>`;
}

// 1–126 已出结果，127–130 识别中，其余待扫
const ERRS = { 47: 1, 83: 1, 112: 1 }, WARNS = { 17: 1, 34: 1, 58: 1, 91: 1, 120: 1 }, VOIDS = { 9: 1 };
function mosaic(cols, rows = 3) {
  let cells = "", rn = "";
  for (let r = 0; r < rows; r++) rn += `<i>${r * cols + 1}</i>`;
  for (let i = 1; i <= cols * rows; i++) {
    const c = ERRS[i] ? "err" : WARNS[i] ? "warn" : VOIDS[i] ? "void" : i <= 126 ? "ok" : i <= 130 ? "run" : "empty";
    cells += `<span class="cell ${c}${i === 126 ? " cur" : ""}" title="#${String(i).padStart(4, "0")}"></span>`;
  }
  return `<div class="mrow"><div class="rn">${rn}</div><div class="mosaic" style="grid-template-columns:repeat(${cols},12px)">${cells}</div></div>`;
}

const MLEGEND = `<div class="mlegend">
  <span><span class="cell ok"></span>成功</span><span><span class="cell warn"></span>数据警告</span>
  <span><span class="cell err"></span>物理异常</span><span><span class="cell run"></span>识别中</span>
  <span><span class="cell void"></span>已作废</span><span><span class="cell empty"></span>待扫</span></div>`;

// 区域色码（原左下角常驻图例的全部 8 项）
const REGION_ITEMS = [["#1d5bbf", "考号"], ["#15803d", "客观题"], ["#b45309", "主观题"], ["#6b3fb0", "条码"],
  ["#15181c;outline:1px solid #888", "定位点 / 同步头"], ["#4e5a66", "信息栏 / 缺考"], ["var(--err)", "物理异常"], ["var(--warn)", "数据警告"]];
const REGION_LEGEND = `<div class="legend2">${REGION_ITEMS.map(([c, t]) => `<span>${sw(c)}${t}</span>`).join("")}</div>`;

function excCard(o) {
  const b = (t, cls = "", tip = "") => `<button class="sm ${cls}" title="${tip}">${t}</button>`;
  const row1 = o.voided ? b("重扫", "", "重新扫描这张卡") + b("恢复", "", "撤销作废，恢复为物理异常")
    : b("重扫", "", "重新扫描这张卡：扫描仪模式下把它单独放进进纸器扫") + b("作废", "danger", "不要这张卡：不输出结果，也不再挡住结束批次");
  const row2 = (o.more || []).map(t => b(t, "", {
    "重新识别": "不用重扫：用已保存的原图再自动识别一次", "手动定位": "在原图上依次点出定位块，按你点的位置重新识别",
    "确认无误": "核对原图后确认不是混卡，跳过标识检查按本模板重新识别" }[t])).join("");
  return `<div class="exc${o.voided ? " voided" : ""}${o.sel ? " sel" : ""}"><div class="top"><div class="thumb"></div><div>
    <div class="no">#${o.seq}${o.voided ? "　已作废" : ""}</div><div class="why">${o.why}</div><div class="msg">${o.msg}</div></div></div>
    <div class="acts">${row1}${o.inline ? row2 : ""}</div>${!o.inline && row2 ? `<div class="acts">${row2}</div>` : ""}
    ${o.note ? `<div class="msg">${o.note}</div>` : ""}</div>`;
}
const EXCS = [
  { seq: "0047", why: "定位失败", msg: "正面四角定位块只找到 2 个", more: ["重新识别", "手动定位"], sel: true },
  { seq: "0083", why: "混卡（卡面与模板不符）", msg: "标识区读到“英语（一）”，与本模板不符", more: ["确认无误", "手动定位"] },
  { seq: "0112", why: "缺少一面", msg: "进纸器最后一页没有背面", more: [] },
];
const VOIDED = { seq: "0009", why: "空白页", msg: "正反面都没有涂写", voided: true };
const excList = (inline = false) => EXCS.map(e => excCard({ ...e, inline })).join("");
const voidCard = () => excCard(VOIDED);

const WARN_LIST = `<div class="wl">
  <div><span class="m">■</span> <b>#0017</b>　2 条警告</div><div><span class="m">■</span> <b>#0034</b>　1 条警告</div>
  <div><span class="m">■</span> <b>#0058</b>　1 条警告</div><div><span class="m">■</span> <b>#0091</b>　1 条警告</div>
  <div><span class="m">■</span> <b>#0120</b>　1 条警告</div></div>`;

const CARD = `<div class="card">
  <div class="rg loc" style="left:2.2%;top:2.6%;width:2.4%;height:2.6%"></div><div class="rg loc" style="left:95.4%;top:2.6%;width:2.4%;height:2.6%"></div>
  <div class="rg loc" style="left:95.4%;top:94.6%;width:2.4%;height:2.6%"></div><div class="rg loc" style="left:2.2%;top:94.6%;width:2.4%;height:2.6%"></div>
  <div class="rg id" style="left:24.8%;top:15.2%;width:22.6%;height:18.6%"></div>
  <div class="rg obj" style="left:5.6%;top:42.4%;width:42.6%;height:14.2%"></div><div class="rg obj" style="left:5.6%;top:58.2%;width:42.6%;height:12.6%"></div>
  <div class="rg obj" style="left:5.6%;top:73%;width:42.6%;height:12.6%"></div><div class="rg sub" style="left:53.6%;top:15.4%;width:42.6%;height:79.8%"></div></div>`;

const READOUT = `<div class="readout">
  ${sw("#1d5bbf")}<span class="k">考号</span><span class="v">100246120310057</span>
  ${sw("#6b3fb0")}<span class="k">条码</span><span class="v">—</span>
  ${sw("#4e5a66")}<span class="k">缺考</span><span class="v">未涂</span>
  ${sw("#15803d")}<span class="k">客观题</span><span class="v">25 / 25</span>
  ${sw("#b45309")}<span class="k">主观题</span><span class="v">切图 2，待转写</span>
  ${sw("#15181c;outline:1px solid #888")}<span class="k">定位</span><span class="v">定位块，倾斜 0.4°</span></div>`;

const SCANNER_TIPS = { dpi: "300 推荐：速度和清晰度兼顾" };

function fillSlots() {
  document.querySelectorAll("[data-slot]").forEach(el => {
    const [k, a] = el.dataset.slot.split(":");
    const map = { rail: RAIL, status: STATUS, tiles: tiles(), mosaic: () => mosaic(+a || 46, 3), mlegend: MLEGEND,
      region: REGION_LEGEND, exc: () => excList(a === "inline"), void: voidCard(), warns: WARN_LIST, card: CARD, readout: READOUT };
    const v = map[k];
    el.outerHTML = typeof v === "function" ? v() : v;
  });
}
// 点按钮展开/收起浮层
function pop(btnSel, popSel, place = "below") {
  const btn = document.querySelector(btnSel), p = document.querySelector(popSel);
  if (!btn || !p) return;
  btn.addEventListener("click", e => {
    e.stopPropagation();
    const open = !p.classList.contains("open");
    document.querySelectorAll(".pop").forEach(m => m.classList.remove("open"));
    if (!open) return;
    p.classList.add("open");
    const r = btn.getBoundingClientRect();
    p.style.left = Math.max(8, Math.min(r.right - p.offsetWidth, innerWidth - p.offsetWidth - 8)) + "px";
    p.style.top = (place === "above" ? r.top - p.offsetHeight - 6 : r.bottom + 6) + "px";
  });
}
document.addEventListener("click", e => { if (!e.target.closest(".pop")) document.querySelectorAll(".pop").forEach(m => m.classList.remove("open")); });
