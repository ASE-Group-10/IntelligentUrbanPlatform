const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, WidthType, ShadingType,
  HeadingLevel, AlignmentType, LevelFormat, Header, Footer, PageNumber, BorderStyle,
} = require("docx");

const OUT = process.argv[2];
const FONT = { ascii: "Arial", hAnsi: "Arial", eastAsia: "Microsoft YaHei", cs: "Arial" };
const MONO = { ascii: "Consolas", hAnsi: "Consolas", eastAsia: "Microsoft YaHei", cs: "Consolas" };
const W = 9638; // A4 with 2 cm margins

// inline markup: **bold**, `code`
function runs(text, base = {}) {
  return String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/).filter(Boolean).map((t) => {
    if (t.startsWith("**")) return new TextRun({ ...base, text: t.slice(2, -2), bold: true });
    if (t.startsWith("`")) return new TextRun({ ...base, text: t.slice(1, -1), font: MONO, size: 18, color: "1F4E79" });
    return new TextRun({ ...base, text: t });
  });
}
const p = (t, o = {}) => new Paragraph({ children: runs(t), spacing: { after: 120 }, ...o });
const note = (t) => new Paragraph({ children: runs(t, { italics: true, color: "595959" }), spacing: { after: 120 } });
const b = (t, level = 0) => new Paragraph({ children: runs(t), numbering: { reference: "bullets", level }, spacing: { after: 60 } });
const h1 = (t) => new Paragraph({ text: t, heading: HeadingLevel.HEADING_1 });
const h2 = (t) => new Paragraph({ text: t, heading: HeadingLevel.HEADING_2 });
const h3 = (t) => new Paragraph({ text: t, heading: HeadingLevel.HEADING_3 });

function cell(content, width, header) {
  const items = Array.isArray(content) ? content : [content];
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: header ? { type: ShadingType.CLEAR, fill: "D9E2F3", color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: items.map((t) => new Paragraph({ children: runs(t, { size: 19, bold: !!header }) })),
  });
}
function table(headers, rows, widths) {
  const sum = widths.reduce((a, c) => a + c, 0);
  if (sum !== W) throw new Error(`widths sum ${sum} != ${W} for ${headers.join("|")}`);
  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, widths[i], true)) }),
      ...rows.map((r) => new TableRow({ children: r.map((c, i) => cell(c, widths[i])) })),
    ],
  });
}
const gap = () => new Paragraph({ children: [], spacing: { after: 60 } });
const fields = (rows) => [table(["字段", "类型", "必填", "说明"], rows, [2100, 2300, 700, 4538]), gap()];
const failures = (rows) => [table(["outcome", "code", "触发条件", "调用方处理"], rows, [1300, 2700, 2900, 2738]), gap()];
function code(text) {
  return String(text).split("\n").map((line, i, arr) => new Paragraph({
    children: [new TextRun({ text: line || " ", font: MONO, size: 16 })],
    shading: { type: ShadingType.CLEAR, fill: "F2F2F2", color: "auto" },
    spacing: { after: i === arr.length - 1 ? 160 : 0, line: 240 },
    indent: { left: 120, right: 120 },
  }));
}
const json = (o) => code(JSON.stringify(o, null, 2));

// ---------- shared example data ----------
const P1 = "5b0c2f1e-8a4d-4c2b-9e61-3f7d2a9c1b01";
const P2 = "a3d94e70-2c1f-4b8e-8d55-6e0b7c4f2a02";
const P3 = "7e215c90-4d3b-4a6f-b0c8-91d2e3f4a503";
const P4 = "c84f0a12-6b7e-4d19-8a3c-2f5e6d7c8b04";
const S1 = "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a05";
const D1 = "d7c6b5a4-9384-4756-a1b2-c3d4e5f60708";
const PLAN = "9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a";
const DEL1 = "0a1b2c3d-4e5f-4a6b-8c7d-8e9f0a1b2c3d";
const VAN = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f";
const HGV = "6f5e4d3c-2b1a-4098-8765-43210fedcba9";
const CORR = "4c0ffee0-1234-4abc-9def-001122334455";

