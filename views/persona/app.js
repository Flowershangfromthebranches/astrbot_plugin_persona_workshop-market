const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = {
  agents: [],
  jobs: [],
  personas: [],
  broker: [],
  active: "",
  agentId: "api_astrbot",
  runtimeSource: "api",
  modelId: "",
  reasoning: "",
};

let currentJobId = "";
let pollTimer = null;
let uploadedMaterials = [];
let confirmResolve = null;
let busy = false;

function unwrap(value) {
  if (value && value.status === "error") {
    const error = new Error(value.message || "操作失败");
    error.data = value.data;
    throw error;
  }
  return value && value.status === "ok" ? value.data : value;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = isError ? "toast error" : "toast";
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, isError ? 8000 : 4000);
}

function applyTheme(context) {
  if (context && typeof context.isDark === "boolean") {
    document.documentElement.dataset.theme = context.isDark ? "dark" : "light";
  }
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  $("tab-create").hidden = name !== "create";
  $("tab-tasks").hidden = name !== "tasks";
  $("tab-library").hidden = name !== "library";
  if (name === "tasks" || name === "library") loadState().catch((error) => showToast(error.message, true));
}

function statusLabel(status) {
  return {
    created: "已创建", planning: "规划中", researching: "研究中", ingesting_sources: "摄取资料",
    extracting: "提取八维", compiling: "编译中", completed: "已完成", completed_with_gaps: "完成（有缺口）",
    waiting_for_materials: "等待资料", failed: "失败", cancelled: "已取消", paused: "已暂停",
    paused_runtime_unavailable: "模型不可用", pause_requested: "正在暂停",
  }[status] || status || "未知";
}

function readyAgents() {
  return (state.agents || []).filter((item) => item.status === "ready");
}

function currentAgent() {
  return readyAgents().find((item) => item.id === state.agentId) || readyAgents()[0] || null;
}

function agentModels() {
  const agent = currentAgent();
  return agent ? { agent, models: agent.models || [] } : { agent: null, models: [] };
}

function agentLabel(agent) {
  const tag = agent.runtime_source === "local_cli" ? "CLI" : (agent.runtime_source === "api" ? "API" : "TEST");
  const research = agent.research && (agent.research.search || agent.research.fetch) ? " · 可研究" : "";
  return `[${tag}] ${agent.name || agent.id}${research}`;
}

function currentResearchCapable() {
  const agent = currentAgent();
  if (agent && agent.research && agent.research.search && agent.research.fetch) return true;
  return state.broker.includes("web_search") && state.broker.includes("web_fetch");
}

function renderAgents() {
  const select = $("pcAgent");
  const previous = select.value;
  const agents = readyAgents();
  select.replaceChildren();
  if (!agents.length) {
    const option = el("option", "", "没有可用的执行器");
    option.value = "";
    select.appendChild(option);
    select.disabled = true;
    $("pcModel").replaceChildren();
    $("pcStart").disabled = true;
    $("pcHint").textContent = "请先在 AstrBot 配置对话模型，或安装可用的本地 CLI Agent";
    return;
  }
  for (const agent of agents) {
    const option = el("option", "", agentLabel(agent));
    option.value = agent.id;
    select.appendChild(option);
  }
  const preferred = agents.find((item) => item.id === state.agentId)
    || agents.find((item) => item.id === previous)
    || agents.find((item) => item.id === "api_astrbot")
    || agents[0];
  select.value = preferred.id;
  state.agentId = preferred.id;
  state.runtimeSource = preferred.runtime_source || "api";
  select.disabled = false;
  renderModelOptions(preferred, state.modelId);
  $("pcStart").disabled = busy;
  $("pcHint").textContent = "";
}

