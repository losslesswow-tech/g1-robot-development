#pragma once
#include <string>
namespace arm_debug {
// Return codes and reported values are different facts. A failed getter does
// not report a new robot mode. Keep each getter's last successful timestamp.
struct RpcWatch {
 int rc=-1,value=-1,failures=0;double last_success=-1,last_attempt=-1;
 void update(int code,int reported,double now){
  rc=code;last_attempt=now;
  if(code==0){value=reported;last_success=now;failures=0;}else ++failures;
 }
 double age(double now)const{return last_success<0?1e9:now-last_success;}
 bool fresh(double now)const{return age(now)<=1.5;}
};
inline std::string mode_error(const RpcWatch& id,const RpcWatch& mode,double now,bool strict){
 if(!id.fresh(now))return "fsm_query_stale";
 if(!mode.fresh(now))return "fsm_mode_query_stale";
 if(id.value!=501)return "fsm_changed";
 // G1 GetFsmMode: 0=standing/static, 1=moving/dynamic. It is not the
 // locomotion controller ID. arm_sdk overlays the built-in controller in
 // either state. User scope does not require stationary feet during tuning.
 if(mode.value!=0&&mode.value!=1)return "fsm_mode_unknown";
 if(strict&&mode.value!=0)return "not_standing_at_acquire";
 if(strict&&(id.rc||mode.rc))return "mode_query_pending";
 return {};
}
}