const query = {
  area: { zoneIds: ["dublin-D2"] },
  timeWindow: { start: "2026-11-20T07:30:00Z", end: "2026-11-20T09:00:00Z" },
  vehicleClass: "HGV",
  activity: "Delivery",
  freeText: "city-centre delivery to a Grafton Street retailer",
  maxPassages: 5,
};
const pass1 = {
  passageId: P1,
  content: "Heavy goods vehicles with five or more axles are not permitted to enter the city-centre cordon between 07:00 and 19:00 unless a valid permit is held.",
  documentId: "dcc-hgv-cordon",
  documentTitle: "City Centre HGV Restrictions (demo data)",
  documentVersion: 3,
  sectionId: "4.2",
  validFrom: "2026-01-01T00:00:00Z",
  validTo: null,
  applicability: { zoneIds: ["dublin-D1", "dublin-D2"], vehicleClasses: ["HGV"], activities: [] },
  score: 0.87,
  constraints: [{
    constraintId: "dcc-hgv-cordon:v3:4.2:c1",
    kind: "VehicleBan",
    zoneIds: ["dublin-D1", "dublin-D2"],
    vehicleClasses: ["HGV"],
    timeOfDay: { from: "07:00", to: "19:00" },
    daysOfWeek: [],
    value: null,
    exemption: "Valid HGV permit",
  }],
};
const pass2 = {
  passageId: P2,
  content: "Loading and unloading on Grafton Street is permitted between 06:00 and 11:00 only.",
  documentId: "dcc-loading-bays",
  documentTitle: "City Centre Loading Bay Rules (demo data)",
  documentVersion: 1,
  sectionId: "2.1",
  validFrom: "2026-03-01T00:00:00Z",
  validTo: null,
  applicability: { zoneIds: ["dublin-D2"], vehicleClasses: [], activities: ["Delivery", "Loading"] },
  score: 0.74,
  constraints: [{
    constraintId: "dcc-loading-bays:v1:2.1:c1",
    kind: "LoadingWindow",
    zoneIds: ["dublin-D2"],
    vehicleClasses: [],
    timeOfDay: { from: "06:00", to: "11:00" },
    daysOfWeek: [],
    value: null,
    exemption: null,
  }],
};
const meta = { corpusVersion: 7, indexVersion: "idx-7", embeddingModel: "embedding-model-tbd" };
const snapshot = {
  snapshotId: S1,
  query,
  retrievalStatus: "Ok",
  items: [pass1, pass2],
  conflicts: [],
  limitations: [],
  ...meta,
  retrievedAt: "2026-11-20T06:55:02Z",
};
const knowledgeOk = {
  status: "Ok",
  passages: [pass1, pass2],
  conflicts: [],
  limitations: [],
  ...meta,
  retrievedAt: "2026-11-20T06:55:02Z",
  evidenceSnapshot: snapshot,
};
const decision = {
  decisionId: D1,
  idempotencyKey: `c05:plan:${PLAN}:v1`,
  decisionType: "PlanGenerated",
  producer: "C05",
  decidedBy: "service:c05-planner",
  subject: { type: "Plan", id: PLAN, version: 1 },
  relatedDecisionId: null,
  inputVersions: {
    deliveries: [{ id: DEL1, version: 2 }],
    resources: [{ id: VAN, version: 5 }, { id: HGV, version: 3 }],
    citySnapshotVersion: 1042,
    corpusVersion: 7,
  },
  selectedOption: {
    optionId: "opt-1",
    summary: "Van delivery via N11, unloading on Grafton Street 07:40-08:10",
    details: { resourceId: VAN, plannedArrival: "2026-11-20T07:40:00Z" },
  },
  consideredAlternatives: [
    {
      optionId: "opt-2",
      summary: "Direct delivery with 5-axle HGV",
      metrics: { etaMin: 32, costEur: 58.0, co2Kg: 21.4 },
      rejectedReason: "Violates city-centre HGV ban 07:00-19:00 (hard constraint)",
      evidenceRefIds: [P1],
    },
    {
      optionId: "opt-3",
      summary: "Van delivery arriving 11:30",
      metrics: { etaMin: 36, costEur: 39.0, co2Kg: 9.1 },
      rejectedReason: "Outside Grafton Street loading window 06:00-11:00",
      evidenceRefIds: [P2],
    },
  ],
  reasons: [
    { reasonId: "r1", text: "HGV option excluded by the city-centre cordon ban", evidenceRefIds: [P1] },
    { reasonId: "r2", text: "Unloading scheduled inside the permitted loading window", evidenceRefIds: [P2] },
    { reasonId: "r3", text: "Lowest predicted cost among feasible options", evidenceRefIds: [] },
  ],
  predictedEffects: { etaMin: 38, distanceKm: 14.2, costEur: 41.5, co2Kg: 9.8 },
  evidenceSnapshots: [snapshot],
  limitations: [],
  runScope: { mode: "Live", runId: null },
  decidedAt: "2026-11-20T06:55:10Z",
};
const citation = (ps) => ({
  refId: ps.passageId, documentId: ps.documentId, documentTitle: ps.documentTitle,
  documentVersion: ps.documentVersion, sectionId: ps.sectionId, excerpt: ps.content,
});
const explanationOk = {
  decisionId: D1,
  decisionType: "PlanGenerated",
  subject: { type: "Plan", id: PLAN, version: 1 },
  decidedAt: "2026-11-20T06:55:10Z",
  runScope: { mode: "Live", runId: null },
  corpusVersion: 7,
  mode: "Generated",
  completeness: "Complete",
  summary: "The planner assigned the delivery to a van rather than the 5-axle HGV. The HGV is banned from the city-centre cordon between 07:00 and 19:00, and the van can unload on Grafton Street inside the 06:00-11:00 loading window. Among the feasible options it had the lowest predicted cost.",
  keyFactors: [
    { text: "5-axle HGVs may not enter the city-centre cordon 07:00-19:00.", citationRefIds: [P1] },
    { text: "Unloading 07:40-08:10 falls inside the Grafton Street loading window.", citationRefIds: [P2] },
    { text: "Lowest predicted cost among feasible options (EUR 41.50).", citationRefIds: [] },
  ],
  alternatives: [
    { optionId: "opt-2", summary: "Direct delivery with 5-axle HGV", rejectedReason: "Violates city-centre HGV ban 07:00-19:00 (hard constraint)", citationRefIds: [P1] },
    { optionId: "opt-3", summary: "Van delivery arriving 11:30", rejectedReason: "Outside Grafton Street loading window 06:00-11:00", citationRefIds: [P2] },
  ],
  citations: [citation(pass1), citation(pass2)],
  effects: [
    { metric: "eta", value: 38, unit: "min", kind: "Predicted" },
    { metric: "cost", value: 41.5, unit: "EUR", kind: "Predicted" },
    { metric: "co2", value: 9.8, unit: "kg", kind: "Predicted" },
  ],
  gaps: [],
  limitations: [],
  generatedAt: "2026-11-20T09:12:44Z",
  generator: { model: "llm-model-tbd", promptVersion: "explain-v1" },
};
const explanationPartial = {
  ...explanationOk,
  mode: "StructuredFallback",
  completeness: "Partial",
  summary: null,
  keyFactors: [
    { text: "HGV option excluded by the city-centre cordon ban", citationRefIds: [P1] },
    { text: "Unloading scheduled inside the permitted loading window", citationRefIds: [] },
    { text: "Lowest predicted cost among feasible options", citationRefIds: [] },
  ],
  // P2 is missing here, so nothing may cite it
  alternatives: explanationOk.alternatives.map((a) => ({ ...a, citationRefIds: a.citationRefIds.filter((r) => r !== P2) })),
  citations: [citation(pass1)],
  gaps: [{ kind: "EvidenceMissing", refId: P2, message: "Evidence item referenced by reason r2 is not available in C10 storage." }],
  limitations: ["Explanation generator unavailable; showing recorded facts only."],
  generator: null,
};

// ---------- document body ----------
const body = [];
const add = (...xs) => xs.flat().forEach((x) => body.push(x));

add(
  new Paragraph({ text: "C09 / C10 实现前约定与接口契约", heading: HeadingLevel.TITLE }),
  p("UC09 Query logistics knowledge · UC10 Explain logistics decision · Group 10"),
  table(["项目", "内容"], [
    ["版本 / 状态", "v0.1 · 草稿（Draft），待 consumer 确认"],
    ["日期", "2026-11-16"],
    ["Owner pair", "Qingyuan + Bangchen"],
    ["组件", "C09 Logistics Knowledge；C10 Decision Records and Explanation"],
    ["Consumers", ["C05 Planning（Ye + Zecheng）", "C08 Allocation / Negotiation（Shixian + Oscar）", "C01 Web Interface（各 pair 页面）", "C12 Replay（Bruno + Alejandro，若接手）", "所有 pair：`recordOutcome`（审计）"]],
    ["依据", "Architecture_Group10_Updated_Overleaf：§1.2.1、§1.2.2、§1.3.1、§1.3.10、§1.3.11、§1.4.1、§1.4.10、§1.4.11 和部署视图；pair plan；README 分工表"],
  ], [2200, 7438]),
  gap(),
  note("说明：architecture 只规定了逻辑接口。本文中的字段名、枚举值、阈值、超时和 HTTP 路径都是本组的建议值，标为“待确认”的条目需要 consumer 签字。"),
);

