// 三个原型共用的侧栏、区域树、画布区域与状态栏，只有工具栏 / 操作区布局不同
const RAIL = `<div class="rail">
  <a><i></i>考试</a><a><i></i>扫描</a><a class="on"><i></i>模板</a><a><i></i>结果</a>
  <span class="grow"></span><a><i></i>设置</a></div>`;

const REGIONS = [
  ["loc", "定位块 1", 2.2, 2.6, 2.4, 2.6], ["loc", "定位块 2", 95.4, 2.6, 2.4, 2.6],
  ["loc", "定位块 3", 95.4, 94.6, 2.4, 2.6], ["loc", "定位块 4", 2.2, 94.6, 2.4, 2.6],
  ["id", "考号 15 位", 24.8, 15.2, 22.6, 18.6],
  ["obj sel", "1–20", 5.6, 42.4, 42.6, 14.2], ["obj", "21–40", 5.6, 58.2, 42.6, 12.6],
  ["obj", "41–55", 5.6, 73, 42.6, 12.6], ["sub", "写作 56", 53.6, 15.4, 42.6, 79.8],
];

function paper() {
  return `<div class="paper">${REGIONS.map(([k, t, x, y, w, h]) =>
    `<div class="rg ${k}" style="left:${x}%;top:${y}%;width:${w}%;height:${h}%"><b>${t}</b></div>`).join("")}</div>`;
}

function tree() {
  const g = (name, n, items, color) => `<div class="grp"><span>▾ ${name}</span><span>${n}</span></div>` +
    items.map((t, i) => `<div class="it${t.startsWith("*") ? " sel" : ""}"><span class="sw" style="background:${color}"></span>${t.replace("*", "")}</div>`).join("");
  return `<div class="tree"><div class="hd"><h2>区域</h2><span class="muted">9 个区域</span></div>${document.getElementById("ctl")?.innerHTML ?? ""}<div class="scroll">
    ${g("定位", 4, ["定位块 1", "定位块 2", "定位块 3", "定位块 4"], "var(--r-loc)")}
    ${g("考号", 1, ["考号 15 位"], "var(--r-id)")}
    ${g("客观题", 3, ["*1–20 单选", "21–40 单选", "41–55 单选"], "var(--r-obj)")}
    ${g("主观题", 1, ["写作 56"], "var(--r-sub)")}
  </div></div>`;
}

const STATUS = `<div class="status"><span>光标 x 124.8 mm，y 61.4 mm</span><span>正面定位点 4</span>
  <span>纸张 420 × 296.9 mm</span><span class="sp"></span><span>方向键微调 0.1 mm（Shift 1 mm）　空格拖动画布　Delete 删除</span></div>`;

function props() {
  return `<h2>客观题 1–20</h2><span class="muted">单选 · 20 题 × 4 选项</span>
    <div class="row"><span>题号</span><span>1 – 20</span></div>
    <div class="row"><span>题型</span><span>单选</span></div>
    <div class="row"><span>选项</span><span>A B C D</span></div>
    <div class="row"><span>排列</span><span>按行</span></div>
    <div class="row"><span>位置</span><span>23.5, 125.9 mm</span></div>`;
}

document.querySelectorAll("[data-slot]").forEach(el => {
  el.outerHTML = { rail: RAIL, tree: tree(), paper: paper(), status: STATUS, props: props() }[el.dataset.slot];
});
