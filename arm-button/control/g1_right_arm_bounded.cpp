// G1 only: bounded right shoulder trial; rt/arm_sdk, never rt/lowcmd.
// Gains preserve repository g1_arm.cpp: kp=60, kd=1.5, tau=0.
#include <unitree/idl/hg/LowCmd_.hpp>
#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <unitree/robot/b2/robot_state/robot_state_client.hpp>
#include <ros/ros.h>
#include <geometry_msgs/Twist.h>
#include <array>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstring>
#include <deque>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <memory>
#include <stdexcept>
#include <thread>
#include <vector>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#include <net/if.h>

using Clock=std::chrono::steady_clock;
using State=unitree_hg::msg::dds_::LowState_;
using Cmd=unitree_hg::msg::dds_::LowCmd_;
constexpr double Pi=3.141592653589793, TargetDelta=-2*Pi/180, HardDelta=3*Pi/180;
std::atomic<bool> interrupted{false};
void signal_stop(int){interrupted=true;}
double blend(double t){t=std::clamp(t,0.,1.);return t*t*t*(10+t*(-15+6*t));}
bool stationary_leg(double q,double initial,double dq,uint32_t status){return std::isfinite(q)&&std::isfinite(dq)&&status==0&&std::abs(q-initial)<=Pi/180&&std::abs(dq)<=.3;}
int64_t monotonic_ms(){return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();}
template<class T> uint32_t crc(const T& value){
 uint32_t result=0xffffffff;const auto* bytes=reinterpret_cast<const unsigned char*>(&value);
 for(size_t i=0;i<sizeof(T)/4-1;++i){uint32_t word;std::memcpy(&word,bytes+4*i,4);
  for(uint32_t mask=0x80000000;mask;mask>>=1){bool high=result&0x80000000;result<<=1;if(high)result^=0x04c11db7;if(word&mask)result^=0x04c11db7;}}
 return result;
}
class Trial {
 std::mutex mutex_;State state_;Clock::time_point last_{};uint64_t samples_=0,rejected_=0;
 std::deque<uint32_t> own_crcs_;bool sending_=false,conflict_=false,nav_motion_=false;
 uint64_t arm_packets_=0,nav_packets_=0,base_packets_=0,sent_=0,release_sent_=0;
 int expected_machine_=0,initial_fsm_=-1;std::atomic<int> fsm_{-1};
 std::atomic<bool> monitor_stop_{false};std::thread monitor_;
 std::array<float,14> start_{},desired_{};float weight_=0;double minimum_pitch_=0,max_other_drift_=0;
 bool record_arm_speed_=false;
 bool horizontal_=false;
 bool first_stage_=false;
 std::array<float,12> initial_legs_{};
 std::atomic<int> zero_velocity_rc_{0};
 std::atomic<int64_t> last_zero_velocity_ms_{0};
 std::atomic<uint64_t> zero_velocity_calls_{0};
 std::array<float,14> envelope_low_{},envelope_high_{};
 std::array<double,14> peak_arm_speed_{};
 std::array<uint64_t,14> speed_exceedances_{};
 unitree::robot::ChannelSubscriberPtr<State> sub_;
 unitree::robot::ChannelSubscriberPtr<Cmd> cmd_sub_;
 unitree::robot::ChannelPublisherPtr<Cmd> pub_;
 std::unique_ptr<unitree::robot::g1::LocoClient> loco_;
 std::unique_ptr<unitree::robot::b2::RobotStateClient> robot_state_;
 std::atomic<bool> builtin_arm_stopped_{false};
 ros::Subscriber nav_sub_,base_sub_;
 void velocity(const geometry_msgs::Twist::ConstPtr& msg,bool base){
  const double values[]={msg->linear.x,msg->linear.y,msg->linear.z,msg->angular.x,msg->angular.y,msg->angular.z};
  std::lock_guard<std::mutex> lock(mutex_);if(base)++base_packets_;else ++nav_packets_;
  for(double v:values)if(!std::isfinite(v)||std::abs(v)>.01)nav_motion_=true;
 }
 State snapshot(){std::lock_guard<std::mutex> lock(mutex_);
  if(!samples_||std::chrono::duration<double>(Clock::now()-last_).count()>.1)throw std::runtime_error("stale_lowstate");
  return state_;}
 void guard(bool hard_envelope){
  if(interrupted)throw std::runtime_error("operator_interrupt");
  if(fsm_!=initial_fsm_)throw std::runtime_error("fsm_changed_or_query_failed");
  if(!builtin_arm_stopped_)throw std::runtime_error("builtin_arm_service_not_confirmed_stopped");
  State s=snapshot();
  if(first_stage_&&sending_&&(zero_velocity_rc_!=0||monotonic_ms()-last_zero_velocity_ms_>750))throw std::runtime_error("zero_velocity_hold_failed_or_stale");
  {std::lock_guard<std::mutex> lock(mutex_);if(conflict_)throw std::runtime_error("another_arm_sdk_writer");if(nav_motion_)throw std::runtime_error("movement_command_or_velocity_detected");}
  if(s.mode_machine()!=expected_machine_)throw std::runtime_error("machine_mode_changed");
  for(float v:s.imu_state().rpy())if(!std::isfinite(v))throw std::runtime_error("invalid_imu");
  if(std::abs(s.imu_state().rpy()[0])>.175||std::abs(s.imu_state().rpy()[1])>.175)throw std::runtime_error("body_tilt");
  for(int i=0;i<3;++i){const auto v=s.imu_state().gyroscope()[i];if(!std::isfinite(v)||std::abs(v)>.3){
   std::cout<<"{\"event\":\"body_rotation_fault\",\"axis\":"<<i<<",\"gyro_rad_s\":"<<v<<",\"rpy_rad\":["<<s.imu_state().rpy()[0]<<','<<s.imu_state().rpy()[1]<<','<<s.imu_state().rpy()[2]<<"]}\n"<<std::flush;
   throw std::runtime_error("body_rotation");}}
  for(int i=0;i<12;++i)if(!std::isfinite(s.motor_state()[i].dq())||std::abs(s.motor_state()[i].dq())>.3){
   std::cout<<"{\"event\":\"leg_motion_fault\",\"joint_index\":"<<i<<",\"dq\":"<<s.motor_state()[i].dq()<<"}\n"<<std::flush;
   throw std::runtime_error("leg_motion");}
  if(first_stage_)for(int i=0;i<12;++i){const auto& motor=s.motor_state()[i];if(!stationary_leg(motor.q(),initial_legs_[i],motor.dq(),motor.motorstate())){
   std::cout<<"{\"event\":\"stationary_leg_fault\",\"joint_index\":"<<i<<",\"q\":"<<motor.q()<<",\"initial_q\":"<<initial_legs_[i]<<",\"dq\":"<<motor.dq()<<",\"motorstate_raw\":"<<motor.motorstate()<<"}\n"<<std::flush;
   throw std::runtime_error("stationary_leg_interlock");}}
  for(int i=0;i<14;++i){const auto& m=s.motor_state()[i+15];
   if(!std::isfinite(m.q())||!std::isfinite(m.dq())||m.motorstate()!=0){
    std::cout<<"{\"event\":\"arm_guard_fault\",\"joint_index\":"<<i+15<<",\"q\":"<<m.q()<<",\"dq\":"<<m.dq()<<",\"motorstate_raw\":"<<m.motorstate()<<"}\n"<<std::flush;
    throw std::runtime_error("arm_state_or_velocity_fault");}
   peak_arm_speed_[i]=std::max(peak_arm_speed_[i],double(std::abs(m.dq())));
   if(std::abs(m.dq())>.3){
    if(!record_arm_speed_)throw std::runtime_error("custom_arm_speed_limit");
    if(!speed_exceedances_[i]++)std::cout<<"{\"event\":\"arm_speed_record_only\",\"joint_index\":"<<i+15<<",\"dq\":"<<m.dq()<<"}\n"<<std::flush;
   }
   const bool outside=horizontal_?(m.q()<envelope_low_[i]||m.q()>envelope_high_[i]):std::abs(m.q()-start_[i])>HardDelta;
   if(hard_envelope&&outside){
    std::cout<<"{\"event\":\"motion_envelope_fault\",\"joint_index\":"<<i+15<<",\"q\":"<<m.q()<<",\"start_q\":"<<start_[i]<<",\"delta_rad\":"<<m.q()-start_[i]<<",\"weight\":"<<weight_<<"}\n"<<std::flush;
    throw std::runtime_error("observed_motion_envelope_exceeded");}
   if(sending_&&std::abs(m.q()-desired_[i])>5*Pi/180)throw std::runtime_error("tracking_error");
   if(i!=7)max_other_drift_=std::max(max_other_drift_,double(std::abs(m.q()-start_[i])));
  }
  minimum_pitch_=std::min(minimum_pitch_,double(s.motor_state()[22].q()));
 }
 void send(const std::array<float,14>& target,float weight){
  Cmd msg{};msg.mode_pr(0);msg.mode_machine(expected_machine_);msg.motor_cmd()[29].q(weight);
  for(int i=0;i<14;++i){auto& m=msg.motor_cmd()[i+15];m.mode(1);m.q(target[i]);m.dq(0);m.kp(60);m.kd(1.5);m.tau(0);}
  msg.crc(crc(msg));
  {std::lock_guard<std::mutex> lock(mutex_);sending_=true;own_crcs_.push_back(msg.crc());if(own_crcs_.size()>256)own_crcs_.pop_front();}
  if(!pub_->Write(msg))throw std::runtime_error("dds_write_failed");
  ++sent_;if(weight==0)++release_sent_;weight_=weight;desired_=target;
 }
 void phase(const char* name,double duration,const std::array<float,14>& a,const std::array<float,14>& b,float wa,float wb){
  for(int i=0;i<14;++i){envelope_low_[i]=std::min(a[i],b[i])-HardDelta;envelope_high_[i]=std::max(a[i],b[i])+HardDelta;}
  auto begin=Clock::now();int frame=0;
  while(true){double t=std::chrono::duration<double>(Clock::now()-begin).count();double ratio=blend(t/duration);
   guard(true);std::array<float,14> target{};for(int i=0;i<14;++i)target[i]=a[i]+(b[i]-a[i])*ratio;
   send(target,wa+(wb-wa)*ratio);
   if(frame++%10==0){auto s=snapshot();std::cout<<"{\"event\":\"sample\",\"phase\":\""<<name<<"\",\"t\":"<<t<<",\"actual_right_pitch\":"<<s.motor_state()[22].q()<<",\"desired_right_pitch\":"<<target[7]<<",\"weight\":"<<weight_
    <<",\"imu_rpy_rad\":["<<s.imu_state().rpy()[0]<<','<<s.imu_state().rpy()[1]<<','<<s.imu_state().rpy()[2]<<"],\"imu_gyro_rad_s\":["<<s.imu_state().gyroscope()[0]<<','<<s.imu_state().gyroscope()[1]<<','<<s.imu_state().gyroscope()[2]<<"],\"leg_q_rad\":[";
    for(int i=0;i<12;++i){if(i)std::cout<<',';std::cout<<s.motor_state()[i].q();}std::cout<<"],\"leg_dq_rad_s\":[";
    for(int i=0;i<12;++i){if(i)std::cout<<',';std::cout<<s.motor_state()[i].dq();}std::cout<<"]}\n"<<std::flush;}
   if(t>=duration)break;
   std::this_thread::sleep_until(begin+std::chrono::milliseconds(frame*20));
  }
 }
 void hand_back(){
  if(!pub_||!sent_)return;
  const auto target=desired_;const float initial=weight_;
  double distance=0;for(int i=0;i<14;++i)distance=std::max(distance,double(std::abs(desired_[i]-start_[i])));
  // If interrupted away from the original pose, release slowly rather than
  // blending a raised arm into the normal hanging posture in one second.
  const int frames=horizontal_?int(std::ceil(std::max(1.,1.875*distance/(5*Pi/180))*50)):50;
  for(int i=0;i<=frames;++i){try{send(target,initial*(1-float(i)/frames));}catch(...){break;}std::this_thread::sleep_for(std::chrono::milliseconds(20));}
  for(int i=0;i<10;++i){try{send(target,0);}catch(...){break;}std::this_thread::sleep_for(std::chrono::milliseconds(20));}
 }
public:
 ~Trial(){monitor_stop_=true;if(monitor_.joinable())monitor_.join();}
 int run(bool execute,const std::string& network_interface,bool record_arm_speed,bool horizontal,bool first_stage){
  record_arm_speed_=record_arm_speed;
  horizontal_=horizontal;
  first_stage_=first_stage;
  const int lock_fd=open("/tmp/g1-right-arm-bounded.lock",O_CREAT|O_RDWR,0600);
  if(lock_fd<0||flock(lock_fd,LOCK_EX|LOCK_NB))throw std::runtime_error("another_local_trial_process");
  unitree::robot::ChannelFactory::Instance()->Init(0,network_interface);
  sub_.reset(new unitree::robot::ChannelSubscriber<State>("rt/lowstate"));
  sub_->InitChannel([this](const void* raw){const auto& s=*static_cast<const State*>(raw);std::lock_guard<std::mutex> lock(mutex_);if(crc(s)!=s.crc()){++rejected_;return;}state_=s;last_=Clock::now();++samples_;},1);
  cmd_sub_.reset(new unitree::robot::ChannelSubscriber<Cmd>("rt/arm_sdk"));
  cmd_sub_->InitChannel([this](const void* raw){const auto& c=*static_cast<const Cmd*>(raw);std::lock_guard<std::mutex> lock(mutex_);++arm_packets_;if(!sending_||std::find(own_crcs_.begin(),own_crcs_.end(),c.crc())==own_crcs_.end())conflict_=true;},16);
  ros::NodeHandle node;nav_sub_=node.subscribe<geometry_msgs::Twist>("/cmd_vel",10,[this](const geometry_msgs::Twist::ConstPtr& m){velocity(m,false);});
  base_sub_=node.subscribe<geometry_msgs::Twist>("/robot/dog_vel",10,[this](const geometry_msgs::Twist::ConstPtr& m){velocity(m,true);});
  std::this_thread::sleep_for(std::chrono::seconds(2));State initial=snapshot();
  expected_machine_=initial.mode_machine();for(int i=0;i<14;++i)start_[i]=initial.motor_state()[i+15].q();desired_=start_;minimum_pitch_=start_[7];
  for(int i=0;i<12;++i)initial_legs_[i]=initial.motor_state()[i].q();
  // SDK clients must be constructed after ChannelFactory initialization.
  loco_=std::make_unique<unitree::robot::g1::LocoClient>();
  loco_->Init();loco_->SetTimeout(1.0f);int mode=-1;const int id_ret=loco_->GetFsmId(initial_fsm_),mode_ret=loco_->GetFsmMode(mode);
  if(id_ret||mode_ret)throw std::runtime_error("preflight_fsm_query_failed");
  fsm_=initial_fsm_;
  uint64_t samples,arm_packets,nav_packets,base_packets;
  {std::lock_guard<std::mutex> lock(mutex_);samples=samples_;arm_packets=arm_packets_;nav_packets=nav_packets_;base_packets=base_packets_;}
  std::cout<<std::setprecision(9)<<"{\"event\":\"preflight\",\"fsm_id\":"<<initial_fsm_<<",\"fsm_mode\":"<<mode<<",\"mode_machine\":"<<expected_machine_<<",\"right_pitch_start\":"<<start_[7]<<",\"horizontal\":"<<(horizontal_?"true":"false")<<",\"first_stage_only\":"<<(first_stage_?"true":"false")<<",\"target_delta_rad\":"<<(horizontal_?(-85*Pi/180-start_[7])*(first_stage_?.15:1.):TargetDelta)<<",\"motion_envelope_margin_rad\":"<<HardDelta<<",\"samples\":"<<samples<<",\"arm_sdk_packets_before_control\":"<<arm_packets<<",\"nav_velocity_samples\":"<<nav_packets<<",\"base_velocity_samples\":"<<base_packets<<",\"motion_commands_sent\":0}\n"<<std::flush;
  robot_state_=std::make_unique<unitree::robot::b2::RobotStateClient>();
  robot_state_->SetTimeout(1.0f);robot_state_->Init();
  if(robot_state_->GetApiVersion()!=robot_state_->GetServerApiVersion())throw std::runtime_error("robot_state_api_version_mismatch");
  std::vector<unitree::robot::b2::ServiceState> services;
  if(robot_state_->ServiceList(services))throw std::runtime_error("arm_service_query_failed");
  for(const auto& service:services)if(service.name=="g1_arm_example"&&service.status==1)builtin_arm_stopped_=true;
  std::cout<<"{\"event\":\"builtin_arm_service_preflight\",\"confirmed_stopped\":"<<(builtin_arm_stopped_?"true":"false")<<"}\n"<<std::flush;
  if(!execute)return 0;
  if(initial_fsm_!=501||mode!=0)throw std::runtime_error("preflight_fsm_not_observed_501_mode0");
  guard(false);
  if(first_stage_){
   for(int i=0;i<3;++i){const int rc=loco_->StopMove();std::cout<<"{\"event\":\"zero_velocity_before_trial\",\"return_code\":"<<rc<<"}\n"<<std::flush;if(rc)throw std::runtime_error("zero_velocity_preflight_failed");last_zero_velocity_ms_=monotonic_ms();++zero_velocity_calls_;std::this_thread::sleep_for(std::chrono::milliseconds(100));}
   std::this_thread::sleep_for(std::chrono::milliseconds(500));guard(false);
  }
  // Monitor FSM/service state. The first-stage repeat also holds zero velocity;
  // it never changes the robot FSM or sends a nonzero base velocity.
  monitor_=std::thread([this]{while(!monitor_stop_){if(first_stage_){const int stop_rc=loco_->StopMove();++zero_velocity_calls_;zero_velocity_rc_=stop_rc;if(stop_rc==0)last_zero_velocity_ms_=monotonic_ms();}
   int id=-1;const int rc=loco_->GetFsmId(id);fsm_=rc?-1:id;
   std::vector<unitree::robot::b2::ServiceState> observed;bool stopped=false;
   if(!robot_state_->ServiceList(observed))for(const auto& service:observed)if(service.name=="g1_arm_example"&&service.status==1)stopped=true;
   builtin_arm_stopped_=stopped;std::this_thread::sleep_for(std::chrono::milliseconds(200));}});
  pub_.reset(new unitree::robot::ChannelPublisher<Cmd>("rt/arm_sdk"));pub_->InitChannel();
  bool success=false;std::string reason="none";double peak=0,return_error=0;
  try{
   auto target=start_;
   if(horizontal_){target[7]=-85*Pi/180;target[8]=-14.75*Pi/180;target[10]=81*Pi/180;}
   else target[7]+=TargetDelta;
   if(first_stage_)for(int i=0;i<14;++i)target[i]=start_[i]+.15*(target[i]-start_[i]);
   phase("acquire",2,start_,start_,0,1);
   if(horizontal_&&!first_stage_){
    auto previous=start_;const double fractions[]={.15,.4,.7,1.};int stage=0;
    for(double fraction:fractions){auto waypoint=start_;double travel=0;
     for(int i=0;i<14;++i){waypoint[i]=start_[i]+(target[i]-start_[i])*fraction;travel=std::max(travel,double(std::abs(waypoint[i]-previous[i])));}
     const auto name=std::string("forward_stage_")+std::to_string(++stage);
     phase(name.c_str(),std::max(3.,1.875*travel/(5*Pi/180)),previous,waypoint,1,1);previous=waypoint;
    }
   }else {double travel=0;for(int i=0;i<14;++i)travel=std::max(travel,double(std::abs(target[i]-start_[i])));phase("forward",first_stage_?std::max(3.,1.875*travel/(5*Pi/180)):3,start_,target,1,1);}
   phase("hold",first_stage_?1:(horizontal_?4:.4),target,target,1,1);
   if(horizontal_){const auto reached=snapshot();bool reached_ok=true;
    std::cout<<"{\"event\":\""<<(first_stage_?"first_stage_pose_observed":"horizontal_pose_observed")<<"\",\"right_q_rad\":[";
    for(int i=7;i<14;++i){if(i>7)std::cout<<',';const auto q=reached.motor_state()[i+15].q();std::cout<<q;if(std::abs(q-target[i])>3*Pi/180)reached_ok=false;}
    std::cout<<"],\"within_3deg_of_target\":"<<(reached_ok?"true":"false")<<"}\n"<<std::flush;
    if(!reached_ok)throw std::runtime_error("horizontal_target_tracking_not_verified");
   }
   peak=snapshot().motor_state()[22].q()-start_[7];
   double travel=0;for(int i=0;i<14;++i)travel=std::max(travel,double(std::abs(target[i]-start_[i])));
   phase("return",horizontal_?std::max(3.,1.875*travel/(5*Pi/180)):3,target,start_,1,1);
   phase("settle",.5,start_,start_,1,1);
   return_error=std::abs(snapshot().motor_state()[22].q()-start_[7]);
   success=std::abs(peak-(horizontal_?target[7]-start_[7]:TargetDelta))<(horizontal_?3:1)*Pi/180&&return_error<1.5*Pi/180;
   if(!success)reason="tracking_not_verified";
  }catch(const std::exception& error){reason=error.what();}
  hand_back();monitor_stop_=true;if(monitor_.joinable())monitor_.join();
  if(first_stage_){const int rc=loco_->StopMove();++zero_velocity_calls_;std::cout<<"{\"event\":\"zero_velocity_after_trial\",\"return_code\":"<<rc<<",\"zero_velocity_calls\":"<<zero_velocity_calls_.load()<<"}\n";}
  for(int i=0;i<14;++i)std::cout<<"{\"event\":\"joint_speed_summary\",\"joint_index\":"<<i+15<<",\"peak_abs_dq\":"<<peak_arm_speed_[i]<<",\"custom_limit_exceedances\":"<<speed_exceedances_[i]<<"}\n";
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  std::cout<<"{\"marker\":\""<<(first_stage_?"G1_RIGHT_ARM_FIRST_STAGE_COMPLETE":(horizontal_?"G1_RIGHT_ARM_HORIZONTAL_COMPLETE":"G1_RIGHT_SHOULDER_TRIAL_COMPLETE"))<<"\",\"success\":"<<(success?"true":"false")<<",\"reason\":\""<<reason<<"\",\"command_frames\":"<<sent_<<",\"zero_weight_frames\":"<<release_sent_<<",\"start_right_pitch\":"<<start_[7]<<",\"observed_peak_delta_rad\":"<<peak<<",\"observed_min_delta_rad\":"<<minimum_pitch_-start_[7]<<",\"return_error_rad\":"<<return_error<<",\"max_other_joint_drift_rad\":"<<max_other_drift_<<",\"fsm_last_observed\":"<<fsm_.load()<<",\"hand_back_commanded\":"<<(release_sent_>0?"true":"false")<<"}\n";
  flock(lock_fd,LOCK_UN);close(lock_fd);return success?0:3;
 }
};
int main(int argc,char** argv){
 if(argc==2&&!std::strcmp(argv[1],"--self-test")){if(blend(0)!=0||blend(1)!=1||std::abs(TargetDelta)>HardDelta)return 3;for(int i=0;i<1000;++i)if(blend(double(i)/1000)>blend(double(i+1)/1000))return 3;
  for(double travel:{.02,.3,1.8}){const double duration=std::max(3.,1.875*travel/(5*Pi/180));for(int i=0;i<1000;++i)if(travel*(blend(double(i+1)/1000)-blend(double(i)/1000))/(duration/1000)>5*Pi/180+1e-8)return 3;}
  if(!stationary_leg(0,0,0,0)||stationary_leg(1.1*Pi/180,0,0,0)||stationary_leg(0,0,.31,0)||stationary_leg(0,0,0,1)||stationary_leg(NAN,0,0,0))return 3;
  std::cout<<"bounded_trial_horizontal_rate_and_stationary_interlock_math_ok\n";return 0;}
 if((argc!=3&&argc!=4)||(std::strcmp(argv[1],"--inspect")&&std::strcmp(argv[1],"--execute-approved-trial")&&std::strcmp(argv[1],"--execute-approved-horizontal")&&std::strcmp(argv[1],"--execute-approved-first-stage"))||(argc==4&&std::strcmp(argv[3],"--record-arm-speed"))){std::cerr<<"Use --self-test or --inspect NETWORK_INTERFACE [--record-arm-speed]\n";return 2;}
 const bool record_arm_speed=argc==4;
 const bool first_stage=!std::strcmp(argv[1],"--execute-approved-first-stage");
 const bool horizontal=first_stage||!std::strcmp(argv[1],"--execute-approved-horizontal");
 const bool execute=horizontal||!std::strcmp(argv[1],"--execute-approved-trial");
#ifndef G1_ENABLE_APPROVED_TRIAL
 if(execute){std::cerr<<"physical_trial_disabled_in_this_build\n";return 2;}
#endif
 const std::string network_interface=argv[2];
 if(network_interface.empty()||!if_nametoindex(network_interface.c_str())){std::cerr<<"network_interface_not_found\n";return 2;}
 std::signal(SIGINT,signal_stop);std::signal(SIGTERM,signal_stop);
 ros::init(argc,argv,"g1_right_arm_bounded",ros::init_options::AnonymousName|ros::init_options::NoSigintHandler);
 ros::AsyncSpinner spinner(1);spinner.start();
 try{Trial trial;return trial.run(execute,network_interface,record_arm_speed,horizontal,first_stage);}
 catch(const std::exception& error){std::cerr<<"trial_not_started: "<<error.what()<<"\n";return 1;}
}