// 1
add(
  h1("1  范围与架构边界"),
  h2("1.1 UC09 / C09 负责什么"),
  b("curator 发布文档：建立版本化的当前语料，索引成功后直接激活，没有第二道审批（`publishKnowledge`）。"),
  b("按 area、时间、车型、活动做语义检索和适用性过滤，返回原文段落、出处、版本，以及低置信或冲突标记（`queryKnowledge`）。"),
  b("检测到来源冲突时通知 curator（`notifyKnowledgeConflict`）。这只是警告，不阻塞规划，也不放松 hard constraint。"),
  b("C09 **不提供** `getEvidence`，也不负责历史解释。"),
  h2("1.2 UC10 / C10 负责什么"),
  b("持久化决策：输入版本、选中方案、实际考虑过的备选方案、理由、预测效果，以及证据原文快照（`recordDecision`）。"),
  b("受保护操作的审计记录（`recordOutcome`），所有 pair 都会调用。"),
  b("查询决策（`getDecision`）、生成解释（`explainDecision`）、查询单条历史证据或备选方案（`getDecisionEvidence`）。"),
  b("解释**只能**使用已保存的记录，**不调用 C09**。证据缺失时明确标出，不能用今天的知识重建，也不能编造。"),
  b("AF1：决策不存在、有歧义或无权访问；AF2：证据不完整，返回带 gaps 的部分解释；AF3：生成器或证据存储故障时，返回结构化事实或 Unavailable。"),
  h2("1.3 明确不做的事"),
  b("C09 不存历史证据；C10 不做检索。"),
  b("Replay 中的解释使用 run-scoped 的 C10 记录，不重新检索。"),
  b("知识冲突或低置信只产生警告和 limitation，不新增人工审批流程。"),
  b("knowledge 不可用属于可用性问题，planner 的 hard constraint 仍然有效。"),
  h2("1.4 谁调用什么"),
  table(["调用方", "API", "调用方式", "时机"], [
    ["C05", "`queryKnowledge`", "HTTP（C09 是独立容器）", "生成方案或重规划之前"],
    ["C08", "`queryKnowledge`", "HTTP", "评估 agent proposal 时"],
    ["C05、C08", "`recordDecision`", "进程内 Python，在调用方事务中执行", "保存 plan 或 allocation 的同一事务里"],
    ["所有 pair", "`recordOutcome`", "进程内 Python", "每个受保护操作结束时，包括被拒绝的操作"],
    ["C01", "`getDecision`、`explainDecision`、`getDecisionEvidence`", "HTTP", "用户查看解释时"],
    ["C12", "`explainDecision`（runScope=Simulation）", "HTTP", "replay 时"],
    ["Curator（经 C01）", "`publishKnowledge`", "HTTP", "发布或更新文档时"],
    ["C09 内部", "`notifyKnowledgeConflict`", "内部调用", "检索时检测到冲突"],
  ], [1700, 2900, 2700, 2338]),
  gap(),
);

// 2
const T = "**待确认**";
add(
  h1("2  实现前必须约定的事项（Checklist）"),
  p("下表每一行都要在截止日期前在 GitHub issue 中关闭：consumer 在对应 PR 中 approve，就算确认。"),
  table(["#", "事项", "本组建议", "需确认方", "截止"], [
    ["1", "`PolicyQuery` 字段；area 用什么表示", "`zoneIds[]`，与 C06 共用 zone ID", "C05、C08、C06", "11-16"],
    ["2", "`KnowledgeResult.status` 的四个值及含义", "Ok / LowConfidence / Conflict / NoApplicable（§7.4）", "C05、C08", "11-16"],
    ["3", "`EvidenceSnapshot` 由谁生成、如何传递", "C09 生成；调用方**原样**转交给 `recordDecision`，不修改", "C05、C08", "11-16"],
    ["4", "planner 如何使用知识：结构化约束格式", "`Passage.constraints[]` 对象（§7.3），时间用 Europe/Dublin 本地时间", "C05", "11-16"],
    ["5", "decisionId 由谁生成", "调用方生成 UUID（Plan 里要先写入 decisionId）", "C05、C08", "11-16"],
    ["6", "`recordDecision` 的调用方式", "进程内函数，接收调用方的 DB connection；C10 **不** commit，由调用方统一提交 plan、decision 和 outbox", "C05、C08", "11-16"],
    ["7", "`DecisionRecord` 字段", "§7.10", "C05、C08", "11-17"],
    ["8", "knowledge 不可用时的行为", "planner 只按结构化约束继续，decision 写入 limitation `knowledge unavailable`；是否改为直接失败由 C05 决定", "C05", "11-17"],
    ["9", "`recordOutcome` 接口", "§7.9", "所有 pair", "11-17"],
    ["10", "`DecisionSelector`", "decisionId，或 subject（type + id + version）", "C01、C12", "11-17"],
    ["11", "`Explanation` 结构", "§7.13", "C01、C12", "11-18"],
    ["12", "`Result` 与错误格式、HTTP 映射", "§3", "全组", "11-16"],
    ["13", "`AuthContext` 和角色名", "KnowledgeCurator、LogisticsOperator、ProviderAdmin、Service:Cxx", "C02 负责人 / 全组", "11-16"],
    ["14", "共享枚举：vehicleClass、activity；zone 列表", "与 C03、C06 共用同一份枚举文件", "C03、C04、C06", "11-16"],
    ["15", "Embedding 模型和向量维度", T + "（pgvector 列维度建表后难改）", "本组", "11-17"],
    ["16", "LLM provider 和 key 管理", T + "；key 只放在服务端环境变量", "本组 + 部署负责人", "11-18"],
    ["17", "Demo 种子语料", "3–5 份 demo 政策文档，与 demo 场景和 C05 约束对应", "C05 + demo 负责人", "11-17"],
    ["18", "表与 migration 归属", "§4", "本组", "11-17"],
    ["19", "UC11 是否由本组接手", "在 issue 中记录全组决定（README：在全组同意前仍归 Jolin）", "Jolin + 全组", "11-16"],
    ["20", "runScope / runId 语义", "Live 或 Simulation；Simulation 必须带 runId", "C12", "11-18"],
  ], [500, 2500, 3700, 1900, 1038]),
  gap(),
);

// 3
add(
  h1("3  全组通用约定"),
  b("**ID**：UUID v4 字符串。由谁生成在每个对象中写明。外部可读的业务 ID（如 documentId `dcc-hgv-cordon`）是 slug 字符串。"),
  b("**时间戳**：ISO 8601 UTC，带 `Z`。**例外**：政策里的“每天几点到几点”用 `HH:MM` 本地时间（Europe/Dublin），因为规则本身按当地时间写。"),
  b("**命名**：JSON 用 camelCase，数据库用 snake_case，枚举值用 PascalCase 字符串。"),
  b("**单位**：写在字段名或 effects 的 `unit` 中：min、km、EUR、kg。"),
  b("**AuthContext**：`{principalId, principalType: User|Service, roles[], scope, correlationId}`。HTTP 调用从 Supabase JWT 解析，correlationId 放在 `X-Correlation-Id` header；进程内调用直接传对象。只有 ID 不代表有权限。"),
  b("**幂等**：所有写操作都带 `idempotencyKey`。同一 key 且 body 相同时，返回第一次的结果并带 `duplicate: true`；同一 key 但 body 不同时返回 `Conflict`。"),
  b("**版本兼容**：增加可选字段属于 minor 版本，通知即可；删除字段、改类型或改语义属于 major 版本，必须 consumer 重新 approve。"),
  h3("Result 与 HTTP 映射"),
  table(["outcome", "HTTP", "含义"], [
    ["Ok", "200 / 201", "成功，body 直接是 T"],
    ["Partial", "200", "成功但不完整，T 中的 `completeness=Partial` 和 `gaps[]` 说明缺什么"],
    ["Invalid", "400", "请求不合法"],
    ["Forbidden", "403", "无权限；不返回任何受限细节"],
    ["NotFound", "404", "对象不存在，或调用方无权知道它存在"],
    ["Conflict", "409", "幂等 key 冲突、selector 有歧义、版本冲突"],
    ["Unavailable", "503", "依赖暂时不可用，可以重试"],
  ], [1600, 1400, 6638]),
  gap(),
  p("错误 body 统一为："),
  json({ outcome: "Unavailable", code: "C09.INDEX_UNAVAILABLE", message: "Vector index is not reachable; retry later.", details: { retryAfterSec: 30 }, correlationId: CORR }),
);

