// Added to the observed task editor bundle by patch_frontend.py.
// Uses its existing React, Form, Select, Input and Button imports.
function G1TaskWaveInput({value, onChange}) {
  const [message, setMessage] = oe.useState(value ? "已导入 WAV" : "16 kHz / 单声道 / 16 位 PCM WAV");
  const [busy, setBusy] = oe.useState(false);
  async function upload(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    onChange(undefined);
    if (file.size > 10 * 1024 * 1024) {
      setMessage("音频超过 10 MB");
      return;
    }
    setBusy(true);
    try {
      const response = await fetch(new URL("/api/task/voice_upload", Vt()).toString(), {method: "POST", headers: {"Content-Type": "audio/wav"}, body: file});
      const result = await response.json();
      if (!response.ok || !result.file) throw new Error(result.error || "导入失败");
      onChange(result.file);
      setMessage(`${file.name}（${result.duration} 秒）`);
    } catch (error) {
      setMessage(error.message || "导入失败，请检查语音连接");
    } finally {
      setBusy(false);
    }
  }
  return i.jsxs("div", {children: [
    i.jsx("input", {type: "file", accept: ".wav,audio/wav", disabled: busy, onChange: upload, "aria-label": "导入任务播报音频", style: {maxWidth: 300}}),
    i.jsx("div", {role: "status", style: {fontSize: 12}, children: busy ? "正在导入…" : message})
  ]});
}

function G1TaskSpeech({path, fieldName}) {
  const form = ne.useFormInstance();
  const mode = form.getFieldValue([...path, fieldName, "target", "speechMode"]) || "tts";
  const [catalog, setCatalog] = oe.useState([]);
  const [error, setError] = oe.useState("");
  async function refresh() {
    try {
      const result = await qe("/api/task/voice_catalog");
      if (!Array.isArray(result.points)) throw new Error(result.error || "语音目录不可用");
      setCatalog(result.points);
      setError(result.live ? "" : "语音后端尚未连接实机");
    } catch (failure) {
      setCatalog([]);
      setError(failure.message || "语音目录不可用");
    }
  }
  oe.useEffect(() => { refresh(); }, []);
  const field = (name, children, options = {}) => i.jsx(ne.Item, {
    name: [fieldName, "target", name], style: {marginBottom: 4}, ...options, children
  });
  return i.jsxs("div", {style: {width: 330, flexShrink: 0}, children: [
    field("speechMode", i.jsx(it, {style: {width: "100%"}, options: [
      {value: "tts", label: "输入文字转语音"},
      {value: "wav", label: "导入预录音频"},
      {value: "saved", label: "选择已保存播报"}
    ]}), {initialValue: "tts"}),
    mode === "tts" ? field("speechText", i.jsx(Pe.TextArea, {rows: 2, maxLength: 1000, showCount: true, placeholder: "到点后需要播报的文字", "aria-label": "任务播报文字"}),
      {rules: [{required: true, whitespace: true, message: "请输入播报文字"}]}) :
    mode === "wav" ? field("speechFile", i.jsx(G1TaskWaveInput, {}),
      {rules: [{required: true, message: "请先导入有效 WAV 音频"}]}) : field("speechRef", i.jsx(it, {
      style: {width: "100%"}, placeholder: "选择文字或已导入音频", showSearch: true, optionFilterProp: "label",
      options: catalog.map(point => ({value: JSON.stringify([point.map_name, point.point_name]), label: `${point.map_name} / ${point.point_name} · ${point.mode === "wav" ? "音频" : "文字"}`}))
    }), {rules: [{required: true, message: "请选择已保存播报"}]}),
    mode === "saved" ? i.jsx(Ve, {size: "small", onClick: refresh, children: "刷新播报列表"}) : null,
    error ? i.jsx("div", {role: "status", style: {color: "#b42318", fontSize: 12}, children: error}) : null,
    i.jsx("div", {style: {fontSize: 12, color: "#667085"}, children: "接在导航步骤后执行；文字语音可用右侧等待秒数留出播报时间。"})
  ]});
}
