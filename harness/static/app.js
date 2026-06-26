"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let sessionState = null;
let pendingAttachments = [];
let streaming = false;
let currentWsPath = "";
let currentFilePath = null;

const KIND_ICON = { image: "🖼", audio: "🎵", video: "🎬" };

// ---------- helpers --------------------------------------------------------

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function setupMarkdown() {
  marked.setOptions({
    highlight: (code, lang) => {
      try {
        return hljs.highlight(code, { language: lang || "plaintext" }).value;
      } catch (_) { return hljs.highlightAuto(code).value; }
    },
    breaks: true,
    gfm: true,
  });
}

async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  const res = await fetch(path, init);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

function kindIcon(k) { return KIND_ICON[k] || "📄"; }

function formatSize(b) {
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  return `${(b / 1024 ** 3).toFixed(2)} GB`;
}

function fileExtension(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

function workspaceIcon(entry) {
  if (entry.is_dir) return "📁";
  const ext = fileExtension(entry.name);
  if (["py", "js", "ts", "jsx", "tsx", "rb", "go", "rs", "c", "cpp", "h", "java"].includes(ext)) return "📄";
  if (["md", "txt", "log", "json", "yml", "yaml", "toml", "ini"].includes(ext)) return "📝";
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp"].includes(ext)) return "🖼";
  if (["wav", "mp3", "flac", "ogg"].includes(ext)) return "🎵";
  if (["mp4", "mov", "mkv", "webm"].includes(ext)) return "🎬";
  return "📎";
}

// ---------- chat rendering -------------------------------------------------

function emptyMessages() { $("#messages").innerHTML = ""; }

function ensureTurnGroup(role) {
  // Returns the trailing turn-group element of `role`, creating one if needed.
  const messages = $("#messages");
  let last = messages.lastElementChild;
  if (last && last.classList.contains("message") && last.dataset.role === role) {
    return last;
  }
  const div = document.createElement("div");
  div.className = `message message-${role}`;
  div.dataset.role = role;
  messages.appendChild(div);
  return div;
}

function newRoundElement(roundId) {
  const round = document.createElement("div");
  round.className = "round";
  round.dataset.roundId = roundId;
  const reasoning = document.createElement("details");
  reasoning.className = "reasoning hidden";
  reasoning.innerHTML = `<summary>Thinking…</summary><div class="reasoning-content"></div>`;
  const content = document.createElement("div");
  content.className = "content";
  round.appendChild(reasoning);
  round.appendChild(content);
  return round;
}

function getRoundElement(turnGroup, roundId) {
  let round = turnGroup.querySelector(`.round[data-round-id="${roundId}"]`);
  if (!round) {
    round = newRoundElement(roundId);
    turnGroup.appendChild(round);
  }
  return round;
}

function renderUserMessage(m) {
  const div = document.createElement("div");
  div.className = "message message-user";
  div.dataset.role = "user";
  if (m.attachments && m.attachments.length) {
    const atts = document.createElement("div");
    atts.className = "attachments";
    for (const a of m.attachments) {
      const chip = document.createElement("span");
      chip.className = `attachment attachment-${a.kind}`;
      chip.textContent = `${kindIcon(a.kind)} ${a.display_name}`;
      atts.appendChild(chip);
    }
    div.appendChild(atts);
  }
  const c = document.createElement("div");
  c.className = "content";
  const text = m.text || (typeof m.content === "string" ? m.content : "");
  c.innerHTML = marked.parse(text || "");
  div.appendChild(c);
  $("#messages").appendChild(div);
}

function renderAssistantRoundFromHistory(m) {
  const turn = ensureTurnGroup("assistant");
  const round = getRoundElement(turn, m.round_id || `r-${Date.now()}`);
  if (m.reasoning) {
    const det = round.querySelector(".reasoning");
    det.classList.remove("hidden");
    det.querySelector("summary").textContent = "Thinking";
    det.querySelector(".reasoning-content").innerHTML = marked.parse(m.reasoning);
  }
  if (m.content) {
    round.querySelector(".content").innerHTML = marked.parse(m.content);
  }
  if (m.tool_calls) {
    for (const tc of m.tool_calls) {
      const card = ensureToolCard(round, tc.id, tc.function.name);
      card.querySelector(".args").textContent = tc.function.arguments;
      card.querySelector(".status").textContent = "called";
    }
  }
}

function renderToolMessageFromHistory(m) {
  const turn = ensureTurnGroup("assistant");
  const round = getRoundElement(turn, m.round_id || `r-${Date.now()}`);
  const card = ensureToolCard(round, m.tool_call_id, m.name || "");
  let result;
  try { result = JSON.parse(m.content); } catch (_) { result = m.content; }
  card.querySelector(".result").textContent = formatToolResult(result);
  card.querySelector(".status").textContent = result && result.error ? "error" : "done";
  if (result && result.error) card.querySelector(".status").classList.add("error");
  card.classList.add("collapsed");
}

function ensureToolCard(roundEl, callId, name) {
  let card = roundEl.querySelector(`.tool-call[data-id="${callId}"]`);
  if (card) return card;
  card = document.createElement("div");
  card.className = "tool-call";
  card.dataset.id = callId;
  card.innerHTML = `
    <div class="head">
      <span class="ic">🔧</span>
      <span class="name">${escapeHtml(name)}</span>
      <span class="status running">running</span>
    </div>
    <div class="body">
      <div class="label">Arguments</div>
      <div class="args"></div>
      <div class="label">Result</div>
      <div class="result">…</div>
    </div>`;
  card.querySelector(".head").addEventListener("click", () => card.classList.toggle("collapsed"));
  roundEl.appendChild(card);
  return card;
}

function formatToolResult(result) {
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result, null, 2);
  } catch (_) {
    return String(result);
  }
}