// 4
add(
  h1("4  数据归属与数据库表"),
  p("migration 由 owner 编写。其他组件不直接读写这些表，只通过本文的接口访问。"),
  table(["表 / 存储", "Owner", "写入规则", "主要内容"], [
    ["`knowledge_documents`", "C09", "新版本插入新行，旧版本标记 Superseded", "documentId、documentVersion、title、issuer、applicability、validFrom/To、status、source 文件路径"],
    ["`knowledge_chunks`", "C09", "随文档版本插入", "passageId、documentId、documentVersion、sectionId、content、`embedding vector(N)`、applicability、constraints（jsonb）"],
    ["`corpus_versions`", "C09", "只插入", "corpusVersion、indexVersion、embeddingModel、activatedAt"],
    ["`knowledge_warnings`", "C09", "按幂等 key 去重插入", "conflictId、passageIds、reason、createdAt"],
    ["Storage bucket `c09-sources`", "C09", "只增不改", "curator 上传的原始文件"],
    ["`decisions`", "C10", "**只插入**；`(producer, idempotency_key)` 唯一", "DecisionRecord 的标量字段，以及 alternatives、reasons、inputVersions、predictedEffects（jsonb）"],
    ["`decision_evidence`", "C10", "**只插入**", "decisionId、snapshotId、refId（= passageId）、**content 原文**、出处和版本、score、constraints、retrievalStatus 和 corpus 元数据"],
    ["`audit_outcomes`", "C10", "只插入", "auditId、operation、outcome、principalId、targetIds、correlationId、occurredAt、details"],
  ], [2200, 800, 2700, 3938]),
  gap(),
  b("“只插入”通过数据库权限或 trigger 强制执行：应用角色没有 UPDATE 和 DELETE 权限。"),
  b("Slice 阶段证据原文直接存在 `decision_evidence.content` 中，不另建 blob。"),
);

// 5
add(
  h1("5  时间节点（DDL）"),
  table(["日期", "交付物", "完成标准"], [
    ["11-16 周一", "本文档 v0.1；开 technical-task issue；UC11 是否接手的决定", "C05、C08 对 §2 中 1–6、12–14 项 approve"],
    ["11-17 周二", "两边的 migration 合并；test double 可用；`recordOutcome` 可用；种子语料定稿", "其他 pair 能 import `record_outcome`；fake `queryKnowledge` 返回 §8 示例"],
    ["11-18 周三", "C09：publish 和 pgvector 检索可用；C10：`recordDecision`、`getDecision` 可用并支持幂等", "§8 的示例能通过 contract 测试；真实检索能返回结构兼容的结果"],
    ["11-19 周四", "**集成第 2 步**：C05 查询知识，在同一事务里保存 plan、decision 和 evidence；`explainDecision` 的结构化 fallback 可用", "端到端测试：查询 → 记录 → 解释"],
    ["11-20 周五", "全组 initial workflow demo", "demo 中保存的 plan 带证据，并且能显示解释"],
    ["11-23 周一", "LLM 解释接入引用校验；AF1–AF3；LowConfidence 和 Conflict 路径；C08 写入决策记录；C01 解释页面", "每条 AF 至少一个测试"],
    ["11-24 周二", "Replan 决策可解释；C12 run-scoped 解释；（若接手）UC11 基础 KPI", "重新发布语料后，旧决策的解释保持不变"],
    ["11-25 至 26", "修 bug、清理部署、彩排", "—"],
  ], [1400, 4800, 3438]),
  gap(),
);

// 6
add(
  h1("6  分工"),
  table(["成员", "负责", "主要交付"], [
    ["Qingyuan", "C09", ["语料模型与 migration；`publishKnowledge`（Markdown 切分、embedding、索引、激活版本）", "`queryKnowledge`：适用性过滤、置信阈值、冲突检测、`notifyKnowledgeConflict`", "生成 `EvidenceSnapshot`；种子语料；C01 解释页面（因为 C10 的工作量更大）"]],
    ["Bangchen", "C10", ["decisions、evidence、audit 表与 migration；`recordOutcome`、`recordDecision`（幂等，事务内调用）", "`getDecision`、`getDecisionEvidence`", "`explainDecision`：LLM 调用、引用校验、结构化 fallback、AF1–AF3"]],
    ["两人一起", "接缝", ["本文档；`contracts/` 下的 Pydantic 模型和示例 JSON", "关键端到端测试：查询 → 记录 → 解释 → 重新发布语料 → 再次解释，结果不变，且全程不调用 C09", "互相 review 对方的 PR；与 C05 一起完成集成第 2 步"]],
  ], [1400, 1200, 7038]),
  gap(),
  note("如果本组接手 UC11，建议由 C09 这一侧承担大部分 KPI 工作，以平衡两人的工作量。"),
);

// 7
add(h1("7  接口契约"));
add(
  h2("7.1 共享类型"),
  h3("AuthContext"),
  ...fields([
    ["principalId", "string", "是", "用户 ID 或 `service:<name>`"],
    ["principalType", "enum User | Service", "是", ""],
    ["roles", "string[]", "是", "见 §2 第 13 项"],
    ["scope", "{providerIds?: string[], zoneIds?: string[]}", "否", "缺省表示不按 provider 或 zone 限制"],
    ["correlationId", "UUID", "是", "贯穿一次用户操作"],
  ]),
  h3("共享枚举（与 C03、C04、C06 共用，" + "待确认）"),
  b("`VehicleClass`：CargoBike | Van | LGV | HGV"),
  b("`Activity`：Pickup | Delivery | Transit | Loading"),
  b("`RunMode`：Live | Simulation；`RunScope = {mode, runId: UUID|null}`，mode 为 Simulation 时 runId 必填"),
);