function renderModelOptions(agent, previous) {
  const select = $("pcModel");
  select.replaceChildren();
  const models = agent && agent.models ? agent.models : [];
  if (!models.length) {
    const option = el("option", "", "该执行器没有可用模型");
    option.value = "";
    select.appendChild(option);
    select.disabled = true;
    state.modelId = "";
    renderReasoningOptions(null);
    return;
  }
  for (const model of models) {
    const option = el("option", "", model.display_name || model.id);
    option.value = model.id;
    select.appendChild(option);
  }
  const preferred = models.find((model) => model.id === previous)
    || models.find((model) => model.id === state.modelId)
    || models[0];
  select.value = preferred.id;
  state.modelId = preferred.id;
  select.disabled = false;
  renderReasoningOptions(preferred);
}

function renderReasoningOptions(model) {
  const select = $("pcReasoning");
  select.replaceChildren();
  const capability = model && model.reasoning_capability ? model.reasoning_capability : {};
  const efforts = capability.supported_efforts || [];
  if (!efforts.length) {
    const option = el("option", "", "默认（该执行器不支持思考强度）");
    option.value = "";
    select.appendChild(option);
    select.value = "";
    state.reasoning = "";
    select.disabled = true;
    return;
  }
  const option = el("option", "", "默认");
  option.value = "";
  select.appendChild(option);
  for (const effort of efforts) {
    const item = el("option", "", effort);
    item.value = effort;
    select.appendChild(item);
  }
  select.value = capability.default_effort || "";
  state.reasoning = select.value;
  select.disabled = false;
}

function renderResearchHint() {
  const needsSearch = ["auto", "web", "hybrid"].includes($("pcResearchMode").value)
    && ["fictional_character", "real_person"].includes($("pcSubjectKind").value);
  const hint = $("pcCapabilityHint");
  if (needsSearch) {
    hint.hidden = false;
    if (currentResearchCapable()) {
      hint.className = "hint";
      const agent = currentAgent();
      hint.textContent = agent && agent.runtime_source === "local_cli"
        ? "联网研究将使用当前本地 CLI 执行器自带的研究工具；如果运行时验证失败，任务会按原项目 fail-closed，不会拿模型记忆冒充联网。"
        : "联网研究将使用 AstrBot 已配置的搜索源；如果搜索源不可用，任务会在预检阶段失败。";
    } else {
      hint.className = "warn";
      hint.textContent = "当前执行器没有已验证的联网研究能力，AstrBot 也没有配置搜索源。联网研究会在预检阶段失败；请改用本地资料/引导访谈，或选择带联网工具的 CLI 执行器，或在 AstrBot 中配置 Tavily/Bocha。";
    }
  } else {
    hint.hidden = true;
    hint.textContent = "";
    hint.className = "warn";
  }
  $("pcOcWarning").hidden = $("pcSubjectKind").value !== "original_character";
  const agent = currentAgent();
  $("pcValidateResearch").hidden = !agent || agent.runtime_source !== "local_cli";
}