function renderAll() {
  emptyMessages();
  for (const m of sessionState.messages) {
    if (m.role === "user") renderUserMessage(m);
    else if (m.role === "assistant") renderAssistantRoundFromHistory(m);
    else if (m.role === "tool") renderToolMessageFromHistory(m);
  }
  scrollToBottom();
}

function scrollToBottom() {
  const m = $("#messages");
  m.scrollTop = m.scrollHeight;
}

// ---------- settings drawer ------------------------------------------------

function populateSettings() {
  $("#system-prompt").value = sessionState.system_prompt || "";
  const s = sessionState.settings;
  $("#set-temperature").value = s.temperature;
  $("#set-top-p").value = s.top_p;
  $("#set-max-tokens").value = s.max_tokens;
  $("#set-enable-thinking").checked = !!s.enable_thinking;
  $("#set-thinking-budget").value = s.thinking_token_budget;
  $("#set-tools-enabled").checked = s.tools_enabled !== false;
  $("#set-use-audio-in-video").checked = !!s.use_audio_in_video;
  renderPromptHistory();
}

function renderPromptHistory() {
  const ol = $("#prompt-history");
  ol.innerHTML = "";
  const history = (sessionState.system_prompt_history || []).slice().reverse();
  for (const h of history) {
    const li = document.createElement("li");
    const ts = new Date(h.ts * 1000).toLocaleString();
    li.innerHTML = `
      <div class="hist-head">
        <small>${escapeHtml(ts)} · ${escapeHtml(h.source)}</small>
        <button class="hist-restore" type="button">restore</button>
      </div>
      <pre>${escapeHtml(h.content)}</pre>`;
    li.querySelector(".hist-restore").addEventListener("click", async () => {
      await fetch("/api/session/system-prompt", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: h.content, source: "user" }),
      });
      await loadSession();
    });
    ol.appendChild(li);
  }
}

async function loadSession() {
  sessionState = await api("/api/session");
  renderAll();
  populateSettings();
}

// ---------- threads (multiple conversations) -------------------------------