add(
  h2("7.2 PolicyQuery"),
  ...fields([
    ["area.zoneIds", "string[]（≥1）", "是", "与 C06 共用的 zone ID，例如 `dublin-D2`。" + T],
    ["timeWindow.start / end", "UTC timestamp", "是", "活动发生的时间段（不是查询时间）；要求 end > start"],
    ["vehicleClass", "VehicleClass", "是", ""],
    ["activity", "Activity", "是", ""],
    ["freeText", "string ≤ 500", "否", "补充语义检索用的文本"],
    ["maxPassages", "int 1–20", "否", "默认 5"],
  ]),
);

add(
  h2("7.3 Passage 与 Constraint"),
  ...fields([
    ["passageId", "UUID", "是", "C09 生成；在证据中作为 refId"],
    ["content", "string", "是", "**原文**，不是摘要"],
    ["documentId", "string (slug)", "是", ""],
    ["documentTitle", "string", "是", ""],
    ["documentVersion", "int ≥ 1", "是", ""],
    ["sectionId", "string", "是", "来自 Markdown 标题编号"],
    ["validFrom / validTo", "UTC / UTC|null", "是", "validTo 为 null 表示没有截止日期"],
    ["applicability", "{zoneIds[], vehicleClasses[], activities[]}", "是", "vehicleClasses 或 activities 为空列表时表示适用于全部"],
    ["score", "float 0–1", "是", "语义相似度，re-rank 后的值"],
    ["constraints", "Constraint[]", "是，可以为空", "供 planner 使用的结构化约束。" + T + "（C05）"],
  ]),
  p("**Constraint**（第 4 项，需要 C05 确认）："),
  ...fields([
    ["constraintId", "string", "是", "`<documentId>:v<version>:<section>:c<n>`"],
    ["kind", "enum VehicleBan | LoadingWindow | MaxGrossWeight", "是", "slice 阶段只支持这三种"],
    ["zoneIds / vehicleClasses", "string[] / VehicleClass[]", "是", "空列表表示全部"],
    ["timeOfDay", "{from: HH:MM, to: HH:MM} | null", "是", "Europe/Dublin 本地时间"],
    ["daysOfWeek", "enum Mon..Sun []", "是", "空列表表示每天"],
    ["value", "object | null", "是", "例如 MaxGrossWeight 用 `{maxKg: 18000}`"],
    ["exemption", "string | null", "是", "豁免条件，仅作说明；planner 是否处理豁免由 C05 决定"],
  ]),
);

add(
  h2("7.4 KnowledgeResult"),
  ...fields([
    ["status", "enum Ok | LowConfidence | Conflict | NoApplicable", "是", ["Ok：证据充分", "LowConfidence：最高 score 低于阈值（默认 0.60，可配置）", "Conflict：至少一组适用段落相互矛盾（同 kind、范围重叠但取值不同）", "NoApplicable：没有段落通过适用性过滤。**这不代表没有限制**，调用方要记录 limitation"]],
    ["passages", "Passage[]", "是，可以为空", "按 score 降序排列"],
    ["conflicts", "{conflictId, passageIds[], reason}[]", "是，可以为空", "冲突的各方段落都包含在 passages 中"],
    ["limitations", "string[]", "是，可以为空", "人类可读的说明"],
    ["corpusVersion / indexVersion / embeddingModel", "int / string / string", "是", "审计用"],
    ["retrievedAt", "UTC", "是", ""],
    ["evidenceSnapshot", "EvidenceSnapshot", "是", "调用方**原样**转交给 `recordDecision`"],
  ]),
  h2("7.5 EvidenceSnapshot"),
  ...fields([
    ["snapshotId", "UUID", "是", "C09 生成"],
    ["query", "PolicyQuery", "是", "原始查询的副本"],
    ["retrievalStatus", "同 KnowledgeResult.status", "是", ""],
    ["items", "Passage[]", "是", "= passages 的完整副本，包括 content"],
    ["conflicts / limitations", "同上", "是", ""],
    ["corpusVersion / indexVersion / embeddingModel / retrievedAt", "同上", "是", ""],
  ]),
  note("一个决策可以带多个 snapshot（例如 planner 按多个 zone 分别查询）。C10 按 passageId + documentVersion 去重。"),
);

add(
  h2("7.6 queryKnowledge（C09）"),
  p("**调用方式**：`POST /api/c09/knowledge/query`，body 为 PolicyQuery，返回 200 和 KnowledgeResult。只读操作，不需要 idempotencyKey。调用方超时设为 5 秒，超时按 Unavailable 处理。"),
  p("**授权**：Service:C05、Service:C08；LogisticsOperator 只读（用于调试页面，可选）。"),
  ...failures([
    ["Invalid", "C09.INVALID_TIME_WINDOW", "end ≤ start", "修正请求"],
    ["Invalid", "C09.UNKNOWN_ZONE", "zoneId 不在共享列表中", "修正请求"],
    ["Forbidden", "C09.FORBIDDEN", "调用身份没有查询权限", "不重试"],
    ["Unavailable", "C09.INDEX_UNAVAILABLE / C09.EMBEDDING_UNAVAILABLE", "向量库或 embedding API 不可用", "按 §2 第 8 项处理，并在 decision 中写入 limitation"],
  ]),
  b("只返回**当前激活**的语料版本中的内容。"),
  b("在同一个 corpusVersion 下，同样的请求返回同样的结果（score 并列时按 passageId 排序）。"),
  b("Conflict 时 C09 会异步调用 `notifyKnowledgeConflict`，不等待结果，也不阻塞调用方。"),
);

add(
  h2("7.7 publishKnowledge（C09）"),
  p("**调用方式**：`POST /api/c09/documents`，仅 KnowledgeCurator 可以调用。成功返回 201。"),
  ...fields([
    ["document.title", "string", "是", ""],
    ["document.format", "enum markdown", "是", "slice 阶段只支持 Markdown；用标题编号作为 sectionId"],
    ["document.content", "string", "是", ""],
    ["metadata.documentId", "string (slug)", "是", "已存在时生成新版本"],
    ["metadata.issuer", "string", "是", ""],
    ["metadata.applicability", "同 Passage.applicability", "是", "默认作用于所有段落"],
    ["metadata.validFrom / validTo", "UTC / UTC|null", "是", ""],
    ["metadata.constraintsBySection", "{sectionId: Constraint[]}", "否", "由 curator 手工标注结构化约束"],
    ["idempotencyKey", "string", "是", ""],
  ]),
  p("**响应 KnowledgePublication**：`{documentId, documentVersion, corpusVersion, indexVersion, chunkCount, activatedAt, duplicate}`"),
  ...failures([
    ["Forbidden", "C09.NOT_CURATOR", "调用方不是 curator", "—"],
    ["Invalid", "C09.INVALID_DOCUMENT", "格式错误、缺字段、sectionId 不存在", "修正后重新提交"],
    ["Conflict", "C09.IDEMPOTENCY_MISMATCH", "同一个 key 但 body 不同", "换一个 key"],
    ["Unavailable", "C09.INDEXING_FAILED", "索引失败；**旧语料保持激活**", "稍后重试"],
  ]),
);

