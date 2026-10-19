#pragma once
#include <array>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string>
namespace arm_debug {
constexpr double pi=3.14159265358979323846;
constexpr double max_step=20*pi/180, session_window=50*pi/180, rate=5*pi/180;
constexpr double left_hold_window=3*pi/180;
constexpr double heartbeat_seconds=3, session_seconds=30;
constexpr double settle_budget_seconds=2;
// Local observation reference, not a calibrated limit or firmware motor error.
// Standing is not required throughout this arm_sdk debugging session.
constexpr double leg_speed_reference=.3;
inline std::string leg_state_error(double q,double dq,uint32_t motorstate){
 if(!std::isfinite(q)||!std::isfinite(dq))return "leg_data_invalid";
 if(motorstate!=0)return "leg_motor_state";
 return {};
}
inline bool leg_speed_warning(double dq){return std::isfinite(dq)&&std::abs(dq)>leg_speed_reference;}
// Fixed audited G1 URDF, SDK right-arm indices 22..28. These are model limits,
// not a claim that full travel has been calibrated on this physical robot.
constexpr std::array<double,7> lower={-3.0892,-2.2515,-2.618,-1.0472,-1.972222054,-1.614429558,-1.614429558};
constexpr std::array<double,7> upper={2.6704,1.5882,2.618,2.0944,1.972222054,1.614429558,1.614429558};
inline double blend(double t){t=std::clamp(t,0.,1.);return t*t*t*(10+t*(-15+6*t));}
inline double motion_seconds(double delta){return std::max(1.,1.875*std::abs(delta)/rate);}
inline std::string target_error(int index,double value,const std::array<float,14>& commanded,const std::array<float,14>& seed){
 if(index<22||index>28)return "right_arm_only";
 if(!std::isfinite(value))return "nonfinite_target";
 const int j=index-22,arm=index-15;
 if(value<lower[j]||value>upper[j])return "model_joint_limit";
 if(std::abs(value-commanded[arm])>max_step+1e-7)return "step_exceeds_20_degrees";
 if(std::abs(value-seed[arm])>session_window+1e-7)return "session_exceeds_50_degrees";
 return {};
}
}
