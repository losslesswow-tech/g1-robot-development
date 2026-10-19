#pragma once
#include <cmath>
#include <string>

// Seconds use a monotonic clock; command coordinates are robot body x/y/yaw.
struct Guard {
  bool armed = false, moving = false, fault = false;
  double armed_at = -1, cmd_at = -1, low_at = -1, lidar_at = -1, fsm_at = -1;
  double vx = 0, vy = 0, yaw = 0;
  int fsm = -1, fsm_code = -1;
  std::string reason = "disarmed";
  bool fresh(double now, double at, double age) const { return at >= 0 && now >= at && now - at <= age; }
  bool healthy(double now) const {
    return fresh(now, low_at, .5) && fresh(now, lidar_at, .5) && fresh(now, fsm_at, 2.5)
      && fsm_code == 0 && fsm == 501;
  }
  void disarm(const std::string& why, bool latch = false) { armed = false; fault = latch; cmd_at = -1; reason = why; }
  bool arm(double now) {
    if (!healthy(now)) { reason = "requires fresh lowstate/lidar and FSM 501"; return false; }
    armed = true; fault = false; armed_at = now; cmd_at = -1; reason = "armed; waiting for a new command";
    return true;
  }
  bool command(double now, double x, double y, double w, double z, double ax, double ay) {
    if (!armed) return false;
    if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(w) || !std::isfinite(z)
        || !std::isfinite(ax) || !std::isfinite(ay) || z != 0 || ax != 0 || ay != 0
        || std::abs(x) > .30 || std::abs(y) > .20 || std::abs(w) > .30) {
      disarm("invalid command or conservative session speed limit exceeded", true); return false;
    }
    vx = x; vy = y; yaw = w; cmd_at = now; return true;
  }
  bool ready(double now) {
    if (!armed) return false;
    if (!healthy(now)) { disarm("telemetry or FSM unavailable", true); return false; }
    if (cmd_at < 0) return false;
    if (!fresh(now, cmd_at, .30) || cmd_at < armed_at) {
      disarm("command stream expired", true); return false;
    }
    reason = "armed"; return true;
  }
};
