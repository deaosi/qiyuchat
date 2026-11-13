"use strict";

const endpoint = "/api/panel/keys";
const element = (id) => document.getElementById(id);
const statuses = { active: "生效中", scheduled: "未生效", expired: "已到期", revoked: "已撤销" };
const shanghaiOffset = 8 * 60 * 60;

const create = element("create-key");
const retry = element("retry");
const signIn = element("sign-in");
const placeholder = element("placeholder");
const tableWrap = element("table-wrap");
const list = element("key-list");
const editor = element("editor");
const editorForm = element("editor-form");
const labelInput = element("key-label");
const startsInput = element("starts-at");
const expiresInput = element("expires-at");
const save = element("save-key");
const revoke = element("revoke");
const confirmRevoke = element("confirm-revoke");
const issued = element("issued");
const secret = element("issued-key");

let records = [];
let editing = null;
let revoking = null;
let saving = false;

function setMessage(id, text = "") {
  const target = element(id);
  target.textContent = text;
  target.hidden = !text;
}

function shanghaiDateTime(seconds) {
  if (!Number.isFinite(seconds)) return "";
  return new Date((seconds + shanghaiOffset) * 1000).toISOString().split(".")[0].replace(/^\+0*/, "");
}

function unixTime(value) {
  if (!value) return null;
  const match = /^(\d{4,5})(-\d{2}-\d{2}T\d{2}:\d{2})(:\d{2})?$/.exec(value);
  if (!match) return NaN;
  const [, year, date, seconds = ":00"] = match;
  const isoYear = year.length === 4 ? year : `+${year.padStart(6, "0")}`;
  const stamp = Date.parse(`${isoYear}${date}${seconds}+08:00`) / 1000;
  return shanghaiDateTime(stamp) === `${year}${date}${seconds}` ? stamp : NaN;
}

