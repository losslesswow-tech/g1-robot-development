"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="voice-token"]').content;
let points = [], selected = null, media = null, live = false, busy = false;
let automaticEnabled = false;
function renderAutomatic(state) {
  const auto = state.automatic || {};
  automaticEnabled = !!auto.enabled;
  const connected = !!auto.connected;
  const label = !connected ? "监听器未连接" : auto.state === "unavailable" ? "导航连接不可用" : auto.state === "task_managed" ? "由导航任务播报步骤接管" : auto.state === "armed" ? "已启用" : "已暂停（只读记录）";
  $("automatic-status").textContent = "自动到达播报：" + label;
  $("automatic-toggle").textContent = automaticEnabled ? "暂停自动播报" : "启用自动播报";
  $("automatic-toggle").disabled = !automaticEnabled && (!connected || auto.state === "unavailable");
  const result = {submitted: "上次播报请求已提交", failed_or_unknown: "上次播报失败或结果不确定，不会自动重试", task_managed: "本次到达由导航任务语音步骤处理", read_only: "已记录到达，自动播报未启用", unconfigured: "到达点未配置播报内容", busy: "音频忙，本次已跳过"}[auto.last_result] || "";
  $("automatic-detail").textContent = (auto.map_name ? "当前地图：" + auto.map_name + "。" : "") + (auto.point_name ? "监听目标：" + auto.point_name + "。" : "") + (result ? result + "。" : "") + "启用后从下一次导航开始监听。旧协议没有任务 ID，跨任务迟到结果仍可能误关联。暂停只阻止后续触发。";
}
$("automatic-toggle").addEventListener("click", async () => {
  $("automatic-toggle").disabled = true;
  try { await api("/api/automatic/enable", {enabled: !automaticEnabled}); await refreshAutomatic(); }
  catch (error) { feedback(error.message, true); await refreshAutomatic(); }
});
async function refreshAutomatic() {
  try { const response = await fetch("/api/state"); if (!response.ok) throw new Error(); renderAutomatic(await response.json()); }
  catch (error) { renderAutomatic({}); }
}
setInterval(refreshAutomatic, 2500);
const mode = () => document.querySelector('input[name="mode"]:checked').value;
function feedback(message, error = false) { const box = $("feedback"); box.hidden = false; box.textContent = message; box.classList.toggle("error", error); }
function markDirty() { $("save-state").textContent = "尚未保存"; }
function setBusy(value) { busy = value; document.querySelectorAll("#editor-form input, #editor-form textarea, #editor-form select, #editor-form button").forEach(input => input.disabled = value); $("new-point").disabled = value; }
async function api(url, data, raw = false) {
  const headers = {"X-Voice-Token": token, "Content-Type": raw ? "application/octet-stream" : "application/json"};
  const response = await fetch(url, {method: "POST", headers, body: raw ? data : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失败，请重试。");
  return result;
}
function renderPoints() {
  const list = $("point-list"); list.replaceChildren(); $("point-count").textContent = points.length;
  const query = $("point-search").value.toLowerCase();
  points.forEach(point => {
    if (!(point.map_name + " " + point.point_name).toLowerCase().includes(query)) return;
    const button = document.createElement("button"); button.type = "button";
    button.className = "point-card"; button.classList.toggle("selected", selected === point.map_name + "\n" + point.point_name);
    const number = document.createElement("span"); number.className = "point-number"; number.textContent = point.mode === "wav" ? "♫" : "Aa";
    const text = document.createElement("span"), title = document.createElement("strong"), detail = document.createElement("small");
    title.textContent = point.point_name; detail.textContent = "地图 " + point.map_name + " · " + (point.mode === "wav" ? "预录音频" : "文字转语音");
    text.append(title, detail); const arrow = document.createElement("span"); arrow.className = "arrow"; arrow.textContent = "›";
    button.append(number, text, arrow); button.addEventListener("click", () => { if (!busy) loadPoint(point); }); list.append(button);
  });
  if (!list.children.length) { const empty = document.createElement("p"); empty.className = "empty-state"; empty.textContent = "没有匹配的导航点"; list.append(empty); }
}
function changeMode() { $("text-panel").hidden = mode() !== "tts"; $("audio-panel").hidden = mode() !== "wav"; }
function showMedia() {
  $("audio-summary").hidden = !media;
  if (!media) { $("audio-preview").removeAttribute("src"); return; }
  $("audio-name").textContent = media.display_name || "已保存的预录音频";
  $("audio-detail").textContent = media.duration ? media.duration + " 秒 · 16 kHz · 单声道 PCM" : "16 kHz · 单声道 · 16 位 PCM";
  $("audio-preview").src = "/media/" + media.file.split("/").pop();
}
function loadPoint(point) {
  selected = point.map_name + "\n" + point.point_name; $("map-name").value = point.map_name; $("point-name").value = point.point_name;
  document.querySelector('input[name="mode"][value="' + (point.mode || "tts") + '"]').checked = true;
  $("speech-text").value = point.text || ""; $("speaker-id").value = String(point.speaker_id || 0);
  media = point.mode === "wav" ? {file: point.file, display_name: point.display_name} : null;
  $("editor-title").textContent = "编辑播报内容"; $("save-state").textContent = "已保存"; $("feedback").hidden = true;
  $("audio-file").value = ""; $("char-count").textContent = $("speech-text").value.length + " / 1000";
  changeMode(); showMedia(); renderPoints();
}
function draft() {
  const point = {map_name: $("map-name").value.trim(), point_name: $("point-name").value.trim(), mode: mode()};
  if (!point.map_name || !point.point_name) throw new Error("请先填写地图名称和导航点名称。");
  if (point.mode === "tts") {
    point.text = $("speech-text").value.trim(); point.speaker_id = Number($("speaker-id").value);
    if (!point.text) throw new Error("请输入需要播报的文字。");
  } else { if (!media) throw new Error("请先导入 WAV 音频。"); point.file = media.file; point.display_name = media.display_name; }
  return point;
}
async function importAudio(file) {
  if (!file || busy) return;
  if (!file.name.toLowerCase().endsWith(".wav")) return feedback("请选择 WAV 文件；其他格式请先转换。", true);
  if (file.size > 10 * 1024 * 1024) return feedback("文件超过 10 MB，请选择最长 5 分钟的 PCM WAV。", true);
  setBusy(true); feedback("正在导入音频……");
  try { const result = await api("/api/upload", await file.arrayBuffer(), true); media = {...result, display_name: file.name}; showMedia(); markDirty(); feedback("音频导入成功，保存后将用于该导航点。"); }
  catch (error) { feedback(error.message, true); }
  finally { setBusy(false); $("audio-file").value = ""; }
}
document.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener("change", () => { changeMode(); markDirty(); }));
["map-name", "point-name", "speaker-id", "speech-text"].forEach(id => $(id).addEventListener("input", () => { markDirty(); $("char-count").textContent = $("speech-text").value.length + " / 1000"; }));
$("point-search").addEventListener("input", renderPoints);
$("audio-file").addEventListener("change", event => importAudio(event.target.files[0]));
$("dropzone").addEventListener("dragover", event => { event.preventDefault(); $("dropzone").classList.add("dragging"); });
$("dropzone").addEventListener("dragleave", () => $("dropzone").classList.remove("dragging"));
$("dropzone").addEventListener("drop", event => { event.preventDefault(); $("dropzone").classList.remove("dragging"); importAudio(event.dataTransfer.files[0]); });
$("new-point").addEventListener("click", () => { if (busy) return; const map = $("map-name").value || "1009"; loadPoint({map_name: map, point_name: "", mode: "tts"}); selected = null; $("editor-title").textContent = "添加播报内容"; markDirty(); renderPoints(); $("point-name").focus(); });
$("editor-form").addEventListener("submit", async event => {
  event.preventDefault(); if (busy) return;
  try { const point = draft(); setBusy(true); const result = await api("/api/save", point); const response = await fetch("/api/state"); if (!response.ok) throw new Error("配置已保存，但列表刷新失败，请刷新页面。"); const state = await response.json(); points = state.config.points; loadPoint(result.point); feedback("已保存：地图 " + point.map_name + " / 导航点 " + point.point_name + "。"); }
  catch (error) { feedback(error.message, true); }
  finally { setBusy(false); }
});
$("test-button").addEventListener("click", async () => {
  if (busy) return;
  try {
    const point = draft(); setBusy(true); feedback(live ? "正在提交机器人播报……" : "正在检查播报内容……");
    const bytes = new Uint8Array(16); crypto.getRandomValues(bytes); const request_id = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
    const result = await api("/api/test", {point, request_id});
    if (!result.submitted) throw new Error(result.error || "提交结果不确定，请先确认现场声音，再决定是否再次测试。");
    feedback(result.simulation ? "模拟通过：" + (point.mode === "tts" ? "将播报“" + point.text + "”" : "将播放“" + point.display_name + "”") : "播报请求已提交，请现场确认声音。接口返回成功不代表播放完成。");
  } catch (error) { feedback(error.message, true); }
  finally { setBusy(false); }
});
(async () => {
  try { const response = await fetch("/api/state"); if (!response.ok) throw new Error("无法读取配置。"); const state = await response.json(); points = state.config.points; live = state.live; $("backend-badge").textContent = live ? "机器人音频已启用" : "本地模拟模式"; $("test-button").textContent = live ? "提交机器人播报" : "模拟播报"; $("test-note").textContent = live ? "点击后将使用 G1 自带音频设备播放当前内容。" : "模拟播报仅检查当前内容，不会让机器人发声。"; if (points.length) loadPoint(points[0]); else $("new-point").click(); }
  catch (error) { $("backend-badge").textContent = "读取失败"; feedback(error.message, true); setBusy(true); }
  await refreshAutomatic();
})();
