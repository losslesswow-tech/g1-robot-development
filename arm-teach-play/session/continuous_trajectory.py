"""Validated, time-indexed arm samples with uniform velocity retiming."""
import bisect
import json
import math
import statistics


class ContinuousTrajectory:
    def __init__(self, recording, limits, peak_deg_s=20.):
        path = recording / 'samples.jsonl'
        if path.stat().st_size > 120_000_000:
            raise ValueError('连续记录过大，请拆分录制')
        times, poses = [], []
        self.total_rows = 0
        with path.open(encoding='utf-8') as source:
            for line in source:
                if not line.strip():continue
                self.total_rows += 1
                if self.total_rows > 50000:raise ValueError('连续记录超过采样数量上限')
                row = json.loads(line)
                if not isinstance(row, dict):raise ValueError('连续记录格式错误')
                if row.get('phase') != 'teaching':continue
                timestamp, upper = row.get('t_s'), row.get('q_upper')
                if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                    raise ValueError('连续记录时间戳无效')
                if not isinstance(upper, list) or len(upper) != 17:
                    raise ValueError('连续记录上肢关节数量错误')
                if not all(isinstance(q, (int, float)) and not isinstance(q, bool) and math.isfinite(q) for q in upper):
                    raise ValueError('连续记录含无效关节角度')
                pose = upper[3:]
                if any(not lo <= q <= hi for q, (lo, hi) in zip(pose, limits)):
                    raise ValueError('连续记录超出 G1 手臂关节限位')
                if times and timestamp < times[-1]:raise ValueError('连续记录时间戳倒退')
                times.append(float(timestamp));poses.append(pose)
        if len(times) < 2 or times[-1] <= times[0]:
            raise ValueError('连续记录至少需要两帧有效示教数据和正时长')
        self.source_start_s = times[0]
        self.recorded_duration_s = times[-1] - times[0]
        self.duplicate_timestamps = sum(a == b for a, b in zip(times, times[1:]))
        self.timebase_repaired = self.duplicate_timestamps > 0
        if self.timebase_repaired:
            metadata = json.loads((recording / 'metadata.json').read_text(encoding='utf-8'))
            if not isinstance(metadata,dict):raise ValueError('连续记录元数据无效')
            hz = metadata.get('sampling_target_hz', 50)
            measured_hz = (len(times)-1) / self.recorded_duration_s
            if hz != 50 or not .9*hz <= measured_hz <= 1.1*hz:
                raise ValueError('重复时间戳无法按 50Hz 采样顺序可靠重建，请重新录制')
            times = [i*self.recorded_duration_s/(len(times)-1) for i in range(len(times))]
        else:
            times = [t-times[0] for t in times]
        source_peak = max(abs(a-b)/(t1-t0) for t0,t1,p0,p1 in zip(times,times[1:],poses,poses[1:]) for a,b in zip(p0,p1))
        self.source_peak_deg_s = math.degrees(source_peak)
        self.time_scale = max(1., source_peak/math.radians(peak_deg_s)*1.001)
        self.times = [t*self.time_scale for t in times]
        self.poses = poses
        self.duration_s = self.times[-1]
        if not math.isfinite(self.duration_s) or not math.isfinite(self.time_scale):
            raise ValueError('连续记录时长或速度无效')
        self.sample_count = len(poses)
        self.median_interval_s = statistics.median(b-a for a,b in zip(self.times,self.times[1:]))

    def at(self, elapsed):
        elapsed = min(self.duration_s, max(0., elapsed))
        index = min(self.sample_count-2, max(0, bisect.bisect_right(self.times, elapsed)-1))
        t0, t1 = self.times[index:index+2]
        fraction = min(1., max(0., (elapsed-t0)/(t1-t0)))
        return [a+(b-a)*fraction for a,b in zip(self.poses[index],self.poses[index+1])], index+1

    def summary(self):
        return {'sample_count':self.sample_count,'recorded_duration_s':self.recorded_duration_s,
                'playback_duration_s':self.duration_s,'time_scale':self.time_scale,
                'source_peak_deg_s':self.source_peak_deg_s,'timebase_repaired':self.timebase_repaired,
                'duplicate_timestamps':self.duplicate_timestamps,'source_start_s':self.source_start_s}