async function request(body) {
  const response = await fetch(endpoint, {
    method: body ? "POST" : "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: body
      ? { Accept: "application/json", "Content-Type": "application/json", "X-Local-Request": "1" }
      : { Accept: "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "操作暂未完成，请稍后再试。");
    error.status = response.status;
    throw error;
  }
  return data;
}

function cell(row, text, className) {
  const td = row.insertCell();
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function action(text, handler, extra = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button action${extra}`;
  button.textContent = text;
  button.addEventListener("click", handler);
  return button;
}

function dateCell(row, value, empty) {
  const dateTime = shanghaiDateTime(value);
  const td = cell(row, empty, "date");
  if (!dateTime) return;
  const time = document.createElement("time");
  time.dateTime = `${dateTime}+08:00`;
  time.textContent = dateTime.slice(0, -3).replace("T", " ");
  time.title = `${dateTime.replace("T", " ")} 北京时间`;
  td.replaceChildren(time);
}

function render() {
  list.replaceChildren();
  retry.hidden = true;
  signIn.hidden = true;
  tableWrap.hidden = !records.length;
  placeholder.hidden = records.length > 0;
  element("placeholder-text").textContent = "还没有生成管理员密钥。";

  for (const record of records) {
    const row = list.insertRow();
    const name = cell(row, "");
    const title = document.createElement("span");
    title.className = "key-label";
    title.textContent = record.label;
    name.append(title);
    if (record.preview) {
      const preview = document.createElement("code");
      preview.className = "key-preview";
      preview.textContent = record.preview;
      name.append(preview);
    }
    const state = document.createElement("span");
    state.className = "state";
    state.dataset.status = record.status;
    state.textContent = Object.hasOwn(statuses, record.status) ? statuses[record.status] : "状态未知";
    if (record.status === "revoked" && record.revoked_at) {
      state.title = `撤销时间 ${shanghaiDateTime(record.revoked_at).replace("T", " ")} 北京时间`;
    }
    cell(row, "").append(state);
    dateCell(row, record.starts_at, "—");
    dateCell(row, record.expires_at, "长期有效");
    const actions = cell(row, "", "actions");
    if (record.status === "active" || record.status === "scheduled") {
      actions.append(action("编辑", () => openEditor(record)));
    }
    if (record.status !== "revoked") actions.append(action("撤销", () => openRevoke(record), " remove"));
  }
}

async function loadKeys() {
  create.disabled = true;
  retry.hidden = true;
  signIn.hidden = true;
  placeholder.hidden = false;
  tableWrap.hidden = true;
  element("placeholder-text").textContent = "正在加载密钥…";
  try {
    const data = await request();
    if (!Array.isArray(data.keys)) throw new Error("密钥列表暂时无法读取。");
    records = data.keys;
    render();
    create.disabled = false;
  } catch (error) {
    const authError = error.status === 401 || error.status === 403;
    element("placeholder-text").textContent = authError ? "请使用恒密钥登录后管理管理员密钥。" : error.message;
    retry.hidden = authError;
    signIn.hidden = !authError;
  }
}

function openEditor(record = null) {
  editing = record;
  editorForm.reset();
  labelInput.value = record?.label ?? "";
  startsInput.value = shanghaiDateTime(record?.starts_at);
  startsInput.required = Boolean(record);
  expiresInput.value = shanghaiDateTime(record?.expires_at);
  element("start-help").hidden = Boolean(record);
  element("editor-title").textContent = record ? "编辑密钥" : "生成密钥";
  save.textContent = record ? "保存" : "生成密钥";
  setMessage("editor-error");
  editor.showModal();
}

function openRevoke(record) {
  revoking = record;
  setMessage("revoke-detail", `撤销「${record.label}」后，使用该密钥的登录会话会立即失效。`);
  setMessage("revoke-error");
  revoke.showModal();
}

function updateRecord(record) {
  const index = records.findIndex((current) => current.id === record.id);
  if (index < 0) records.unshift(record);
  else records[index] = record;
  render();
}

function setBusy(dialog, value) {
  saving = value;
  for (const control of dialog.querySelectorAll("button, input")) control.disabled = value;
}

editorForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (saving) return;
  const label = labelInput.value.trim();
  const startsAt = unixTime(startsInput.value) ?? Math.floor(Date.now() / 1000);
  const expiresAt = unixTime(expiresInput.value);
  if (!label) return setMessage("editor-error", "请填写密钥名称。");
  if (!Number.isFinite(startsAt) || (expiresAt !== null && !Number.isFinite(expiresAt))) {
    return setMessage("editor-error", "请输入有效的北京时间。");
  }
  if (expiresAt !== null && expiresAt <= startsAt) {
    return setMessage("editor-error", "到期时间必须晚于生效时间。");
  }
  const body = { action: editing ? "update" : "create", label, starts_at: startsAt, expires_at: expiresAt };
  if (editing) body.id = editing.id;
  setMessage("editor-error");
  setBusy(editor, true);
  try {
    const data = await request(body);
    if (!data.record || (body.action === "create" && typeof data.key !== "string")) {
      throw new Error("密钥保存结果暂时无法确认，请刷新列表核对。");
    }
    updateRecord(data.record);
    editor.close();
    setMessage("notice", body.action === "create" ? "已生成密钥。" : "已保存密钥设置。");
    if (body.action === "create") {
      secret.value = data.key;
      setMessage("copy-status");
      issued.showModal();
    }
  } catch (error) {
    setMessage("editor-error", error.message);
  } finally {
    setBusy(editor, false);
  }
});

confirmRevoke.addEventListener("click", async () => {
  if (saving || !revoking) return;
  setMessage("revoke-error");
  setBusy(revoke, true);
  try {
    const data = await request({ action: "revoke", id: revoking.id });
    if (!data.record) throw new Error("暂时无法确认撤销结果，请刷新列表核对。");
    updateRecord(data.record);
    revoke.close();
    setMessage("notice", "已撤销密钥。");
  } catch (error) {
    setMessage("revoke-error", error.message);
  } finally {
    setBusy(revoke, false);
  }
});

element("copy-key").addEventListener("click", async () => {
  if (!secret.value) return;
  try {
    await navigator.clipboard.writeText(secret.value);
    setMessage("copy-status", "已复制到剪贴板。");
  } catch {
    secret.select();
    setMessage("copy-status", "请按 Ctrl+C 或 ⌘C 复制选中的密钥。");
  }
});

create.addEventListener("click", () => openEditor());
retry.addEventListener("click", loadKeys);
for (const dialog of [editor, revoke, issued]) {
  dialog.addEventListener("cancel", (event) => { if (saving) event.preventDefault(); });
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
}
issued.addEventListener("close", () => { secret.value = ""; });
window.addEventListener("pagehide", () => {
  secret.value = "";
  if (issued.open) issued.close();
});

void loadKeys();
