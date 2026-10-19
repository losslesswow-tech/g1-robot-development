// Validate the whole proposed edit, without changing any raw target values.
export function allowsTrialEdit(plan, baseline, proposed) {
  const changed=proposed.map((v,i)=>v!==baseline[i]?i:-1).filter(i=>i>=0);
  return changed.length<=1 || ([4,5,6].includes(plan.schema) && changed.every(i=>plan.multi_channels.includes(i)));
}