function relTime(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

async function loadThreads() {
  const data = await api("/api/threads");
  renderThreads(data.threads);
}

function renderThreads(threads) {
  const ul = $("#thread-list");
  ul.innerHTML = "";
  if (!threads.length) {
    ul.innerHTML = `<li class="empty">No conversations yet.</li>`;
    return;
  }
  for (const t of threads) {
    const li = document.createElement("li");
    li.className = "thread" + (t.active ? " active" : "");
    li.title = t.updated_at ? new Date(t.updated_at * 1000).toLocaleString() : "";
    li.innerHTML = `
      <span class="title">${escapeHtml(t.title)}</span>
      <span class="meta">
        <span>${t.message_count} msg · ${escapeHtml(relTime(t.updated_at))}</span>
        <span class="spacer"></span>
        <button class="act rename" title="Rename">✎</button>
        <button class="act del" title="Delete">🗑</button>
      </span>`;
    li.addEventListener("click", (e) => {
      if (e.target.closest(".act")) return;
      if (!t.active) switchThread(t.id);
    });
    li.querySelector(".rename").addEventListener("click", async (e) => {
      e.stopPropagation();
      const name = prompt("Rename conversation:", t.title);
      if (name == null) return;
      await fetch(`/api/threads/${t.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: name }),
      });
      await loadThreads();
    });
    li.querySelector(".del").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete "${t.title}"? This removes its history (Nemo's workspace and memory are untouched).`)) return;
      await api(`/api/threads/${t.id}`, { method: "DELETE" });
      await loadThreads();
      if (t.active) await loadSession();   // server switched us to another thread
    });
    ul.appendChild(li);
  }
}

async function switchThread(id) {
  await api("/api/threads/activate", { method: "POST", body: JSON.stringify({ id }) });
  await loadSession();
  await loadThreads();
}

async function newThread() {
  await api("/api/threads", { method: "POST", body: JSON.stringify({}) });
  await loadSession();
  await loadThreads();
}

function maybeRefreshThreads() {
  if ($("#threads-drawer").classList.contains("open")) loadThreads();
}

// ---------- attachments ----------------------------------------------------

async function uploadFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

function checkAttachmentLimit(kind) {
  const same = pendingAttachments.filter((a) => a.kind === kind).length;
  if (same >= 1) {
    alert(`Only 1 ${kind} per prompt — remove the existing one first.`);
    return false;
  }
  return true;
}

function addAttachment(att) {
  pendingAttachments.push(att);
  renderPendingAttachments();
}

function removeAttachment(idx) {
  pendingAttachments.splice(idx, 1);
  renderPendingAttachments();
}

function renderPendingAttachments() {
  const el = $("#attachments");
  el.innerHTML = "";
  pendingAttachments.forEach((a, i) => {
    const chip = document.createElement("span");
    chip.className = `attachment attachment-${a.kind}`;
    chip.innerHTML = `${kindIcon(a.kind)} ${escapeHtml(a.display_name)} <button class="remove" data-idx="${i}" type="button">×</button>`;
    el.appendChild(chip);
  });
}

$("#attachments").addEventListener("click", (e) => {
  if (e.target.classList.contains("remove")) {
    removeAttachment(parseInt(e.target.dataset.idx, 10));
  }
});

$("#btn-attach").addEventListener("click", () => $("#file-input").click());

$("#file-input").addEventListener("change", async (e) => {
  for (const f of e.target.files) {
    try {
      const att = await uploadFile(f);
      if (!checkAttachmentLimit(att.kind)) continue;
      addAttachment(att);
    } catch (err) { alert(`Upload failed: ${err.message}`); }
  }
  e.target.value = "";
});

let dragCounter = 0;
document.addEventListener("dragenter", (e) => {
  e.preventDefault();
  dragCounter++;
  document.body.classList.add("dragging");
});
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("dragleave", () => {
  dragCounter--;
  if (dragCounter <= 0) { dragCounter = 0; document.body.classList.remove("dragging"); }
});
document.addEventListener("drop", async (e) => {
  e.preventDefault();
  dragCounter = 0;
  document.body.classList.remove("dragging");
  const files = e.dataTransfer && e.dataTransfer.files ? Array.from(e.dataTransfer.files) : [];
  for (const f of files) {
    try {
      const att = await uploadFile(f);
      if (!checkAttachmentLimit(att.kind)) continue;
      addAttachment(att);
    } catch (err) { alert(`Upload failed: ${err.message}`); }
  }
});

// ---------- composer + chat streaming --------------------------------------

const inputEl = $("#input");
function autoSize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 240) + "px";
}
inputEl.addEventListener("input", autoSize);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});