add(
  h2("7.8 notifyKnowledgeConflict（C09 内部）"),
  p("由 C09 自己调用，其他组件不调用。入参为 `{conflictId, passageIds[], documentIds[], reason}` 和 key，其中 key = hash(按序排列的 passageIds + corpusVersion)。返回 `WarningReceipt {warningId, duplicate}`。slice 阶段写入 `knowledge_warnings` 表，并显示在 curator 页面上。"),
);

add(
  h2("7.9 recordOutcome（C10）"),
  p("**调用方式**：进程内函数 `record_outcome(conn, ctx: AuthContext, outcome: AuditOutcome) -> Result[AuditId]`。可以在调用方的事务里执行；被拒绝的操作也要记录（这时通常用独立事务）。"),
  ...fields([
    ["operation", "string", "是", "`<Component>.<operation>`，例如 `C04.submitDeliveryRequest`"],
    ["outcome", "enum Ok | Invalid | Forbidden | NotFound | Conflict | Unavailable | Infeasible | Partial | Unconfirmed", "是", ""],
    ["targetIds", "string[]", "是，可以为空", "被操作对象的 ID"],
    ["occurredAt", "UTC", "是", ""],
    ["details", "object", "否", "**不得包含**密码、token 或个人敏感数据"],
  ]),
  note("principalId 和 correlationId 从 ctx 中取，不需要重复传。"),
);

add(
  h2("7.10 DecisionRecord"),
  ...fields([
    ["decisionId", "UUID", "是", "**调用方生成**"],
    ["idempotencyKey", "string", "是", "建议格式 `<producer>:<subjectType>:<id>:v<version>`"],
    ["decisionType", "enum PlanGenerated | Replan | Allocation | Negotiation", "是", "C08 需要哪些类型待 C08 确认"],
    ["producer", "enum C05 | C08", "是", "必须和 ctx 的服务身份一致"],
    ["decidedBy", "string", "是", "principalId（agent 或用户）"],
    ["subject", "{type: Plan|Allocation|Negotiation, id: UUID, version: int}", "是", "解释时用它定位决策"],
    ["relatedDecisionId", "UUID | null", "是", "Replan 时指向上一个决策"],
    ["inputVersions", "{deliveries[{id,version}], resources[{id,version}], citySnapshotVersion: int|null, corpusVersion: int|null}", "是", "决策时实际使用的输入版本"],
    ["selectedOption", "{optionId, summary, details: object}", "是", ""],
    ["consideredAlternatives", "{optionId, summary, metrics?, rejectedReason, evidenceRefIds[]}[]", "是，可以为空", "只写**实际评估过**的方案"],
    ["reasons", "{reasonId, text, evidenceRefIds[]}[]", "是（≥1）", "evidenceRefIds 必须能在 snapshots 中找到"],
    ["predictedEffects", "{etaMin?, distanceKm?, costEur?, co2Kg?}", "否", "一律视为预测值"],
    ["evidenceSnapshots", "EvidenceSnapshot[]", "是，可以为空", "knowledge 不可用时传空列表，并在 limitations 中说明"],
    ["limitations", "string[]", "是，可以为空", ""],
    ["runScope", "RunScope", "是", ""],
    ["decidedAt", "UTC", "是", ""],
  ]),
);

add(
  h2("7.11 recordDecision（C10）"),
  p("**调用方式**：进程内函数 `record_decision(conn, ctx: AuthContext, record: DecisionRecord) -> Result[RecordDecisionResponse]`。**必须传入调用方的事务 connection；C10 不 commit。** 只要调用方回滚，decision 和 evidence 也一起回滚。"),
  p("**响应**：`{decisionId, recordedAt, duplicate: bool}`"),
  ...failures([
    ["Invalid", "C10.MISSING_FIELD", "缺少必填字段或枚举值非法", "修复代码"],
    ["Invalid", "C10.EVIDENCE_REF_MISSING", "reasons 或 alternatives 引用的 refId 不在 snapshots 中", "修复代码"],
    ["Invalid", "C10.EVIDENCE_CONTENT_MISSING", "snapshot 的 item 缺少 content，只有引用", "原样转交 C09 返回的 snapshot"],
    ["Forbidden", "C10.PRODUCER_MISMATCH", "producer 和 ctx 的服务身份不一致", "—"],
    ["Conflict", "C10.IDEMPOTENCY_MISMATCH", "同一个 key 但 body 不同", "调用方回滚事务"],
  ]),
  b("写入后不可修改。之后重新发布语料不会影响已保存的证据。"),
  b("同一个 key 且 body 相同时，返回原来的 decisionId，并带 `duplicate: true`。"),
);

add(
  h2("7.12 getDecision（C10）"),
  p("**调用方式**：`GET /api/c10/decisions/{decisionId}`，或 `GET /api/c10/decisions?subjectType=&subjectId=&subjectVersion=&runId=`。"),
  p("**DecisionSelector**：二选一：`{decisionId}`，或 `{subjectType, subjectId, subjectVersion?, runId?}`。"),
  p("**响应 DecisionRecordView**：DecisionRecord 的全部字段（evidence items 只含元数据，不含 content），加上 `recordedAt` 和 `evidenceStatus[] {refId, status: Stored|Missing}`。证据原文通过 §7.14 获取。"),
  ...failures([
    ["NotFound", "C10.DECISION_NOT_FOUND", "不存在，或无权知道它存在（AF1）", "提示用户"],
    ["Conflict", "C10.DECISION_AMBIGUOUS", "subject 没给 version，且对应多条决策（AF1）", "从 `details.candidates[]` 中选一条"],
    ["Forbidden", "C10.FORBIDDEN", "超出 scope", "不返回任何细节"],
  ]),
);

add(
  h2("7.13 explainDecision 与 Explanation（C10）"),
  p("**调用方式**：`POST /api/c10/explanations`，body 为 `{selector: DecisionSelector, language?: \"en\"}`，返回 200 和 Explanation。LLM 超时 20 秒，超时后走 fallback。"),
  ...fields([
    ["decisionId / decisionType / subject / decidedAt / runScope / corpusVersion", "同 DecisionRecord", "是", "让解释带上原始的上下文"],
    ["mode", "enum Generated | StructuredFallback", "是", "Generated 表示由 LLM 生成；StructuredFallback 表示 AF3 时直接展示记录的事实"],
    ["completeness", "enum Complete | Partial", "是", "Partial 时 gaps 不为空（AF2）"],
    ["summary", "string | null", "是", "fallback 时为 null"],
    ["keyFactors", "{text, citationRefIds[]}[]", "是", ""],
    ["alternatives", "{optionId, summary, rejectedReason, citationRefIds[]}[]", "是，可以为空", "来自记录中的 consideredAlternatives"],
    ["citations", "{refId, documentId, documentTitle, documentVersion, sectionId, excerpt}[]", "是，可以为空", "excerpt 取自 C10 已保存的原文"],
    ["effects", "{metric, value, unit, kind: Predicted|Observed}[]", "是，可以为空", "slice 阶段只有 Predicted"],
    ["gaps", "{kind: EvidenceMissing|InconsistentRecord, refId?, message}[]", "是，可以为空", ""],
    ["limitations", "string[]", "是，可以为空", "包括决策时记录的 limitation"],
    ["generatedAt", "UTC", "是", ""],
    ["generator", "{model, promptVersion} | null", "是", "fallback 时为 null"],
  ]),
  p("**生成规则**："),
  b("prompt 中只放该决策的记录和已保存的证据原文，不调用 C09，也不加入其他检索结果。"),
  b("LLM 输出中所有 citationRefIds 都必须出现在已保存的证据中；有任何一个不在，就丢弃 LLM 结果，改用 StructuredFallback。"),
  b("调用不会修改原始的 decision 或 plan。"),
  ...failures([
    ["NotFound / Conflict / Forbidden", "同 §7.12", "AF1", "同 §7.12"],
    ["Ok（Partial）", "—", "部分证据缺失或记录不一致（AF2）", "显示 gaps"],
    ["Ok（StructuredFallback）", "—", "LLM 不可用或引用校验失败（AF3）", "照常显示，并提示是结构化结果"],
    ["Unavailable", "C10.EVIDENCE_STORE_UNAVAILABLE", "连决策记录本身都读不到（AF3）", "稍后重试"],
  ]),
);

