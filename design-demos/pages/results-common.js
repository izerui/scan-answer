// 结果页三版原型共用的数据与片段（学生列表、卡面叠层、答案表、主观题、交付明细）；三版只有按钮 / 功能区的摆放不同
const R = {};

R.rail = `<div class="rail">
  <a><i></i>考试</a><a><i></i>扫描</a><a><i></i>模板</a><a class="on"><i></i>结果</a>
  <span class="grow"></span><a><i></i>设置</a></div>`;

R.status = `<div class="status"><span>文件导入模式</span><span>考研管理类综合能力 2026</span><span>无进行中的批次</span>
  <span>转写已开启</span><span class="sp"></span><span>使用说明（F1）</span><span>v0.3.1</span></div>`;

R.batch = "第 2 批 · 0526_上午";
R.exam = "考研管理类综合能力 2026";

// 序号、考号、警告列、类型（ok / warn / void）
R.rows = [
  [1, "100015000123441", ""], [2, "100015000123442", ""], [3, "100015000123443", ""], [4, "100015000123444", ""],
  [5, "100015000123445", ""], [6, "100015000123447", ""], [7, "100015000123456", "2 / 转写失败 1", "warn"],
  [8, "100015000123449", ""], [9, "已作废", "", "void"], [10, "100015000123451", ""], [11, "100015000123452", ""],
  [12, "100015000123453", ""], [13, "100015000123454", ""], [14, "100015000123455", "待转写 1", "warn"],
  [15, "100015000123457", ""], [16, "100015000123458", ""],
];
R.list = (sel = 7) => `<div class="slist"><div class="th"><span>序号</span><span>考号</span><span>警告</span></div>${
  R.rows.map(([s, no, w, k]) => `<div class="tr${s === sel ? " sel" : ""}${k === "void" ? " void" : ""}">
    <span>#${String(s).padStart(4, "0")}</span><span>${no}</span><span class="w ${k === "warn" ? "warn" : ""}">${w}</span></div>`).join("")}</div>`;

// 卡面上实际涂的答案（从 card-filled.svg 读出）：12 题多涂 A、C，37 题 C 涂得浅
const ANS = {}; const PAT = "AEDCB";
for (let q = 1; q <= 55; q++) ANS[q] = PAT[(q - 1) % 5];
ANS[12] = "AC"; ANS[37] = "E";
const COLX = [125, 309, 493, 669], ROWY = [524, 704, 884];
function bubble(q, opt) {
  const i = q - 1, c = Math.floor(i / 15), g = Math.floor((i % 15) / 5), r = i % 5;
  return [COLX[c] + "ABCDE".indexOf(opt) * 30.3, ROWY[g] + r * 28];
}
const box = (x, y, w, h, cls, label = "") =>
  `<div class="ov ${cls}" style="left:${x / 16.8}%;top:${y / 11.88}%;width:${w / 16.8}%;height:${h / 11.88}%">${label ? `<b>${label}</b>` : ""}</div>`;
R.paper = () => {
  let s = box(418, 178, 376, 224, "b", "考号 100015000123456") + box(570, 418, 34, 20, "b");
  for (let q = 1; q <= 55; q++) for (const o of ANS[q]) {
    const [x, y] = bubble(q, o);
    s += box(x - 12, y - 8, 24, 16, q === 12 ? "r" : "g");
  }
  const [x, y] = bubble(37, "C"); s += box(x - 13, y - 9, 26, 18, "r", "37 存疑");
  const [x2, y2] = bubble(12, "A"); s += `<div class="ov r" style="left:${(x2 - 16) / 16.8}%;top:${(y2 - 11) / 11.88}%;width:${(30.3 * 2 + 32) / 16.8}%;height:${22 / 11.88}%"><b>12 多涂</b></div>`;
  return `<div class="paper2">${s}</div>`;
};
R.legend = `<span class="legend" title="卡面上的彩色框是程序识别到的位置；红框处要对照原图核对">叠层：
  <span><span class="sw" style="background:#15803d"></span> 识别选项</span><span><span class="sw" style="background:#1d5bbf"></span> 考号 / 填涂字段</span>
  <span><span class="sw" style="background:#b45309"></span> 选做组</span><span><span class="sw" style="background:#cc2f1d"></span> 异常</span></span>`;