$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (streaming) return;
  const text = inputEl.value.trim();
  if (!text && pendingAttachments.length === 0) return;

  const userAtts = pendingAttachments.slice();
  renderUserMessage({ role: "user", text, attachments: userAtts });

  inputEl.value = "";
  autoSize();
  pendingAttachments = [];
  renderPendingAttachments();
  scrollToBottom();

  streaming = true;
  $("#btn-send").disabled = true;

  // round buffers, keyed by round_id
  const roundBuffers = {};

  function bufferFor(roundId) {
    if (!roundBuffers[roundId]) {
      roundBuffers[roundId] = { reasoning: "", content: "", toolArgs: {} };
    }
    return roundBuffers[roundId];
  }

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, attachments: userAtts }),
    });
    if (!res.ok || !res.body) {
      const turn = ensureTurnGroup("assistant");
      const r = getRoundElement(turn, "err");
      r.querySelector(".content").innerHTML = `<div class="error">Error: ${escapeHtml(res.statusText)}</div>`;
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const events = buf.split("\n\n");
      buf = events.pop();
      for (const evtRaw of events) {
        const lines = evtRaw.split("\n");
        let event = "message";
        let data = "";
        for (const ln of lines) {
          if (ln.startsWith("event:")) event = ln.slice(6).trim();
          else if (ln.startsWith("data:")) data += ln.slice(5).trim();
        }
        if (!data) continue;
        let parsed;
        try { parsed = JSON.parse(data); } catch (_) { continue; }

        await handleStreamEvent(event, parsed, bufferFor);
        scrollToBottom();
      }
    }
  } catch (err) {
    const turn = ensureTurnGroup("assistant");
    const r = getRoundElement(turn, "err");
    r.querySelector(".content").innerHTML = `<div class="error">${escapeHtml(err.message)}</div>`;
  } finally {
    streaming = false;
    $("#btn-send").disabled = false;
    await loadSession();           // re-sync from server's persisted state
    refreshWorkspace();            // tools may have written files
    maybeRefreshThreads();         // title/count/order may have changed
  }
});

async function handleStreamEvent(event, data, bufferFor) {
  if (event === "round_start") {
    const turn = ensureTurnGroup("assistant");
    getRoundElement(turn, data.round_id);
    return;
  }
  if (event === "reasoning") {
    const buf = bufferFor(data.round_id);
    buf.reasoning += data.delta;
    const turn = ensureTurnGroup("assistant");
    const round = getRoundElement(turn, data.round_id);
    const det = round.querySelector(".reasoning");
    det.classList.remove("hidden");
    det.open = true;
    det.querySelector("summary").textContent = "Thinking…";
    det.querySelector(".reasoning-content").innerHTML = marked.parse(buf.reasoning);
    return;
  }
  if (event === "content") {
    const buf = bufferFor(data.round_id);
    buf.content += data.delta;
    const turn = ensureTurnGroup("assistant");
    const round = getRoundElement(turn, data.round_id);
    round.querySelector(".content").innerHTML = marked.parse(buf.content);
    // Once content starts, collapse the thinking block
    const det = round.querySelector(".reasoning");
    if (!det.classList.contains("hidden")) {
      det.open = false;
      det.querySelector("summary").textContent = "Thinking";
    }
    return;
  }
  if (event === "tool_call_start") {
    const turn = ensureTurnGroup("assistant");
    const round = getRoundElement(turn, data.round_id);
    ensureToolCard(round, data.id, data.name);
    bufferFor(data.round_id).toolArgs[data.id] = "";
    return;
  }
  if (event === "tool_call_args") {
    const buf = bufferFor(data.round_id);
    // We don't always know the call id here (vLLM sends `index`).
    // Find the most recent card by index order.
    const turn = ensureTurnGroup("assistant");
    const round = getRoundElement(turn, data.round_id);
    const cards = round.querySelectorAll(".tool-call");
    const card = cards[data.index];
    if (!card) return;
    const id = card.dataset.id;
    buf.toolArgs[id] = (buf.toolArgs[id] || "") + data.delta;
    card.querySelector(".args").textContent = buf.toolArgs[id];
    return;
  }
  if (event === "tool_call_exec") {
    const turn = ensureTurnGroup("assistant");
    const round = getRoundElement(turn, data.round_id);
    const card = round.querySelector(`.tool-call[data-id="${data.id}"]`);
    if (card) {
      card.querySelector(".status").textContent = "running";
      card.querySelector(".status").classList.remove("error");
    }
    return;
  }
  if (event === "tool_result") {
    const turn = ensureTurnGroup("assistant");
    const round = getRoundElement(turn, data.round_id);
    const card = round.querySelector(`.tool-call[data-id="${data.id}"]`);
    if (card) {
      card.querySelector(".result").textContent = formatToolResult(data.result);
      const status = card.querySelector(".status");
      status.classList.remove("running");
      status.textContent = data.is_error ? "error" : "done";
      if (data.is_error) status.classList.add("error");
      card.classList.add("collapsed");
    }
    refreshWorkspace();   // a tool just touched the workspace
    return;
  }
  if (event === "done" || event === "error") {
    if (event === "error") {
      const turn = ensureTurnGroup("assistant");
      const r = getRoundElement(turn, data.round_id || "err");
      r.querySelector(".content").innerHTML += `<div class="error">${escapeHtml(data.message)}</div>`;
    }
    return;
  }
}

