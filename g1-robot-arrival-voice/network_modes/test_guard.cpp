#include "guard.hpp"
#include <cassert>
#include <iostream>
#include <limits>
Guard healthy(double now) {
  Guard g; g.low_at = g.lidar_at = g.fsm_at = now; g.fsm_code = 0; g.fsm = 501; return g;
}
int main() {
  int checks = 0;
  auto check = [&](bool ok) { ++checks; assert(ok); };
  auto g = healthy(10);
  check(!g.command(10,.1,0,0,0,0,0)); check(!g.ready(10));
  check(g.arm(10)); check(!g.ready(10));
  check(g.command(10.01,.1,0,.1,0,0,0)); check(g.ready(10.02));
  check(!g.ready(10.32)); check(g.fault && !g.armed);
  g.low_at = g.lidar_at = g.fsm_at = 10.33;
  check(!g.command(10.33,.1,0,0,0,0,0)); check(!g.ready(10.33));
  check(g.arm(10.34)); check(!g.ready(10.34)); // no cached replay after rearm
  check(g.command(10.35,0,0,0,0,0,0)); check(g.ready(10.36));
  for (int source=0; source<3; ++source) {
    g = healthy(20); check(g.arm(20)); check(g.command(20.1,.1,0,0,0,0,0));
    if (source==0) g.low_at=19; if (source==1) g.lidar_at=19; if (source==2) g.fsm_at=17;
    check(!g.ready(20.2)); check(!g.armed && g.fault);
  }
  for (int fsm : {802,500,-1}) { g=healthy(30); g.fsm=fsm; check(!g.arm(30)); }
  for (double bad : {.301, std::numeric_limits<double>::quiet_NaN(), std::numeric_limits<double>::infinity()}) {
    g=healthy(40); check(g.arm(40)); check(!g.command(40.1,bad,0,0,0,0,0)); check(g.fault);
  }
  g=healthy(50); check(g.arm(50)); check(!g.command(50.1,0,0,0,1,0,0));
  g=healthy(50); check(g.arm(50)); check(!g.command(50.1,0,0,0,0,1,0));
  g=healthy(60); g.fsm_code=3104; check(!g.arm(60));
  g=healthy(70); check(!g.healthy(69));
  std::cout << "{\"guard_assertions\":" << checks << ",\"motion_calls\":0}" << std::endl;
}
