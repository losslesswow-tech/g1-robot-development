// G1 only. Starts with DDS readers and Get RPCs. rt/arm_sdk publisher is created
// only by an explicit ACQUIRE. No lowcmd publisher, base setters or service switch.
#include "safety.hpp"
#include "rpc_watch.hpp"
#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/idl/hg/LowCmd_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <unitree/robot/b2/robot_state/robot_state_client.hpp>
#include <chrono>
#include <csignal>
#include <cstring>
#include <deque>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <thread>
#include <atomic>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#include <net/if.h>
using Clock=std::chrono::steady_clock;
using State=unitree_hg::msg::dds_::LowState_;
using Cmd=unitree_hg::msg::dds_::LowCmd_;
using namespace arm_debug;
std::atomic<bool> interrupted{false};
void signal_stop(int){interrupted=true;}
double elapsed(Clock::time_point t){return std::chrono::duration<double>(Clock::now()-t).count();}
double monotonic(){return std::chrono::duration<double>(Clock::now().time_since_epoch()).count();}
template<class T> uint32_t crc(const T& value){
 uint32_t result=0xffffffff;const auto* bytes=reinterpret_cast<const unsigned char*>(&value);
 for(size_t i=0;i<sizeof(T)/4-1;++i){uint32_t word;std::memcpy(&word,bytes+4*i,4);
  for(uint32_t mask=0x80000000;mask;mask>>=1){bool high=result&0x80000000;result<<=1;if(high)result^=0x04c11db7;if(word&mask)result^=0x04c11db7;}}
 return result;
}
class Worker {
 std::mutex mutex_,output_;State state_{};bool seen_=false,closed_=false,finished_=false,started_=false;
 Clock::time_point received_{},tick_changed_{},rpc_checked_{},last_foreign_{},heartbeat_{},session_start_{},phase_start_{};
 int fsm_=-1,mode_=-1,expected_machine_=0,lock_fd_=-1,moving_joint_=7;
 RpcWatch id_query_,mode_query_,service_query_;
 bool stopped_=false,versions_match_=false,release_to_fault_=false;
 std::string phase_="readonly",fault_,fault_context_="null";
 std::array<float,14> seed_{},desired_{},from_{},goal_{};
 float weight_=0,release_weight_=0;double move_seconds_=1;
 uint64_t rejected_=0,sent_=0,foreign_=0;
 std::array<uint64_t,12> leg_speed_samples_{};
 std::array<double,12> leg_speed_peaks_{};
 std::deque<uint32_t> own_crcs_;
 std::atomic<bool> monitor_stop_{false};std::thread monitor_,control_;
 unitree::robot::ChannelPtr<State> state_sub_;
 unitree::robot::ChannelPtr<Cmd> command_sub_;
 unitree::robot::ChannelPublisherPtr<Cmd> publisher_;
 bool controlled()const{return phase_=="acquiring"||phase_=="holding"||phase_=="moving"||phase_=="settling";}
 void emit(const std::string& line){std::lock_guard<std::mutex> g(output_);std::cout<<line<<std::endl;}
 std::string guard(bool acquiring=false){
  if(!seen_||elapsed(received_)>.1||elapsed(tick_changed_)>.1)return "stale_lowstate";
  const auto rpc_error=mode_error(id_query_,mode_query_,monotonic(),acquiring);
  if(!rpc_error.empty())return rpc_error;
  if(!service_query_.fresh(monotonic()))return "service_query_stale";
  if(acquiring&&service_query_.rc)return "service_query_pending";
  if(!versions_match_||!stopped_)return "builtin_arm_service_not_stopped";
  if(elapsed(last_foreign_)<1)return "another_arm_sdk_writer";
  if(!acquiring&&state_.mode_machine()!=expected_machine_)return "machine_mode_changed";
  for(int i=0;i<3;++i){float r=state_.imu_state().rpy()[i],v=state_.imu_state().gyroscope()[i];
   if(!std::isfinite(r)||!std::isfinite(v))return "invalid_imu";
   if(std::abs(v)>.3)return "body_rotation";
   if(i<2&&std::abs(r)>.175)return "body_tilt";
  }
  for(int i=0;i<12;++i){const auto& m=state_.motor_state()[i];const auto error=leg_state_error(m.q(),m.dq(),m.motorstate());if(!error.empty())return error;}
  for(int i=0;i<14;++i){const auto& m=state_.motor_state()[i+15];
   if(!std::isfinite(m.q())||!std::isfinite(m.dq())||m.motorstate()!=0)return "arm_state_fault";
   if(!acquiring&&i<7&&std::abs(m.q()-seed_[i])>left_hold_window+1e-6)return "left_arm_hold_envelope";
   if(!acquiring&&i>=7&&std::abs(m.q()-seed_[i])>session_window+1e-6)return "observed_session_envelope";
   if(!acquiring&&std::abs(m.q()-desired_[i])>5*pi/180)return "tracking_error";
  }
  return {};
 }
 void send(){
  Cmd msg{};msg.mode_pr(0);msg.mode_machine(expected_machine_);msg.motor_cmd()[29].q(weight_);
  for(int i=0;i<14;++i){auto& m=msg.motor_cmd()[i+15];m.mode(1);m.q(desired_[i]);m.dq(0);m.kp(60);m.kd(1.5);m.tau(0);}
  msg.crc(crc(msg));own_crcs_.push_back(msg.crc());if(own_crcs_.size()>256)own_crcs_.pop_front();
  if(!publisher_->Write(msg))throw std::runtime_error("dds_write_failed");
  ++sent_;
 }
 void release(bool fault,const std::string& reason){
  if(fault&&!reason.empty()&&fault_.empty()){
   fault_=reason;
   // Capture before the phase changes; later successful queries cannot erase
   // the failure-time return codes, values, weight, and sensor observations.
   fault_context_=state_json();
   emit("{\"type\":\"fault_event\",\"snapshot\":"+fault_context_+"}");
  }
  if(phase_=="releasing"){release_to_fault_=release_to_fault_||fault;return;}
  if(!controlled()){if(fault)phase_="fault";return;}
  release_to_fault_=fault;release_weight_=weight_;phase_="releasing";phase_start_=Clock::now();
 }
 std::string state_json(){
  std::ostringstream s;s<<std::setprecision(9)<<"{\"type\":\"state\",\"connected\":"<<(seen_?"true":"false")
   <<",\"phase\":"<<std::quoted(phase_)<<",\"fault\":"<<std::quoted(fault_)
   <<",\"fresh\":"<<(seen_&&elapsed(received_)<=.1&&elapsed(tick_changed_)<=.1?"true":"false")
   <<",\"age_ms\":"<<(seen_?elapsed(received_)*1000:-1)<<",\"fsm_id\":"<<fsm_<<",\"fsm_mode\":"<<mode_
   <<",\"fsm_query_rc\":"<<id_query_.rc<<",\"fsm_mode_query_rc\":"<<mode_query_.rc
   <<",\"service_query_rc\":"<<service_query_.rc
   <<",\"fsm_success_age_ms\":"<<id_query_.age(monotonic())*1000
   <<",\"fsm_mode_success_age_ms\":"<<mode_query_.age(monotonic())*1000
   <<",\"service_success_age_ms\":"<<service_query_.age(monotonic())*1000
   <<",\"fsm_query_failures\":"<<id_query_.failures<<",\"fsm_mode_query_failures\":"<<mode_query_.failures
   <<",\"query_pending\":"<<((id_query_.rc||mode_query_.rc||service_query_.rc)?"true":"false")
   <<",\"fault_context\":"<<fault_context_
   <<",\"builtin_arm_stopped\":"<<(stopped_?"true":"false")<<",\"api_versions_match\":"<<(versions_match_?"true":"false")
   <<",\"control_available\":"
#ifdef G1_ARM_DEBUG_ENABLE_CONTROL
   <<"true"
#else
   <<"false"
#endif
   <<",\"limits\":{\"step_deg\":"<<max_step*180/pi<<",\"session_deg\":"<<session_window*180/pi
   <<",\"left_hold_deg\":"<<left_hold_window*180/pi<<",\"preview_rate_deg_s\":"<<rate*180/pi<<'}'
   <<",\"weight\":"<<weight_<<",\"command_frames\":"<<sent_<<",\"crc_rejected\":"<<rejected_
   <<",\"phase_elapsed_s\":"<<elapsed(phase_start_)<<",\"mode_machine\":"<<int(state_.mode_machine())
   <<",\"remaining_s\":"<<(controlled()?std::max(0.,session_seconds-elapsed(session_start_)):0.)<<",\"joints\":[";
  for(int i=0;i<14;++i){if(i)s<<',';const auto& m=state_.motor_state()[15+i];
   s<<"{\"index\":"<<15+i<<",\"q_rad\":"<<m.q()<<",\"dq_rad_s\":"<<m.dq()<<",\"motorstate\":"<<m.motorstate()<<",\"target_rad\":"<<desired_[i]<<",\"seed_rad\":"<<seed_[i]<<'}';}
  s<<"],\"imu_rpy_rad\":[";for(int i=0;i<3;++i){if(i)s<<',';s<<state_.imu_state().rpy()[i];}
  s<<"],\"imu_gyro_rad_s\":[";for(int i=0;i<3;++i){if(i)s<<',';s<<state_.imu_state().gyroscope()[i];}
  s<<"],\"legs\":[";
  for(int i=0;i<12;++i){if(i)s<<',';const auto& m=state_.motor_state()[i];s<<"{\"index\":"<<i<<",\"q_rad\":"<<m.q()<<",\"dq_rad_s\":"<<m.dq()<<",\"motorstate\":"<<m.motorstate()<<'}';}
  s<<"],\"leg_speed_monitor\":{\"reference_rad_s\":"<<leg_speed_reference<<",\"abort_on_speed\":false,\"exceedance_samples\":[";
  for(int i=0;i<12;++i){if(i)s<<',';s<<leg_speed_samples_[i];}
  s<<"],\"peak_abs_dq_rad_s\":[";
  for(int i=0;i<12;++i){if(i)s<<',';s<<leg_speed_peaks_[i];}
  s<<"]}}";return s.str();
 }
 void loop(){
  auto deadline=Clock::now(),last_loop=Clock::now();int count=0;
  while(true){std::string report;
   {std::lock_guard<std::mutex> g(mutex_);
    if(closed_&&!controlled()&&phase_!="releasing")break;
    if(controlled()){
     std::string error=guard();
     if(interrupted)error="operator_interrupt";
     else if(closed_)error="input_closed";
     else if(elapsed(heartbeat_)>heartbeat_seconds)error="browser_heartbeat_lost";
     else if(elapsed(session_start_)>session_seconds)error="session_timeout";
     if(!error.empty())release(true,error);
    }
    const auto now=Clock::now();
    // During a brief getter failure, hold the last command and weight. Do not
    // advance a trajectory or accept SET until all queries have recovered.
    const bool pending=id_query_.rc||mode_query_.rc||service_query_.rc;
    if(controlled()&&pending)phase_start_+=now-last_loop;
    last_loop=now;
    if(phase_=="acquiring"&&!pending){
     weight_=float(blend(elapsed(phase_start_)/2));if(elapsed(phase_start_)>=2)phase_="holding";
    }else if(phase_=="moving"&&!pending){
     const double ratio=blend(elapsed(phase_start_)/move_seconds_);
     for(int i=0;i<14;++i)desired_[i]=from_[i]+float((goal_[i]-from_[i])*ratio);
     if(elapsed(phase_start_)>=move_seconds_){phase_="settling";phase_start_=Clock::now();}
    }else if(phase_=="settling"&&!pending){
     const auto& m=state_.motor_state()[moving_joint_+15];
     if(elapsed(phase_start_)>=.3&&std::abs(m.q()-goal_[moving_joint_])<=pi/180&&std::abs(m.dq())<=.15)phase_="holding";
     else if(elapsed(phase_start_)>1.5)release(true,"target_not_reached");
    }else if(phase_=="releasing"){
     weight_=release_weight_*float(1-blend(elapsed(phase_start_)/1.5));
    }
    if(controlled()||phase_=="releasing"){
     try{send();}catch(const std::exception& e){release(true,e.what());}
    }
    if(phase_=="releasing"&&elapsed(phase_start_)>=1.7){
     weight_=0;phase_=release_to_fault_?"fault":"readonly";publisher_.reset();
     if(lock_fd_>=0){flock(lock_fd_,LOCK_UN);close(lock_fd_);lock_fd_=-1;}
    }
    if(count++%10==0)report=state_json();
   }
   if(!report.empty())emit(report);
   deadline+=std::chrono::milliseconds(20);std::this_thread::sleep_until(deadline);
   if(Clock::now()-deadline>std::chrono::milliseconds(100))deadline=Clock::now();
  }
 }
public:
 ~Worker(){try{finish();}catch(...){}}
 void start(const std::string& nic){
  unitree::robot::ChannelFactory::Instance()->Init(0,nic);
  started_=true;
  state_sub_=unitree::robot::ChannelFactory::Instance()->CreateRecvChannel<State>("rt/lowstate",[this](const void* raw){const auto& s=*static_cast<const State*>(raw);std::lock_guard<std::mutex> g(mutex_);
   if(crc(s)!=s.crc()){++rejected_;return;}
   bool finite=true;
   for(int i=0;i<29;++i){const auto& m=s.motor_state()[i];finite=finite&&std::isfinite(m.q())&&std::isfinite(m.dq());}
   for(int i=0;i<3;++i)finite=finite&&std::isfinite(s.imu_state().rpy()[i])&&std::isfinite(s.imu_state().gyroscope()[i]);
   if(!finite){++rejected_;return;}
   if(!seen_||s.tick()!=state_.tick())for(int i=0;i<12;++i){
    const double speed=std::abs(s.motor_state()[i].dq());leg_speed_peaks_[i]=std::max(leg_speed_peaks_[i],speed);
    if(leg_speed_warning(speed))++leg_speed_samples_[i];
   }
   if(!seen_||s.tick()!=state_.tick())tick_changed_=Clock::now();
   state_=s;received_=Clock::now();seen_=true;},1);
  command_sub_=unitree::robot::ChannelFactory::Instance()->CreateRecvChannel<Cmd>("rt/arm_sdk",[this](const void* raw){const auto& c=*static_cast<const Cmd*>(raw);std::lock_guard<std::mutex> g(mutex_);
   if(std::find(own_crcs_.begin(),own_crcs_.end(),c.crc())==own_crcs_.end()){last_foreign_=Clock::now();++foreign_;}},16);
  monitor_=std::thread([this]{
   try{
   unitree::robot::g1::LocoClient loco;loco.SetTimeout(.5f);loco.Init();
   unitree::robot::b2::RobotStateClient service;service.SetTimeout(.5f);service.Init();
   const auto local=service.GetApiVersion(),remote=service.GetServerApiVersion();
   const bool versions=!local.empty()&&!remote.empty()&&local==remote;
   while(!monitor_stop_){int id=-1,mode=-1;
    const int a=loco.GetFsmId(id);
    {std::lock_guard<std::mutex> g(mutex_);id_query_.update(a,id,monotonic());fsm_=id_query_.value;}
    const int b=loco.GetFsmMode(mode);
    {std::lock_guard<std::mutex> g(mutex_);mode_query_.update(b,mode,monotonic());mode_=mode_query_.value;}
    std::vector<unitree::robot::b2::ServiceState> services;bool stopped=false;
    const int c=service.ServiceList(services);
    if(c==0)for(const auto& s:services)if(s.name=="g1_arm_example"&&s.status==1)stopped=true;
    {std::lock_guard<std::mutex> g(mutex_);service_query_.update(c,stopped?1:0,monotonic());if(c==0)stopped_=stopped;versions_match_=versions;rpc_checked_=Clock::now();}
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
   }
   }catch(const std::exception&){std::lock_guard<std::mutex> g(mutex_);id_query_.update(-2,-1,monotonic());mode_query_.update(-2,-1,monotonic());service_query_.update(-2,-1,monotonic());versions_match_=false;}
  });
  control_=std::thread([this]{loop();});
 }
 void command(const std::string& line){
  std::istringstream input(line);std::string op,id,extra,error;int index=-1;double value=0;
  input>>op>>id;
  if(id.empty()||id.size()>64||id.find_first_not_of("0123456789abcdef")!=std::string::npos)return;
  std::lock_guard<std::mutex> g(mutex_);
  if(op=="PING"){heartbeat_=Clock::now();}
  else if(op=="ACQUIRE"){
#ifndef G1_ARM_DEBUG_ENABLE_CONTROL
   error="control_disabled_in_build";
#else
   if(phase_!="readonly")error="not_readonly";
   else if(!(error=guard(true)).empty()){}
   else{
    lock_fd_=open("/tmp/g1-right-arm-bounded.lock",O_CREAT|O_RDWR,0600);
    if(lock_fd_<0||flock(lock_fd_,LOCK_EX|LOCK_NB)){error="another_local_arm_controller";if(lock_fd_>=0){close(lock_fd_);lock_fd_=-1;}}
    else{
     for(int i=0;i<14;++i)seed_[i]=desired_[i]=goal_[i]=state_.motor_state()[15+i].q();
     expected_machine_=state_.mode_machine();weight_=0;fault_.clear();fault_context_="null";release_to_fault_=false;
     publisher_.reset(new unitree::robot::ChannelPublisher<Cmd>("rt/arm_sdk"));publisher_->InitChannel();
     phase_="acquiring";phase_start_=heartbeat_=session_start_=Clock::now();
    }
   }
#endif
  }else if(op=="SET"){
   if(!(input>>index>>value)||input>>extra)error="invalid_target_command";
   else if(phase_!="holding")error="not_ready_or_busy";
   else if(id_query_.rc||mode_query_.rc||service_query_.rc)error="mode_query_pending";
   else if(!(error=guard()).empty())release(true,error);
   else if(!(error=target_error(index,value,desired_,seed_)).empty()){}
   else if(elapsed(session_start_)+motion_seconds(value-desired_[index-15])+settle_budget_seconds>session_seconds)error="insufficient_session_time";
   else{from_=desired_;goal_=desired_;moving_joint_=index-15;goal_[moving_joint_]=float(value);move_seconds_=motion_seconds(value-from_[moving_joint_]);phase_="moving";phase_start_=heartbeat_=Clock::now();}
  }else if(op=="RELEASE"){release(false,"");}
  else if(op=="QUIT"){closed_=true;release(false,"");}
  else error="unknown_command";
  std::ostringstream reply;reply<<"{\"type\":\"reply\",\"id\":"<<std::quoted(id)<<",\"ok\":"<<(error.empty()?"true":"false")<<",\"error\":"<<std::quoted(error)<<",\"phase\":"<<std::quoted(phase_)<<'}';emit(reply.str());
 }
 void finish(){
  {std::lock_guard<std::mutex> g(mutex_);if(finished_)return;finished_=true;closed_=true;release(false,"");}
  if(control_.joinable())control_.join();
  monitor_stop_=true;if(monitor_.joinable())monitor_.join();
  // Match the verified passive probe shutdown: detach native listener first.
  auto close_sub=[](auto& channel){if(!channel)return;auto reader=channel->GetReader()->GetNative();reader.listener(nullptr,::dds::core::status::StatusMask::none());reader.close();reader=::dds::core::null;channel.reset();};
  close_sub(command_sub_);close_sub(state_sub_);if(started_)unitree::robot::ChannelFactory::Instance()->Release();
 }
};
int main(int argc,char** argv){
 if(argc==2&&std::string(argv[1])=="--self-test"){
  std::array<float,14> zero{};
  if(target_error(22,20.1*pi/180,zero,zero)!="step_exceeds_20_degrees"||!target_error(15,0,zero,zero).size()||!target_error(22,NAN,zero,zero).size())return 3;
  auto previous=zero;previous[7]=float(30*pi/180);
  if(!target_error(22,50*pi/180,previous,zero).empty()||!target_error(22,20*pi/180,zero,zero).empty())return 3;
  previous[7]=float(50*pi/180);
  if(target_error(22,50.1*pi/180,previous,zero)!="session_exceeds_50_degrees")return 3;
  previous[7]=float(-30*pi/180);
  if(!target_error(22,-50*pi/180,previous,zero).empty())return 3;
  previous[7]=float(-50*pi/180);
  if(target_error(22,-50.1*pi/180,previous,zero)!="session_exceeds_50_degrees")return 3;
  const double duration=motion_seconds(max_step);
  if(std::abs(duration-7.5)>1e-9)return 3;
  for(int i=0;i<1000;++i)if((blend((i+1)/1000.)-blend(i/1000.))*max_step/(duration/1000)>rate+1e-6)return 3;
  RpcWatch id,mode;id.update(0,501,10);mode.update(0,0,10);
  mode.update(3104,-1,10.4);
  if(!mode_error(id,mode,10.5,false).empty()||mode_error(id,mode,10.5,true)!="mode_query_pending")return 4;
  id.update(0,501,11.4);
  if(mode_error(id,mode,11.6,false)!="fsm_mode_query_stale")return 4;
  // Replay the observed 501/0 -> 501/1 transition. It must not release an
  // existing arm session; acquiring a new session still starts from static.
  mode.update(0,1,11.7);
  if(!mode_error(id,mode,11.7,false).empty()||mode_error(id,mode,11.7,true)!="not_standing_at_acquire")return 4;
  mode.update(0,2,11.75);
  if(mode_error(id,mode,11.75,false)!="fsm_mode_unknown")return 4;
  mode.update(0,0,11.8);id.update(0,1,11.8);
  if(mode_error(id,mode,11.8,false)!="fsm_changed")return 4;
  id.update(0,501,11.9);
  if(!mode_error(id,mode,11.9,true).empty())return 4;
  // Replay the actual speed-trigger snapshot without DDS or motion. Speed is
  // observational; invalid samples and motor flags still prevent control.
  for(double dq:{-.336218089,-.544559896})
   if(!leg_state_error(.04,dq,0).empty()||!leg_speed_warning(dq))return 5;
  if(leg_state_error(.04,-.336218089,1)!="leg_motor_state"||leg_state_error(NAN,0,0)!="leg_data_invalid"||leg_state_error(0,INFINITY,0)!="leg_data_invalid")return 5;
  std::cout<<"{\"self_test\":\"passed\",\"dds_initialized\":false,\"motion_commands_sent\":0}"<<std::endl;return 0;
 }
 if(argc!=3||std::string(argv[1])!="--listen"||!if_nametoindex(argv[2]))return 2;
 std::signal(SIGPIPE,SIG_IGN);std::signal(SIGINT,signal_stop);std::signal(SIGTERM,signal_stop);
 try{Worker worker;worker.start(argv[2]);std::string line;while(!interrupted&&std::getline(std::cin,line)){if(line.size()>256)continue;worker.command(line);}worker.finish();return 0;}
 catch(const std::exception& e){std::cerr<<"worker_error: "<<e.what()<<std::endl;return 1;}
}