async function validateResearch() {
  const agent = currentAgent();
  if (!agent || agent.runtime_source !== "local_cli") {
    showToast("只有本地 CLI 执行器需要单独验证联网能力。", true);
    return;
  }
  if (!state.modelId) {
    showToast("请先选择模型", true);
    return;
  }
  const button = $("pcValidateResearch");
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "正在验证（最多 5 分钟）…";
  try {
    const data = unwrap(await bridge.apiPost("page/continuum/research/validate", {
      runtime_source: state.runtimeSource,
      agent_id: state.agentId,
      model_id: state.modelId,
      reasoning_effort: state.reasoning || null,
    }));
    const research = data.probe?.research || {};
    agent.research = research;
    const status = research.verification_status || "unknown";
    showToast(`联网能力验证结果：${status}`);
    renderResearchHint();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

function renderConditionalFields() {
  const kind = $("pcSubjectKind").value;
  $("pcRealFields").hidden = kind !== "real_person";
  $("pcWorkWrap").hidden = kind === "real_person";
  renderResearchHint();
}

function renderCustomPolicy() {
  $("pcCustomPolicy").hidden = $("pcPolicy").value !== "custom";
}

function renderUploads() {
  const root = $("pcUploads");
  root.replaceChildren();
  uploadedMaterials.forEach((item, index) => {
    const row = el("div", "upload-item");
    row.appendChild(el("span", "", `${item.filename} · ${item.size ? Math.round(item.size / 1024) + " KB" : "已上传"}`));
    const remove = el("button", "ghost", "移除");
    remove.type = "button";
    remove.addEventListener("click", () => {
      uploadedMaterials.splice(index, 1);
      renderUploads();
    });
    row.appendChild(remove);
    root.appendChild(row);
  });
}

function renderActiveBadge() {
  const badge = $("activeBadge");
  const current = state.personas.find((item) => item.active);
  if (current) {
    badge.textContent = `当前人格：${current.manifest?.display_name || current.id}`;
    badge.classList.add("ready");
  } else if (state.active) {
    badge.textContent = `当前 AstrBot 人格：${state.active}`;
    badge.classList.remove("ready");
  } else {
    badge.textContent = "当前人格：未设置";
    badge.classList.remove("ready");
  }
  $("jobCount").textContent = state.jobs.length ? String(state.jobs.length) : "";
  $("personaCount").textContent = state.personas.length ? String(state.personas.length) : "";
}

function renderJobList() {
  const root = $("jobList");
  root.replaceChildren();
  if (!state.jobs.length) {
    root.appendChild(el("div", "empty", "还没有制作任务。去“完整制作”创建一个。"));
    return;
  }
  for (const job of state.jobs) {
    const card = el("article", "job-card");
    const head = el("div", "job-card-head");
    const title = el("div");
    title.appendChild(el("h3", "", job.display_name || job.id));
    title.appendChild(el("p", "", `${job.creation_mode || ""} · ${job.agent_id || ""} / ${job.model_id || ""} · ${formatDate(job.created_at)}`));
    head.appendChild(title);
    head.appendChild(el("span", `chip ${job.status === "failed" ? "error" : ""}`, statusLabel(job.status)));
    card.appendChild(head);
    const progress = job.progress || {};
    const bar = el("div", "job-mini-bar");
    const fill = el("span");
    fill.style.width = `${Math.max(0, Math.min(100, Number(progress.percent) || 0))}%`;
    bar.appendChild(fill);
    card.appendChild(bar);
    card.appendChild(el("p", "", `${progress.label || job.current_stage || "—"} · ${progress.percent || 0}% · ${progress.message || ""}`));
    const actions = el("div", "actions");
    const view = el("button", "ghost", "查看进度");
    view.type = "button";
    view.addEventListener("click", () => openJob(job.id));
    actions.appendChild(view);
    card.appendChild(actions);
    root.appendChild(card);
  }
}

function renderPersonaLibrary() {
  const root = $("personaLibrary");
  root.replaceChildren();
  if (!state.personas.length) {
    root.appendChild(el("div", "empty", "还没有完成的人格。先在“完整制作”里创建一个。"));
    return;
  }
  for (const record of state.personas) {
    const manifest = record.manifest || {};
    const card = el("article", `persona-card ${record.active ? "is-active" : ""}`);
    const title = el("h3");
    title.appendChild(document.createTextNode(manifest.display_name || record.id));
    if (record.active) title.appendChild(el("span", "chip active", "使用中"));
    card.appendChild(title);
    card.appendChild(el("p", "summary", manifest.summary || "还没有概述"));
    const meta = el("div", "persona-meta");
    meta.appendChild(el("span", "chip", manifest.compile_state || "未知状态"));
    meta.appendChild(el("span", "chip", `来源 ${manifest.source_count ?? "—"}`));
    meta.appendChild(el("span", "chip", `置信 ${manifest.confidence?.creation_coverage != null ? Math.round(Number(manifest.confidence.creation_coverage) * 100) + "%" : "—"}`));
    if (manifest.created_at) meta.appendChild(el("span", "chip", formatDate(manifest.created_at)));
    card.appendChild(meta);
    const actions = el("div", "actions");
    const use = el("button", "primary", record.active ? "使用中" : "使用");
    use.type = "button";
    use.disabled = record.active;
    use.addEventListener("click", () => usePersona(record.id));
    actions.appendChild(use);
    const remove = el("button", "danger", "删除");
    remove.type = "button";
    remove.disabled = record.active;
    if (record.active) remove.title = "正在使用中，请先切换";
    remove.addEventListener("click", () => deletePersona(record.id, manifest.display_name || record.id));
    actions.appendChild(remove);
    card.appendChild(actions);
    root.appendChild(card);
  }
}

function numberFrom(object, keys) {
  if (!object || typeof object !== "object") return null;
  for (const key of keys) {
    const value = object[key];
    if (typeof value === "number") return value;
    if (typeof value === "string" && value.trim() && !Number.isNaN(Number(value))) return Number(value);
  }
  return null;
}

function renderJob(job) {
  if (!job) return;
  $("pcProgressEmpty").hidden = true;
  $("pcProgressBox").hidden = false;
  const progress = job.progress || {};
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  const status = $("pcJobStatus");
  status.textContent = statusLabel(job.status);
  status.className = `chip ${job.status === "failed" ? "error" : ""}`;
  $("pcStage").textContent = progress.label || job.current_stage || "处理中";
  $("pcPercent").textContent = `${percent}%`;
  $("pcBar").style.width = `${percent}%`;
  $("pcMessage").textContent = progress.message || progress.current_operation || progress.current_item || "";

  const coverage = job.coverage || {};
  const policy = job.research_policy || {};
  const dimension = job.dimension_progress || {};
  const dimensionCount = Object.keys(dimension).length;
  const privateCoverage = job.private_coverage || {};
  $("pcSources").textContent = job.source_count ?? 0;
  $("pcIndependent").textContent = numberFrom(coverage, ["independent_source_count", "independent_sources", "unique_source_count"]) ?? "—";
  $("pcBudget").textContent = policy.soft_max_sources ? `${policy.soft_max_sources}/${policy.hard_max_sources || "—"}` : "—";
  $("pcDimensions").textContent = `${dimensionCount}/8`;
  $("pcLifeStages").textContent = (job.life_stages || []).length || "—";
  $("pcGaps").textContent = (job.research_gaps || []).length;
  $("pcEvidence").textContent = numberFrom(privateCoverage, ["evidence_unit_count", "unit_count", "evidence_units"]) ?? "—";
  $("pcFused").textContent = numberFrom(privateCoverage, ["fused_count", "fused"]) ?? "—";
  $("pcContradictions").textContent = numberFrom(privateCoverage, ["contradiction_count", "contradictions"]) ?? "—";
  $("pcRecords").textContent = numberFrom(privateCoverage, ["record_count", "records"]) ?? "—";
  $("pcBatches").textContent = numberFrom(privateCoverage, ["batch_count", "batches", "analysis_batches"]) ?? "—";
  $("pcStopReason").textContent = job.research_stop_reason ? `停止原因：${job.research_stop_reason}` : "";
  $("pcCoverage").textContent = JSON.stringify({ coverage: job.coverage, gaps: job.research_gaps, policy: job.research_policy }, null, 2);
  $("pcMaterialStats").textContent = JSON.stringify(privateCoverage, null, 2);
  const binding = progress.runtime_binding_snapshot || {};
  $("pcRuntimeAgent").textContent = `Agent: ${job.agent_id || binding.agent_id || "—"}`;
  $("pcRuntimeModel").textContent = `Model: ${job.model_id || binding.model_id || "—"}`;
  $("pcRuntimeStage").textContent = `Stage: ${job.current_stage || "—"}`;

  const questions = job.interview_questions || [];
  const interview = $("pcInterview");
  if (questions.length) {
    const question = questions[questions.length - 1];
    interview.hidden = false;
    $("pcInterviewQuestion").textContent = question.question || question.text || question.prompt || JSON.stringify(question);
  } else {
    interview.hidden = true;
  }

  const statusValue = job.status || "";
  const running = ["created", "planning", "researching", "ingesting_sources", "extracting", "compiling", "pause_requested"].includes(statusValue);
  $("pcPause").hidden = !["created", "planning", "researching", "ingesting_sources", "extracting", "compiling"].includes(statusValue);
  $("pcResume").hidden = !["paused", "paused_runtime_unavailable"].includes(statusValue);
  $("pcRetry").hidden = statusValue !== "failed";
  $("pcContinue").hidden = !["completed", "completed_with_gaps"].includes(statusValue);
  $("pcSwitchModel").hidden = !["paused", "paused_runtime_unavailable", "failed"].includes(statusValue);
  $("pcCancel").hidden = !running;

  const error = $("pcError");
  if (job.error || job.failure_json) {
    error.hidden = false;
    error.textContent = job.error || JSON.stringify(job.failure_json, null, 2);
  } else {
    error.hidden = true;
    error.textContent = "";
  }

  const eventsRoot = $("pcEvents");
  eventsRoot.replaceChildren();
  const events = (job.events || []).slice(-12).reverse();
  if (!events.length) eventsRoot.appendChild(el("div", "event", "暂无事件"));
  for (const event of events) {
    const type = event.event || event.event_type || event.type || event.stage || "event";
    const message = event.message || event.detail || event.summary || "";
    const row = el("div", "event");
    row.appendChild(el("b", "", type));
    if (message) row.appendChild(document.createTextNode(` · ${message}`));
    if (event.created_at) row.appendChild(document.createTextNode(` · ${formatDate(event.created_at)}`));
    eventsRoot.appendChild(row);
  }
  $("pcRaw").textContent = JSON.stringify(job, null, 2);
}

async function loadState(refresh = false) {
  const data = unwrap(await bridge.apiGet("page/continuum/state", refresh ? { refresh: "1" } : {}));
  state.agents = data.agents || [];
  state.jobs = data.jobs || [];
  state.broker = data.broker_capabilities || [];
  const personas = unwrap(await bridge.apiGet("page/continuum/personas", {}));
  state.personas = personas.personas || [];
  state.active = personas.active_persona_id || "";
  renderAgents();
  renderActiveBadge();
  renderJobList();
  renderPersonaLibrary();
  renderResearchHint();
}

function deriveTypeAndMode() {
  const subject = $("pcSubjectKind").value;
  const research = $("pcResearchMode").value;
  const privacy = $("pcPrivacyScope").value;
  const life = $("pcLifeStatus").value;
  let type = "public_living_person";
  let mode = "public_research";
  if (subject === "real_person") {
    if (privacy === "private") {
      type = life === "deceased" ? "private_deceased_person" : "private_living_person";
      mode = research === "guided_interview" ? "guided_interview" : "private_materials";
    } else {
      type = life === "deceased" ? "public_historical_person" : "public_living_person";
      mode = "public_research";
    }
  } else {
    type = "fictional_or_synthetic_person";
    if (["web", "hybrid", "auto"].includes(research) && subject === "fictional_character") {
      mode = "public_research";
    } else if (research === "guided_interview") {
      mode = "guided_interview";
    } else if (research === "local") {
      mode = "private_materials";
    } else {
      mode = "fictional";
    }
  }
  const webScope = subject === "original_character"
    ? "background_only"
    : (subject === "fictional_character" ? "identity_and_canon" : "identity_and_biography");
  const needsWeb = mode === "public_research";
  return { type, mode, needsWeb, webScope };
}

function buildResearchPolicy() {
  const profile = $("pcPolicy").value;
  const policy = { profile };
  if (profile === "custom") {
    policy.min_unique_sources = Number($("pcCustomMin").value || 30);
    policy.preferred_source_target = Number($("pcCustomTarget").value || 60);
    policy.soft_max_sources = Number($("pcCustomSoft").value || 100);
    policy.hard_max_sources = Number($("pcCustomHard").value || 150);
    policy.min_source_categories = Number($("pcCustomCategories").value || 6);
    policy.min_sources_per_dimension = Number($("pcCustomDimension").value || 4);
  }
  return policy;
}

function buildMaterials() {
  const text = $("pcMaterials").value.trim();
  const materials = text
    ? text.split(/\n\s*\n/).map((part, index) => ({
        title: `用户资料 ${index + 1}`,
        source_type: "user_provided",
        content: part.trim(),
      })).filter((item) => item.content)
    : [];
  for (const item of uploadedMaterials) {
    materials.push({ upload_id: item.upload_id, filename: item.filename, source_type: "user_file" });
  }
  return materials;
}

async function uploadFile(file) {
  const data = unwrap(await bridge.upload("page/continuum/material/upload", file));
  uploadedMaterials.push({
    upload_id: data.upload_id,
    filename: data.filename || file.name,
    size: data.size || file.size,
  });
  renderUploads();
}

async function handleFiles(files) {
  for (const file of Array.from(files || [])) {
    try {
      $("pcHint").textContent = `正在上传 ${file.name}…`;
      await uploadFile(file);
      $("pcHint").textContent = "";
    } catch (error) {
      showToast(`${file.name} 上传失败：${error.message}`, true);
    }
  }
  $("pcFiles").value = "";
}

async function startCreation() {
  if (busy) return;
  const name = $("pcName").value.trim();
  if (!name) {
    showToast("请先填写名称", true);
    $("pcName").focus();
    return;
  }
  const { type, mode, needsWeb, webScope } = deriveTypeAndMode();
  if (needsWeb && !currentResearchCapable()) {
    showToast("当前执行器没有可用的联网研究能力，也没有配置 AstrBot 搜索源。请改用本地资料/引导访谈，或选择带联网工具的 CLI 执行器。", true);
    return;
  }
  const materials = buildMaterials();
  const subject = $("pcSubjectKind").value;
  const privacy = $("pcPrivacyScope").value;
  const privateRemote = (subject === "real_person" && privacy === "private") && materials.length;
  if (privateRemote && !$("pcRemoteConsent").checked) {
    showToast("私人人物 + 远程模型时必须勾选“允许将私人资料发送给远程模型 Provider”。", true);
    return;
  }
  const { agent, models } = agentModels();
  if (!agent || !models.length) {
    showToast("没有可用的 AstrBot 模型", true);
    return;
  }
  const payload = {
    display_name: name,
    aliases: $("pcAliases").value.split(",").map((item) => item.trim()).filter(Boolean),
    persona_type: type,
    creation_mode: mode,
    subject_kind: subject,
    work_or_universe: $("pcWorkUniverse").value.trim() || null,
    life_status: $("pcLifeStatus").value,
    privacy_scope: privacy,
    persona_notes: $("pcNotes").value.trim(),
    research_mode: $("pcResearchMode").value,
    web_scope: webScope,
    birth_date: $("pcBirth").value || null,
    death_date: $("pcDeath").value || null,
    data_cutoff_date: $("pcCutoff").value || null,
    runtime_source: state.runtimeSource,
    agent_id: state.agentId,
    model_id: state.modelId,
    reasoning_effort: state.reasoning || null,
    research_policy: buildResearchPolicy(),
    materials,
    material_semantic_gate_mode: $("pcGateMode").value,
    remote_material_consent: $("pcRemoteConsent").checked,
    start_worker: true,
  };
  setBusy(true);
  try {
    let data;
    try {
      data = unwrap(await bridge.apiPost("page/continuum/job/create", payload));
    } catch (error) {
      if (error.message === "persona_exists" && error.data?.persona_id) {
        const display = error.data.display_name || error.data.persona_id;
        if (!(await confirmDialog(`已存在 Persona：${display}\n是否继续补充已有 Persona？`))) {
          setBusy(false);
          return;
        }
        payload.existing_persona_id = error.data.persona_id;
        payload.duplicate_action = "enrich";
        data = unwrap(await bridge.apiPost("page/continuum/job/create", payload));
      } else {
        throw error;
      }
    }
    const job = data.job;
    uploadedMaterials = [];
    renderUploads();
    showToast("制作任务已创建。");
    if (job?.id) {
      currentJobId = job.id;
      renderJob(job);
      startPolling();
      await loadState();
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
  }
}

function setBusy(value) {
  busy = value;
  $("pcStart").disabled = value || !agentModels().models.length;
  $("pcUploads").querySelectorAll("button").forEach((button) => { button.disabled = value; });
}

function startPolling() {
  clearTimeout(pollTimer);
  if (!currentJobId) return;
  const tick = async () => {
    try {
      const data = unwrap(await bridge.apiGet("page/continuum/job/status", { job_id: currentJobId }));
      const job = data.job;
      renderJob(job);
      if (["completed", "completed_with_gaps", "failed", "cancelled", "paused", "paused_runtime_unavailable"].includes(job.status)) {
        await loadState();
        return;
      }
    } catch (error) {
      showToast(error.message, true);
      return;
    }
    pollTimer = setTimeout(tick, 1800);
  };
  pollTimer = setTimeout(tick, 500);
}

async function openJob(jobId) {
  currentJobId = jobId;
  switchTab("create");
  try {
    const data = unwrap(await bridge.apiGet("page/continuum/job/status", { job_id: jobId }));
    renderJob(data.job);
    startPolling();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function jobAction(action, extra = {}) {
  if (!currentJobId) return;
  try {
    const data = unwrap(await bridge.apiPost("page/continuum/job/action", {
      job_id: currentJobId,
      action,
      ...extra,
    }));
    renderJob(data.job);
    await loadState();
    if (["completed", "completed_with_gaps", "failed", "cancelled", "paused", "paused_runtime_unavailable"].includes(data.job.status)) {
      clearTimeout(pollTimer);
    } else {
      startPolling();
    }
    showToast("任务状态已更新。");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function submitInterview() {
  const answer = $("pcInterviewAnswer").value.trim();
  if (!answer) return;
  await jobAction("interview", { answer });
  $("pcInterviewAnswer").value = "";
}

async function switchModelAndContinue() {
  if (!currentJobId) return;
  const jobData = unwrap(await bridge.apiGet("page/continuum/job/status", { job_id: currentJobId }));
  const status = jobData.job.status;
  const runtime = { runtime_source: state.runtimeSource, agent_id: state.agentId, model_id: state.modelId, reasoning_effort: state.reasoning || null };
  await jobAction(status === "failed" ? "retry" : "resume", { runtime });
}

async function usePersona(id) {
  try {
    const data = unwrap(await bridge.apiPost("page/continuum/persona/use", { id }));
    showToast(`已启用：${data.active_persona_id}`);
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function deletePersona(id, name) {
  if (!(await confirmDialog(`确定删除人格“${name}”吗？\n会同时删除 continuum 数据和对应的 AstrBot 原生人格。`))) return;
  try {
    unwrap(await bridge.apiPost("page/continuum/persona/delete", { id }));
    showToast("人格已删除。");
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
}

function resetForm() {
  if (busy) return;
  for (const id of ["pcName", "pcAliases", "pcWorkUniverse", "pcNotes", "pcMaterials", "pcBirth", "pcDeath", "pcCutoff"]) {
    $(id).value = "";
  }
  $("pcSubjectKind").value = "fictional_character";
  $("pcResearchMode").value = "auto";
  $("pcPolicy").value = "deep";
  $("pcGateMode").value = "auto";
  $("pcRemoteConsent").checked = false;
  uploadedMaterials = [];
  renderUploads();
  renderConditionalFields();
  renderCustomPolicy();
  $("pcProgressBox").hidden = true;
  $("pcProgressEmpty").hidden = false;
  currentJobId = "";
  clearTimeout(pollTimer);
}

function confirmDialog(message) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $("confirmMessage").textContent = message;
    $("confirmOverlay").hidden = false;
    $("confirmOk").focus();
  });
}

function closeConfirm(value) {
  $("confirmOverlay").hidden = true;
  if (confirmResolve) {
    const resolve = confirmResolve;
    confirmResolve = null;
    resolve(value);
  }
}

function bindEvents() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });
  $("pcSubjectKind").addEventListener("change", renderConditionalFields);
  $("pcResearchMode").addEventListener("change", renderResearchHint);
  $("pcPolicy").addEventListener("change", renderCustomPolicy);
  $("pcModel").addEventListener("change", () => {
    state.modelId = $("pcModel").value;
    renderReasoningOptions((agentModels().models || []).find((model) => model.id === state.modelId) || null);
  });
  $("pcAgent").addEventListener("change", () => {
    const agent = readyAgents().find((item) => item.id === $("pcAgent").value);
    if (agent) {
      state.agentId = agent.id;
      state.runtimeSource = agent.runtime_source || "api";
      state.modelId = "";
      renderModelOptions(agent, "");
      renderResearchHint();
    }
  });
  $("pcReasoning").addEventListener("change", () => { state.reasoning = $("pcReasoning").value; });
  $("pcStart").addEventListener("click", startCreation);
  $("pcValidateResearch").addEventListener("click", validateResearch);
  $("pcReset").addEventListener("click", resetForm);
  $("pcFiles").addEventListener("change", (event) => handleFiles(event.target.files));
  $("pcPause").addEventListener("click", () => jobAction("pause"));
  $("pcResume").addEventListener("click", () => jobAction("resume"));
  $("pcRetry").addEventListener("click", () => jobAction("retry"));
  $("pcContinue").addEventListener("click", () => jobAction("continue", { research_policy: buildResearchPolicy() }));
  $("pcSwitchModel").addEventListener("click", switchModelAndContinue);
  $("pcCancel").addEventListener("click", async () => {
    if (await confirmDialog("确定取消这个制作任务吗？已完成的证据和检查点会保留。")) {
      await jobAction("cancel");
    }
  });
  $("pcInterviewSubmit").addEventListener("click", submitInterview);
  $("refreshJobs").addEventListener("click", () => loadState(true).catch((error) => showToast(error.message, true)));
  $("refreshPersonas").addEventListener("click", () => loadState(true).catch((error) => showToast(error.message, true)));
  $("confirmCancel").addEventListener("click", () => closeConfirm(false));
  $("confirmOk").addEventListener("click", () => closeConfirm(true));
  $("confirmOverlay").addEventListener("click", (event) => {
    if (event.target === $("confirmOverlay")) closeConfirm(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("confirmOverlay").hidden) closeConfirm(false);
  });
}

(async () => {
  if (!bridge) {
    document.body.textContent = "请从 AstrBot 插件页面打开人格工坊。";
    return;
  }
  try {
    const context = await bridge.ready();
    applyTheme(context);
    if (typeof bridge.onContext === "function") bridge.onContext(applyTheme);
    for (const id of ["pcBirth", "pcDeath", "pcCutoff"]) {
      const input = $(id);
      input.value = "";
      input.required = false;
    }
    bindEvents();
    renderConditionalFields();
    renderCustomPolicy();
    await loadState();
  } catch (error) {
    showToast(error.message || "页面初始化失败", true);
    console.error(error);
  }
})();