// ---------- drawers --------------------------------------------------------

$("#btn-threads").addEventListener("click", async () => {
  const drawer = $("#threads-drawer");
  // Threads and workspace share the left rail — don't stack them.
  $("#workspace-drawer").classList.remove("open");
  drawer.classList.toggle("open");
  // Squeeze the chat aside like the workspace drawer instead of overlaying it.
  $("#app").classList.toggle("ws-open", drawer.classList.contains("open"));
  if (drawer.classList.contains("open")) await loadThreads();
});
$("#btn-thread-new").addEventListener("click", newThread);

$("#btn-workspace").addEventListener("click", async () => {
  const drawer = $("#workspace-drawer");
  $("#threads-drawer").classList.remove("open");
  drawer.classList.toggle("open");
  $("#app").classList.toggle("ws-open", drawer.classList.contains("open"));
  if (drawer.classList.contains("open")) await refreshWorkspace();
});
$("#btn-settings").addEventListener("click", () => $("#settings-drawer").classList.toggle("open"));
$("#btn-files").addEventListener("click", async () => {
  const drawer = $("#files-drawer");
  drawer.classList.toggle("open");
  if (drawer.classList.contains("open")) await browseHost("");
});

// --- Preview pane: window onto the app Nemo serves on the preview port ---
async function refreshPreviewStatus() {
  const el = $("#preview-status");
  try {
    const d = await (await fetch("/api/preview/status")).json();
    el.textContent = d.up ? "● live" : `○ no app on :${d.port}`;
    el.classList.toggle("up", !!d.up);
  } catch {
    el.textContent = "";
    el.classList.remove("up");
  }
}
function loadPreview() {
  $("#preview-frame").src = "/preview/?t=" + Date.now();  // cache-bust the reload
  refreshPreviewStatus();
}
$("#btn-preview").addEventListener("click", () => {
  const pane = $("#preview-pane");
  pane.classList.toggle("open");
  const open = pane.classList.contains("open");
  $("#app").classList.toggle("preview-open", open);
  if (open) loadPreview();
});
$("#btn-preview-reload").addEventListener("click", loadPreview);
$("#btn-preview-open").addEventListener("click", () => window.open("/preview/", "_blank"));

$$(".close").forEach((btn) => btn.addEventListener("click", () => {
  $(`#${btn.dataset.target}`).classList.remove("open");
  if (btn.dataset.target === "workspace-drawer" || btn.dataset.target === "threads-drawer") $("#app").classList.remove("ws-open");
  if (btn.dataset.target === "preview-pane") $("#app").classList.remove("preview-open");
}));

// system prompt save
$("#btn-save-prompt").addEventListener("click", async () => {
  const content = $("#system-prompt").value;
  await fetch("/api/session/system-prompt", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, source: "user" }),
  });
  await loadSession();
});

const NUMBER_FIELDS = {
  "set-temperature": "temperature",
  "set-top-p": "top_p",
  "set-max-tokens": "max_tokens",
  "set-thinking-budget": "thinking_token_budget",
};
for (const [elId, key] of Object.entries(NUMBER_FIELDS)) {
  document.getElementById(elId).addEventListener("change", async (e) => {
    const val = parseFloat(e.target.value);
    if (Number.isNaN(val)) return;
    await fetch("/api/session/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [key]: val }),
    });
  });
}