add(
  h2("7.14 getDecisionEvidence（C10）"),
  p("**调用方式**：`GET /api/c10/decisions/{decisionId}/evidence/{referenceId}`。referenceId 可以是证据的 refId（passageId），也可以是备选方案的 optionId。"),
  p("**响应 DecisionEvidence**：`{decisionId, referenceId, kind: Evidence|Alternative, status: Stored|Missing, evidence: Passage|null, alternative: object|null}`。只读 C10 自己保存的数据，不查询当前语料。"),
  ...failures([
    ["NotFound", "C10.REFERENCE_NOT_FOUND", "该决策中没有这个 referenceId", "—"],
    ["Forbidden", "C10.FORBIDDEN", "超出 scope", "—"],
  ]),
);

// 8
add(
  h1("8  JSON 示例"),
  p("以下示例以 `contracts/c09_c10/examples/<名字>.json` 的形式放进 repo，同时作为 test double 的返回值和 contract 测试的输入。所有示例使用同一个 demo 场景：11 月 20 日早上，一个 HGV 订单要送到 D2 的 Grafton Street。"),
  note("政策内容是为 demo 编的，不是真实的都柏林法规。"),
  h2("E1  query_knowledge.request.json"),
  ...json(query),
  h2("E2  query_knowledge.ok.response.json"),
  ...json(knowledgeOk),
  h2("E3  query_knowledge.low_confidence.response.json（片段）"),
  note("片段：省略了 evidenceSnapshot，它的结构与 E2 相同，retrievalStatus 为 LowConfidence。"),
  ...json({
    status: "LowConfidence",
    passages: [{ ...pass2, passageId: P3, score: 0.41 }],
    conflicts: [],
    limitations: ["Top score 0.41 is below the confidence threshold 0.60."],
    ...meta,
    retrievedAt: "2026-11-20T06:55:02Z",
  }),
  h2("E4  query_knowledge.conflict.response.json（片段）"),
  note("片段：passages 中包含 P1 和 P4 两条冲突段落（这里省略），evidenceSnapshot 也省略。"),
  ...json({
    status: "Conflict",
    conflicts: [{
      conflictId: "f0e1d2c3-b4a5-4968-8776-655443322110",
      passageIds: [P1, P4],
      reason: "Both apply to HGV in dublin-D2 on 2026-11-20 but the ban windows differ (07:00-19:00 vs 07:00-10:30).",
    }],
    limitations: ["Conflicting sources; curator has been notified."],
  }),
  h2("E5  error.unavailable.json"),
  ...json({ outcome: "Unavailable", code: "C09.INDEX_UNAVAILABLE", message: "Vector index is not reachable; retry later.", details: { retryAfterSec: 30 }, correlationId: CORR }),
  h2("E6  publish_knowledge.request.json / response.json"),
  ...json({
    document: {
      title: pass1.documentTitle,
      format: "markdown",
      content: "# 4 HGV restrictions\n\n## 4.2 City-centre cordon\n\n" + pass1.content,
    },
    metadata: {
      documentId: "dcc-hgv-cordon",
      issuer: "City authority (demo data)",
      applicability: pass1.applicability,
      validFrom: "2026-01-01T00:00:00Z",
      validTo: null,
      constraintsBySection: { "4.2": pass1.constraints },
    },
    idempotencyKey: "publish:dcc-hgv-cordon:v3",
  }),
  ...json({ documentId: "dcc-hgv-cordon", documentVersion: 3, corpusVersion: 7, indexVersion: "idx-7", chunkCount: 12, activatedAt: "2026-11-18T14:02:11Z", duplicate: false }),
  h2("E7  record_outcome.request.json / response.json"),
  ...json({ operation: "C04.submitDeliveryRequest", outcome: "Forbidden", targetIds: [DEL1], occurredAt: "2026-11-19T10:15:00Z", details: { reason: "provider scope mismatch" } }),
  ...json({ auditId: "2b3c4d5e-6f70-4812-9a3b-4c5d6e7f8091" }),
  h2("E8  record_decision.request.json（C05 PlanGenerated）"),
  ...json(decision),
  h2("E9  record_decision.response.json"),
  ...json({ decisionId: D1, recordedAt: "2026-11-20T06:55:10Z", duplicate: false }),
  h2("E10  explain_decision.request.json"),
  ...json({ selector: { decisionId: D1 }, language: "en" }),
  h2("E11  explain_decision.ok.response.json（Generated, Complete）"),
  ...json(explanationOk),
  h2("E12  explain_decision.partial.response.json（StructuredFallback, Partial）"),
  ...json(explanationPartial),
  h2("E13  get_decision_evidence.response.json"),
  ...json({ decisionId: D1, referenceId: P1, kind: "Evidence", status: "Stored", evidence: pass1, alternative: null }),
  h2("E14  error.decision_ambiguous.json"),
  ...json({
    outcome: "Conflict",
    code: "C10.DECISION_AMBIGUOUS",
    message: "More than one decision matches this subject; specify subjectVersion or decisionId.",
    details: { candidates: [{ decisionId: D1, subjectVersion: 1 }, { decisionId: "b1a2c3d4-e5f6-4789-8abc-def012345678", subjectVersion: 2 }] },
    correlationId: CORR,
  }),
);