R.anoms = `<div class="anoms"><div><span class="sq">■</span>第 12 题多涂</div><div><span class="sq">■</span>第 37 题涂得浅或擦除不净</div>
  <div><span class="sq">■</span>转写失败 1 项</div></div>`;
R.info = "填涂考号 100015000123456　条码 100015000123456　缺考 否　姓名 张明远";

// 客观题答案表
const FILL = q => (0.78 + ((q * 37) % 17) / 100).toFixed(2);
R.answers = (from = 1, n = 12) => {
  let rows = "";
  for (let q = from; q < from + n && q <= 55; q++) {
    let a = ANS[q], st = "正常", f = `${a} ${FILL(q)}`, bad = false;
    if (q === 12) { a = "—"; st = "多涂"; f = "A 0.84 / C 0.79"; bad = true; }
    if (q === 37) { st = "存疑"; f = "C 0.31 / E 0.88"; bad = true; }
    rows += `<div class="tr${bad ? " bad" : ""}"><span>${q}</span><span>${a}</span><span>${st}</span><span>${f}</span></div>`;
  }
  return `<div class="atable"><div class="th"><span>题号</span><span>答案</span><span>状态</span><span>涂黑度</span></div><div class="rows">${rows}</div></div>`;
};

// 信息栏与主观题条目（代码中主观题在前、信息栏在后）
R.subj = () => `
  <div class="subj"><div class="t">第 56 题（写作）<span class="tag bad">转写失败</span></div>
    <div class="txt st">转写失败：请求超时（60 秒），模型服务没有响应</div>
    <div class="ops"><button title="在“证据 / JSON”页查看该区域的切图">查看区域 1</button><button title="重新转写这一项（失败原因见上方）">重试转写</button></div></div>
  <div class="subj"><div class="t">信息栏 报考单位</div><div class="txt">北京理工大学</div>
    <div class="ops"><button title="在“证据 / JSON”页查看该区域的切图">查看图片</button></div></div>
  <div class="subj"><div class="t">信息栏 考生姓名</div><div class="txt">张明远</div>
    <div class="ops"><button title="在“证据 / JSON”页查看该区域的切图">查看图片</button></div></div>`;

// 交付检查结果：1 个阻断项、2 个说明项
R.verdict = "暂不可交付 · 1 项阻断 · 2 项说明";
R.blockers = [["#0014", "文字转写尚未完成：第 56 题（写作）", "在“核对答卷”右栏点“转写剩余内容”，或“不再转写剩余项…”"]];
R.notes = [["#0007", "文字转写失败：第 56 题（写作）；请求超时（60 秒）", "不阻断交付，会写入交付记录"],
  ["#0009", "已作废答卷明确排除：重复扫描，作废多余的一份", "不阻断交付，会写入交付记录"]];
R.hint = "还不能交付，先处理“文字转写尚未完成”：在“核对答卷”右栏点“转写剩余内容”，或“不再转写剩余项…”。处理完重新点“检查交付”。";
R.issues = (list, fixLabel = "处理方法：") => list.map(([s, m, f]) =>
  `<div class="issue"><span class="seq">${s}</span><span>${m}</span><span class="fix">${fixLabel}${f}</span></div>`).join("");

R.tips = {
  refresh: "重新读取这一批的识别与转写结果；在扫描页重扫、作废后回来点它",
  retry: "用大模型把手写内容转成文字：失败的重新转写，没开启转写的一并补做（可选）",
  skip: "不再转写这 1 项“等待转写”（标为未启用），这样就不挡交付；以后仍可补做",
  check: "只检查不写文件：看数据和图片是否齐全；有阻断项会写明去哪里处理",
  exportNo: "不能生成：还有 1 项阻断：按下方明细处理后，重新点“检查交付”",
  prev: "看列表里的上一份（PgUp）", next: "看列表里的下一份（PgDn）",
  search: "按序号（如 0012）、考号或来源文件名筛选，边输边过滤；清空后显示全部",
};
R.filter = `<div class="seg"><button class="on" title="显示这一批的全部答卷">全部</button>
  <button title="只看识别了但要核对的卡：存疑、多涂、考号重复、转写失败或等待转写等">有警告</button>
  <button title="只看物理异常（没扫好、对不上模板）、已作废或结果不可用的卡">异常</button></div>
  <span class="help" title="有警告：识别了但有需要核对的地方。异常：物理异常、作废或结果不可用的卡。">?</span>`;