const BOOL_FIELDS = {
  "set-enable-thinking": "enable_thinking",
  "set-tools-enabled": "tools_enabled",
  "set-use-audio-in-video": "use_audio_in_video",
};
for (const [elId, key] of Object.entries(BOOL_FIELDS)) {
  document.getElementById(elId).addEventListener("change", async (e) => {
    await fetch("/api/session/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [key]: e.target.checked }),
    });
  });
}

// "+ new" now spins up a fresh conversation/thread instead of wiping the
// current one — the old conversation stays around in the threads list.
$("#btn-reset").addEventListener("click", newThread);

// ---------- host file browser ----------------------------------------------

async function browseHost(path) {
  let data;
  try { data = await api(`/api/files?path=${encodeURIComponent(path)}`); }
  catch (err) {
    $("#file-list").innerHTML = `<li class="error">${escapeHtml(err.message)}</li>`;
    return;
  }
  $("#file-path").textContent = "/" + (data.path || "");
  const ul = $("#file-list");
  ul.innerHTML = "";
  if (data.path && data.path !== "") {
    const up = document.createElement("li");
    up.className = "file dir";
    up.textContent = "📁 ..";
    up.addEventListener("click", () => browseHost(data.parent || ""));
    ul.appendChild(up);
  }
  for (const e of data.entries) {
    const li = document.createElement("li");
    const supported = e.is_dir || !!e.kind;
    li.className = "file" + (e.is_dir ? " dir" : "") + (!supported ? " unsupported" : "");
    const icon = e.is_dir ? "📁" : kindIcon(e.kind);
    const sizeStr = e.is_dir ? "" : formatSize(e.size);
    li.innerHTML = `<span class="ic">${icon}</span><span>${escapeHtml(e.name)}</span><small>${sizeStr}</small>`;
    if (e.is_dir) {
      li.addEventListener("click", () => {
        const next = data.path ? `${data.path}/${e.name}` : e.name;
        browseHost(next);
      });
    } else if (e.kind) {
      li.addEventListener("click", async () => {
        if (!checkAttachmentLimit(e.kind)) return;
        const full = data.path ? `${data.path}/${e.name}` : e.name;
        try {
          const att = await api("/api/files/select", {
            method: "POST",
            body: JSON.stringify({ path: full }),
          });
          addAttachment(att);
          $("#files-drawer").classList.remove("open");
        } catch (err) { alert(`Could not attach: ${err.message}`); }
      });
    }
    ul.appendChild(li);
  }
}

// ---------- workspace browser ----------------------------------------------

async function refreshWorkspace() {
  if (!$("#workspace-drawer").classList.contains("open")) return;
  await browseWorkspace(currentWsPath);
}

async function browseWorkspace(path) {
  currentWsPath = path || "";
  let data;
  try { data = await api(`/api/sandbox/files?path=${encodeURIComponent(path || "")}`); }
  catch (err) {
    $("#ws-list").innerHTML = `<li class="error">${escapeHtml(err.message)}</li>`;
    return;
  }
  if (data.error) {
    $("#ws-list").innerHTML = `<li class="error">${escapeHtml(data.error)}</li>`;
    return;
  }
  $("#ws-path").textContent = "/" + (data.path || "");
  const ul = $("#ws-list");
  ul.innerHTML = "";
  if (data.path && data.path !== "") {
    const up = document.createElement("li");
    up.className = "file dir";
    up.textContent = "📁 ..";
    const parent = data.path.split("/").slice(0, -1).join("/");
    up.addEventListener("click", () => browseWorkspace(parent));
    ul.appendChild(up);
  }
  for (const e of data.entries) {
    const li = document.createElement("li");
    li.className = "file" + (e.is_dir ? " dir" : "");
    const icon = workspaceIcon(e);
    const sizeStr = e.is_dir ? "" : formatSize(e.size);
    li.innerHTML = `<span class="ic">${icon}</span><span>${escapeHtml(e.name)}</span><small>${sizeStr}</small>`;
    const fullPath = data.path ? `${data.path}/${e.name}` : e.name;
    if (e.is_dir) {
      li.addEventListener("click", () => browseWorkspace(fullPath));
    } else {
      li.addEventListener("click", () => openFileModal(fullPath));
    }
    ul.appendChild(li);
  }
}