// 9
add(
  h1("9  待确认问题与签字"),
  h2("9.1 待确认问题"),
  table(["#", "问题", "问谁"], [
    ["Q1", "Constraint 格式（§7.3）planner 是否能直接用？是否还需要其他 kind？", "Ye + Zecheng"],
    ["Q2", "area 用 zoneIds 还是 GeoJSON；zone 列表放在哪里", "Bruno + Alejandro（C06）"],
    ["Q3", "C08 需要记录哪些 decisionType；协商结果是否也需要查询知识", "Shixian + Oscar"],
    ["Q4", "实际结果（Observed effects）从哪里来：C11 的事件，还是 C08 的执行结果？slice 阶段是否只展示 Predicted", "全组"],
    ["Q5", "谁能看哪些决策的解释（provider scope 规则）", "C02 负责人"],
    ["Q6", "Embedding、LLM 的 provider，费用和 key 管理", "本组 + 部署负责人"],
    ["Q7", "UC11 是否由本组接手", "Jolin + 全组"],
    ["Q8", "Replay 时 runId 的生成和传递方式", "C12 负责人"],
  ], [700, 6238, 2700]),
  gap(),
  h2("9.2 签字"),
  table(["内容", "Producer", "Consumer", "确认人", "日期"], [
    ["§7.2–7.6 queryKnowledge", "C09", "C05", "", ""],
    ["§7.2–7.6 queryKnowledge", "C09", "C08", "", ""],
    ["§7.9 recordOutcome", "C10", "全组", "", ""],
    ["§7.10–7.11 recordDecision", "C10", "C05", "", ""],
    ["§7.10–7.11 recordDecision", "C10", "C08", "", ""],
    ["§7.12–7.14 解释相关接口", "C10", "C01 / C12", "", ""],
  ], [3238, 1300, 1700, 2000, 1400]),
);

const doc = new Document({
  styles: {
    default: { document: { run: { font: FONT, size: 21 } } },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", run: { font: FONT, size: 40, bold: true, color: "1F3864" }, paragraph: { spacing: { before: 0, after: 120 }, keepNext: true } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 30, bold: true, color: "1F3864" }, paragraph: { spacing: { before: 360, after: 160 }, keepNext: true, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 25, bold: true, color: "2E5395" }, paragraph: { spacing: { before: 240, after: 100 }, keepNext: true, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 22, bold: true, color: "404040" }, paragraph: { spacing: { before: 180, after: 80 }, keepNext: true, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [{
      reference: "bullets",
      levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 400, hanging: 260 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "–", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 800, hanging: 260 } } } },
      ],
    }],
  },
  sections: [{
    properties: { page: { margin: { top: 1134, bottom: 1134, left: 1134, right: 1134 } } },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: runs("Group 10 · C09/C10 Contract · v0.1 Draft", { size: 16, color: "808080" }) })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ children: [PageNumber.CURRENT], size: 16, color: "808080" })] })] }) },
    children: body,
  }],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("wrote", OUT, buf.length); });

// ---------- JSON export (full versions; doc shows E3/E4 as fragments) ----------
const EX = process.argv[3];
if (EX) {
  const pass3 = { ...pass2, passageId: P3, score: 0.41 };
  const pass4 = {
    ...pass1,
    passageId: P4,
    content: "Heavy goods vehicles with five or more axles may not enter the city-centre cordon between 07:00 and 10:30.",
    documentId: "dcc-hgv-cordon-amendment",
    documentTitle: "City Centre HGV Restrictions - Amendment (demo data)",
    documentVersion: 1,
    sectionId: "1.1",
    score: 0.83,
    constraints: [{ ...pass1.constraints[0], constraintId: "dcc-hgv-cordon-amendment:v1:1.1:c1", timeOfDay: { from: "07:00", to: "10:30" }, exemption: null }],
  };
  const conflicts = [{
    conflictId: "f0e1d2c3-b4a5-4968-8776-655443322110",
    passageIds: [P1, P4],
    reason: "Both apply to HGV in dublin-D2 on 2026-11-20 but the ban windows differ (07:00-19:00 vs 07:00-10:30).",
  }];
  const result = (status, passages, conflicts, limitations, snapshotId) => ({
    status, passages, conflicts, limitations, ...meta, retrievedAt: "2026-11-20T06:55:02Z",
    evidenceSnapshot: { snapshotId, query, retrievalStatus: status, items: passages, conflicts, limitations, ...meta, retrievedAt: "2026-11-20T06:55:02Z" },
  });
  const files = {
    "query_knowledge.request.json": query,
    "query_knowledge.ok.response.json": knowledgeOk,
    "query_knowledge.low_confidence.response.json": result("LowConfidence", [pass3], [], ["Top score 0.41 is below the confidence threshold 0.60."], "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c06"),
    "query_knowledge.conflict.response.json": result("Conflict", [pass1, pass4], conflicts, ["Conflicting sources; curator has been notified."], "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d07"),
    "error.unavailable.json": { outcome: "Unavailable", code: "C09.INDEX_UNAVAILABLE", message: "Vector index is not reachable; retry later.", details: { retryAfterSec: 30 }, correlationId: CORR },
    "publish_knowledge.request.json": {
      document: { title: pass1.documentTitle, format: "markdown", content: "# 4 HGV restrictions\n\n## 4.2 City-centre cordon\n\n" + pass1.content },
      metadata: { documentId: "dcc-hgv-cordon", issuer: "City authority (demo data)", applicability: pass1.applicability, validFrom: "2026-01-01T00:00:00Z", validTo: null, constraintsBySection: { "4.2": pass1.constraints } },
      idempotencyKey: "publish:dcc-hgv-cordon:v3",
    },
    "publish_knowledge.response.json": { documentId: "dcc-hgv-cordon", documentVersion: 3, corpusVersion: 7, indexVersion: "idx-7", chunkCount: 12, activatedAt: "2026-11-18T14:02:11Z", duplicate: false },
    "record_outcome.request.json": { operation: "C04.submitDeliveryRequest", outcome: "Forbidden", targetIds: [DEL1], occurredAt: "2026-11-19T10:15:00Z", details: { reason: "provider scope mismatch" } },
    "record_outcome.response.json": { auditId: "2b3c4d5e-6f70-4812-9a3b-4c5d6e7f8091" },
    "record_decision.request.json": decision,
    "record_decision.response.json": { decisionId: D1, recordedAt: "2026-11-20T06:55:10Z", duplicate: false },
    "explain_decision.request.json": { selector: { decisionId: D1 }, language: "en" },
    "explain_decision.ok.response.json": explanationOk,
    "explain_decision.partial.response.json": explanationPartial,
    "get_decision_evidence.response.json": { decisionId: D1, referenceId: P1, kind: "Evidence", status: "Stored", evidence: pass1, alternative: null },
    "error.decision_ambiguous.json": {
      outcome: "Conflict", code: "C10.DECISION_AMBIGUOUS",
      message: "More than one decision matches this subject; specify subjectVersion or decisionId.",
      details: { candidates: [{ decisionId: D1, subjectVersion: 1 }, { decisionId: "b1a2c3d4-e5f6-4789-8abc-def012345678", subjectVersion: 2 }] },
      correlationId: CORR,
    },
  };
  fs.mkdirSync(EX, { recursive: true });
  for (const [name, obj] of Object.entries(files)) fs.writeFileSync(`${EX}/${name}`, JSON.stringify(obj, null, 2) + "\n");
  console.log("wrote", Object.keys(files).length, "examples to", EX);
}
