export function interpolate(points, value, from, to) {
  if (!Number.isFinite(value)) throw Error('位置必须是有限数值');
  const sorted = [...points].sort((a,b) => a[from]-b[from]);
  if (value < sorted[0][from] || value > sorted.at(-1)[from]) throw Error('位置超出当前标定范围');
  for (let i=1;i<sorted.length;i++) {
    const a=sorted[i-1], b=sorted[i];
    if (value<=b[from]) return a[to]+(value-a[from])/(b[from]-a[from])*(b[to]-a[to]);
  }
  throw Error('映射点不足');
}
export const degreesToRaw = (channel, degrees) => Math.round(interpolate(channel.points, degrees, 'degrees', 'raw'));
export const rawToDegrees = (channel, raw) => interpolate(channel.points, raw, 'raw', 'degrees');
export function validatePose(data, mappingHash) {
  if (data.schema!==1 || !['L','R'].includes(data.side) || !Array.isArray(data.raw) || data.raw.length!==6 || data.raw.some(v=>!Number.isInteger(v)||v<0||v>1000)) throw Error('姿态文件格式不符');
  if (data.mapping_sha256!==mappingHash) throw Error('姿态使用的映射文件与当前配置不同，请核对后重新导出');
  return {side:data.side,raw:[...data.raw]};
}