$("#btn-ws-refresh").addEventListener("click", () => refreshWorkspace());

$("#btn-ws-mkdir").addEventListener("click", async () => {
  const name = prompt("New directory name (relative to current):");
  if (!name) return;
  const full = currentWsPath ? `${currentWsPath}/${name}` : name;
  try {
    await api("/api/sandbox/mkdir", {
      method: "POST",
      body: JSON.stringify({ path: full }),
    });
    await refreshWorkspace();
  } catch (err) { alert(`Could not create: ${err.message}`); }
});

$("#btn-ws-newfile").addEventListener("click", async () => {
  const name = prompt("New file name (relative to current):");
  if (!name) return;
  const full = currentWsPath ? `${currentWsPath}/${name}` : name;
  try {
    await api("/api/sandbox/file", {
      method: "POST",
      body: JSON.stringify({ path: full, content: "" }),
    });
    await refreshWorkspace();
    openFileModal(full);
  } catch (err) { alert(`Could not create: ${err.message}`); }
});

// ---------- file preview modal --------------------------------------------

const modalBackdrop = $("#modal-backdrop");
const modalPath = $("#file-modal-path");
const modalView = $("#file-modal-view code");
const modalEdit = $("#file-modal-edit");
const btnEdit = $("#btn-file-edit");
const btnSave = $("#btn-file-save");
const btnDelete = $("#btn-file-delete");
const btnClose = $("#btn-file-close");

function setModalEditMode(on) {
  if (on) {
    modalEdit.value = modalView.textContent;
    modalEdit.classList.remove("hidden");
    modalView.parentElement.classList.add("hidden");
    btnEdit.classList.add("hidden");
    btnSave.classList.remove("hidden");
  } else {
    modalEdit.classList.add("hidden");
    modalView.parentElement.classList.remove("hidden");
    btnEdit.classList.remove("hidden");
    btnSave.classList.add("hidden");
  }
}

async function openFileModal(path) {
  currentFilePath = path;
  modalPath.textContent = "/" + path;
  modalView.textContent = "Loading…";
  setModalEditMode(false);
  modalBackdrop.classList.remove("hidden");
  let data;
  try { data = await api(`/api/sandbox/file?path=${encodeURIComponent(path)}`); }
  catch (err) { modalView.textContent = `Error: ${err.message}`; return; }
  if (data.error) { modalView.textContent = `Error: ${data.error}`; return; }
  if (data.binary) { modalView.textContent = "(binary file)"; return; }
  modalView.textContent = data.content || "";
  try {
    const ext = fileExtension(path);
    const lang = { py: "python", js: "javascript", ts: "typescript", md: "markdown", sh: "bash", yml: "yaml", yaml: "yaml" }[ext] || "";
    if (lang) modalView.innerHTML = hljs.highlight(data.content || "", { language: lang }).value;
    else modalView.innerHTML = hljs.highlightAuto(data.content || "").value;
  } catch (_) {}
}

btnEdit.addEventListener("click", () => setModalEditMode(true));
btnSave.addEventListener("click", async () => {
  if (!currentFilePath) return;
  try {
    await api("/api/sandbox/file", {
      method: "POST",
      body: JSON.stringify({ path: currentFilePath, content: modalEdit.value }),
    });
    await openFileModal(currentFilePath);
    refreshWorkspace();
  } catch (err) { alert(`Save failed: ${err.message}`); }
});
btnDelete.addEventListener("click", async () => {
  if (!currentFilePath) return;
  if (!confirm(`Delete /${currentFilePath}?`)) return;
  try {
    await fetch("/api/sandbox/file", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentFilePath }),
    });
    modalBackdrop.classList.add("hidden");
    refreshWorkspace();
  } catch (err) { alert(`Delete failed: ${err.message}`); }
});
btnClose.addEventListener("click", () => modalBackdrop.classList.add("hidden"));
modalBackdrop.addEventListener("click", (e) => { if (e.target === modalBackdrop) modalBackdrop.classList.add("hidden"); });

// ---------- init -----------------------------------------------------------

setupMarkdown();
loadSession().catch((err) => {
  document.body.innerHTML = `<pre style="padding:24px;color:#ff6b6b">Failed to load session: ${escapeHtml(err.message)}</pre>`;
});
