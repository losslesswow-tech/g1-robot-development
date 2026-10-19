// G1 teaching transport. No locomotion setters, replay or hand writes.
// Startup is read-only; only explicit T frames create an arm_sdk publisher.
#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/idl/hg/LowCmd_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <unitree/robot/b2/robot_state/robot_state_client.hpp>
#include <array>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <thread>
#include <unistd.h>
#include <sys/file.h>
#include "gravity.hpp"

using Clock=std::chrono::steady_clock;
using State=unitree_hg::msg::dds_::LowState_;
using Cmd=unitree_hg::msg::dds_::LowCmd_;
std::atomic<bool> interrupted{false};
void stop_signal(int){interrupted=true;}
double seconds(Clock::time_point t){return std::chrono::duration<double>(Clock::now()-t).count();}
template<class T> uint32_t crc(const T& value){
 uint32_t result=0xffffffff;const auto* bytes=reinterpret_cast<const unsigned char*>(&value);
 for(size_t i=0;i<sizeof(T)/4-1;++i){uint32_t word;std::memcpy(&word,bytes+4*i,4);
  for(uint32_t mask=0x80000000;mask;mask>>=1){bool high=result&0x80000000;result<<=1;if(high)result^=0x04c11db7;if(word&mask)result^=0x04c11db7;}}
 return result;
}
constexpr std::array<double,17> lower={-2.618,-.52,-.52,-3.0892,-1.5882,-2.618,-1.0472,-1.972222054,-1.614429558,-1.614429558,-3.0892,-2.2515,-2.618,-1.0472,-1.972222054,-1.614429558,-1.614429558};
constexpr std::array<double,17> upper={2.618,.52,.52,2.6704,2.2515,2.618,2.0944,1.972222054,1.614429558,1.614429558,2.6704,1.5882,2.618,2.0944,1.972222054,1.614429558,1.614429558};
bool valid_target(const std::array<double,17>& q,double w){
 if(!std::isfinite(w)||w<0||w>1)return false;
 for(int i=0;i<17;++i)if(!std::isfinite(q[i])||q[i]<lower[i]||q[i]>upper[i])return false;
 return true;
}
Cmd frame(const std::array<double,17>& q,double weight,int machine,double soften=0,const gravity::Torques& tau={}){
 Cmd c{};c.mode_machine(machine);c.mode_pr(0);
 for(int i=12;i<29;++i){auto& m=c.motor_cmd()[i];m.mode(1);m.q(q[i-12]);m.dq(0);m.tau(i>=15&&weight>0?tau[i-15]:0);m.kp(i<15?200:40*(1-soften));m.kd(i<15?5:10*(1-soften)+.8*soften);}
 c.motor_cmd()[29].q(weight);c.crc(crc(c));return c;
}
template<class T> void detach(unitree::robot::ChannelPtr<T>& channel){
 if(!channel)return;
 auto reader=channel->GetReader()->GetNative();
 reader.listener(nullptr,::dds::core::status::StatusMask::none());reader.close();
 reader=::dds::core::null;channel.reset();
}

class Transport{
 std::mutex mutex_;State state_{};bool seen_=false;
 Clock::time_point received_{},tick_at_{},foreign_at_{},rpc_at_{};
 int fsm_=-1,mode_=-1,machine_=0;bool service_stopped_=false,api_match_=false;
 uint64_t samples_=0,rejected_=0,foreign_=0,sent_=0,zeros_=0;
 std::deque<uint32_t> own_;
 std::atomic<bool> monitor_stop_{false};std::thread monitor_;
 unitree::robot::ChannelPtr<State> state_sub_;
 unitree::robot::ChannelPtr<Cmd> command_sub_;
 unitree::robot::ChannelPublisherPtr<Cmd> publisher_;
 std::array<double,17> target_{};double weight_=0,soften_=0;
 int lock_=-1;bool allow_=false,acquired_=false;
 bool gravity_=false,releasing_=false;
 gravity::Ramp gravity_ramp_;
 gravity::Torques feedforward_{},release_feedforward_{};
 double release_weight_=0;
 Clock::time_point gravity_start_{},last_send_{};

 gravity::Torques gravity_locked(){
  std::array<double,17> q{};std::array<double,4> imu{};
  for(int i=0;i<17;++i)q[i]=state_.motor_state()[12+i].q();
  for(int i=0;i<4;++i)imu[i]=state_.imu_state().quaternion()[i];
  return gravity::calculate(q,imu);
 }

 void send(double weight){
  auto now=Clock::now();
  if(!gravity_||weight==0){feedforward_.fill(0);gravity_ramp_.value.fill(0);}
  else if(releasing_)feedforward_=gravity::handback(release_feedforward_,release_weight_,weight);
  else{
   gravity::Torques raw;{std::lock_guard<std::mutex> g(mutex_);raw=gravity_locked();}
   if(gravity::exceeds(raw))throw std::runtime_error("gravity_feedforward_limit");
   feedforward_=gravity_ramp_.update(raw,std::chrono::duration<double>(now-gravity_start_).count(),
                                   std::chrono::duration<double>(now-last_send_).count(),weight);
  }
  last_send_=now;
  auto c=frame(target_,weight,machine_,soften_,feedforward_);
  {std::lock_guard<std::mutex> g(mutex_);own_.push_back(c.crc());if(own_.size()>512)own_.pop_front();}
  if(!publisher_->Write(c))throw std::runtime_error("dds_write_failed");
  weight_=weight;++sent_;if(weight==0)++zeros_;
  std::cout<<"{\"type\":\"command\",\"sequence\":"<<sent_<<",\"weight\":"<<weight<<",\"q_upper\":[";
  for(int i=0;i<17;++i){if(i)std::cout<<',';std::cout<<target_[i];}
  std::cout<<"],\"feedforward_nm\":[";for(int i=0;i<14;++i){if(i)std::cout<<',';std::cout<<feedforward_[i];}
  std::cout<<"],\"gravity_enabled\":"<<(gravity_?"true":"false")<<",\"mode_machine\":"<<machine_<<",\"mode_pr\":0,\"crc\":"<<c.crc()<<"}"<<std::endl;
 }
 std::string guard(bool initial){
  std::lock_guard<std::mutex> g(mutex_);
  if(!seen_||seconds(received_)>.15||seconds(tick_at_)>.15)return "stale_lowstate";
  if(seconds(rpc_at_)>2||fsm_!=501||(mode_!=0&&mode_!=1))return "regular_mode_unconfirmed";
  if(!api_match_||!service_stopped_)return "builtin_arm_service_active_or_unconfirmed";
  if(seconds(foreign_at_)<1)return "another_arm_sdk_writer";
  if(!initial&&state_.mode_machine()!=machine_)return "machine_mode_changed";
  const auto& remote=state_.wireless_remote();const unsigned keys=unsigned(remote[2])|(unsigned(remote[3])<<8);
  if(keys&(1u<<3))return "controller_select_stop";
  for(int i=0;i<29;++i){const auto& m=state_.motor_state()[i];
   if(!std::isfinite(m.q())||!std::isfinite(m.dq())||m.motorstate()!=0)return "motor_state_invalid_or_fault";
  }
  if(gravity_){try{if(gravity::exceeds(gravity_locked()))return "gravity_feedforward_limit";}catch(const std::exception& e){return e.what();}}
  return {};
 }
 void emit_state(){
  std::lock_guard<std::mutex> g(mutex_);
  if(!seen_)return;
  gravity::Torques preview{};std::string gravity_error;
  if(gravity_){try{preview=gravity_locked();if(gravity::exceeds(preview))gravity_error="gravity_feedforward_limit";}catch(const std::exception& e){gravity_error=e.what();}}
  std::cout<<"{\"type\":\"state\",\"age_s\":"<<seconds(received_)<<",\"tick_age_s\":"<<seconds(tick_at_)
   <<",\"tick\":"<<state_.tick()<<",\"mode_machine\":"<<int(state_.mode_machine())<<",\"mode_pr\":"<<int(state_.mode_pr())
   <<",\"fsm\":"<<fsm_<<",\"fsm_mode\":"<<mode_<<",\"service_stopped\":"<<(service_stopped_?"true":"false")
   <<",\"api_match\":"<<(api_match_?"true":"false")<<",\"rpc_age_s\":"<<seconds(rpc_at_)<<",\"foreign_age_s\":"<<seconds(foreign_at_)
   <<",\"foreign_frames\":"<<foreign_<<",\"samples\":"<<samples_<<",\"rejected\":"<<rejected_<<",\"weight\":"<<weight_<<",\"sent\":"<<sent_<<",\"q\":[";
  for(int i=0;i<29;++i){if(i)std::cout<<',';std::cout<<state_.motor_state()[i].q();}
  std::cout<<"],\"dq\":[";for(int i=0;i<29;++i){if(i)std::cout<<',';std::cout<<state_.motor_state()[i].dq();}
  std::cout<<"],\"tau_est\":[";for(int i=0;i<29;++i){if(i)std::cout<<',';std::cout<<state_.motor_state()[i].tau_est();}
  std::cout<<"],\"motorstate\":[";for(int i=0;i<29;++i){if(i)std::cout<<',';std::cout<<state_.motor_state()[i].motorstate();}
  std::cout<<"],\"quaternion\":[";for(int i=0;i<4;++i){if(i)std::cout<<',';std::cout<<state_.imu_state().quaternion()[i];}
  std::cout<<"],\"rpy\":[";for(int i=0;i<3;++i){if(i)std::cout<<',';std::cout<<state_.imu_state().rpy()[i];}
  std::cout<<"],\"gyro\":[";for(int i=0;i<3;++i){if(i)std::cout<<',';std::cout<<state_.imu_state().gyroscope()[i];}
  const auto& r=state_.wireless_remote();unsigned keys=unsigned(r[2])|(unsigned(r[3])<<8);
  std::cout<<"],\"remote_keys\":"<<keys<<",\"gravity_enabled\":"<<(gravity_?"true":"false")
   <<",\"gravity_model_sha256\":"<<std::quoted(gravity::MODEL_SHA256)<<",\"gravity_ready\":"<<(gravity_error.empty()?"true":"false")
   <<",\"gravity_error\":"<<std::quoted(gravity_error)<<",\"hand_load_kg\":["<<gravity::LOADS[0].mass<<','<<gravity::LOADS[1].mass<<"],\"gravity_raw_nm\":[";
  for(int i=0;i<14;++i){if(i)std::cout<<',';std::cout<<preview[i];}
  std::cout<<"],\"feedforward_nm\":[";for(int i=0;i<14;++i){if(i)std::cout<<',';std::cout<<feedforward_[i];}
  std::cout<<"]}"<<std::endl;
 }
public:
 Transport(const char* nic,bool allow,bool gravity=false):allow_(allow),gravity_(gravity){
  unitree::robot::ChannelFactory::Instance()->Init(0,nic);
  state_sub_=unitree::robot::ChannelFactory::Instance()->CreateRecvChannel<State>("rt/lowstate",[this](const void* p){
   const auto& s=*static_cast<const State*>(p);std::lock_guard<std::mutex> g(mutex_);
   if(crc(s)!=s.crc()){++rejected_;return;}
   if(!seen_||s.tick()!=state_.tick())tick_at_=Clock::now();
   state_=s;received_=Clock::now();seen_=true;++samples_;},1);
  command_sub_=unitree::robot::ChannelFactory::Instance()->CreateRecvChannel<Cmd>("rt/arm_sdk",[this](const void* p){
   const auto& c=*static_cast<const Cmd*>(p);std::lock_guard<std::mutex> g(mutex_);
   if(std::find(own_.begin(),own_.end(),c.crc())==own_.end()){foreign_at_=Clock::now();++foreign_;}},16);
  monitor_=std::thread([this]{try{
   unitree::robot::g1::LocoClient loco;loco.SetTimeout(.5f);loco.Init();
   unitree::robot::b2::RobotStateClient service;service.SetTimeout(.5f);service.Init();
   const auto a=service.GetApiVersion(),b=service.GetServerApiVersion();
   while(!monitor_stop_){int id=-1,mode=-1;
    int r1=loco.GetFsmId(id),r2=loco.GetFsmMode(mode);
    std::vector<unitree::robot::b2::ServiceState> states;int r3=service.ServiceList(states);bool off=false;
    for(const auto& s:states)if(s.name=="g1_arm_example"&&s.status==1)off=true;
    if(!r1&&!r2&&!r3){std::lock_guard<std::mutex> g(mutex_);fsm_=id;mode_=mode;service_stopped_=off;api_match_=!a.empty()&&a==b;rpc_at_=Clock::now();}
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
   }
  }catch(const std::exception& e){std::cerr<<"RPC monitor: "<<e.what()<<std::endl;}});
 }
 void finish(bool urgent){
  if(!publisher_)return;
  // Hold the latest measured upper-body pose during normal handback.
  {std::lock_guard<std::mutex> g(mutex_);if(seen_)for(int i=0;i<17;++i)target_[i]=state_.motor_state()[i+12].q();}
  double start=weight_;int n=urgent?0:50;
  releasing_=true;release_feedforward_=feedforward_;release_weight_=start;
  for(int i=1;i<=n;++i){send(start*(1-double(i)/n));std::this_thread::sleep_for(std::chrono::milliseconds(20));}
  for(int i=0;i<10;++i){send(0);std::this_thread::sleep_for(std::chrono::milliseconds(20));}
  publisher_.reset();
 }
 int run(double observe_seconds,double max_seconds=25){
  std::cout<<std::setprecision(9);
  fcntl(STDIN_FILENO,F_SETFL,fcntl(STDIN_FILENO,F_GETFL)|O_NONBLOCK);
  std::string buffer,reason="completed";auto start=Clock::now(),last_input=start,first_frame=start;
  bool done=false,urgent=false;int result=0;
  while(!done){auto next=Clock::now()+std::chrono::milliseconds(20);
   char bytes[4096];ssize_t count=read(STDIN_FILENO,bytes,sizeof(bytes));
   if(count>0)buffer.append(bytes,count);
   if(count==0){done=true;reason="stdin_closed";}
   if(buffer.size()>8192){done=true;reason="protocol_overflow";result=3;}
   auto end=buffer.find('\n');
   while(end!=std::string::npos&&!done){std::string line=buffer.substr(0,end);buffer.erase(0,end+1);end=buffer.find('\n');
    if(!line.empty()&&line.back()=='\r')line.pop_back();
    if(line=="RELEASE"||line=="QUIT"){done=true;reason=line;break;}
    std::istringstream input(line);std::string op,extra;double w,soften=-1;std::array<double,17> target{};
    bool ok=bool(input>>op>>w>>soften)&&op=="T"&&std::isfinite(soften)&&soften>=0&&soften<=1;for(double& q:target)ok=bool(input>>q)&&ok;
    if(input>>extra)ok=false;
    if(!allow_||!ok||!valid_target(target,w)||(gravity_&&soften!=0)){done=true;result=3;reason="invalid_or_disallowed_command";break;}
    auto error=guard(!acquired_);if(!error.empty()){done=true;result=3;reason=error;urgent=error=="controller_select_stop"||error=="machine_mode_changed";break;}
    if(!publisher_){
     if(w!=0){done=true;result=3;reason="first_frame_must_have_zero_weight";break;}
     lock_=open("/tmp/g1-right-arm-bounded.lock",O_CREAT|O_RDWR,0600);
     if(lock_<0||flock(lock_,LOCK_EX|LOCK_NB)){done=true;result=3;reason="another_local_arm_controller";break;}
     {std::lock_guard<std::mutex> g(mutex_);machine_=state_.mode_machine();}
     publisher_.reset(new unitree::robot::ChannelPublisher<Cmd>("rt/arm_sdk"));publisher_->InitChannel();
     first_frame=Clock::now();acquired_=true;
     gravity_start_=last_send_=first_frame;
    }
    target_=target;soften_=soften;
    try{send(w);}catch(const std::exception& e){done=true;result=3;reason=e.what();break;}
    last_input=Clock::now();
   }
   if(acquired_&&!done){auto error=guard(false);if(!error.empty()){done=true;result=3;reason=error;urgent=error=="controller_select_stop"||error=="machine_mode_changed";}
    if(seconds(last_input)>.25){done=true;result=3;reason="policy_input_watchdog";}
    if(seconds(first_frame)>max_seconds){done=true;result=3;reason="single_trial_deadline";}
   }
   if(interrupted){done=true;reason="signal_stop";urgent=true;result=3;}
   if(!acquired_&&seconds(start)>observe_seconds){done=true;reason="observation_complete";}
   emit_state();std::this_thread::sleep_until(next);
  }
  try{finish(urgent);}catch(const std::exception& e){reason=std::string("handback_exception:")+e.what();result=4;}
  emit_state();
  std::cout<<"{\"type\":\"finished\",\"reason\":"<<std::quoted(reason)<<",\"sent\":"<<sent_<<",\"zero_weight_frames\":"<<zeros_<<",\"final_weight\":"<<weight_<<",\"returncode\":"<<result<<"}"<<std::endl;
  return result;
 }
 ~Transport(){monitor_stop_=true;if(monitor_.joinable())monitor_.join();detach(command_sub_);detach(state_sub_);if(lock_>=0){flock(lock_,LOCK_UN);close(lock_);}unitree::robot::ChannelFactory::Instance()->Release();}
};

int main(int argc,char** argv){
 if(argc==2&&std::string(argv[1])=="--self-test"){
  std::array<double,17> q{};auto c=frame(q,0,5);
  bool ok=valid_target(q,0)&&c.motor_cmd()[29].q()==0&&c.crc()==crc(c);
  for(int i=0;i<12;++i)ok=ok&&c.motor_cmd()[i].kp()==0&&c.motor_cmd()[i].tau()==0;
  for(int i=12;i<29;++i)ok=ok&&c.motor_cmd()[i].mode()==1&&c.motor_cmd()[i].kp()==(i<15?200:40);
  auto soft=frame(q,1,5,1);
  for(int i=15;i<29;++i)ok=ok&&soft.motor_cmd()[i].kp()==0&&std::abs(soft.motor_cmd()[i].kd()-.8)<1e-6;
  ok=ok&&soft.motor_cmd()[12].kp()==200&&soft.mode_machine()==5&&soft.crc()==crc(soft);
  gravity::Torques torque{};torque.fill(1.2);auto compensated=frame(q,1,5,0,torque),zero=frame(q,0,5,0,torque);
  for(int i=0;i<15;++i)ok=ok&&compensated.motor_cmd()[i].tau()==0;
  for(int i=15;i<29;++i)ok=ok&&std::abs(compensated.motor_cmd()[i].tau()-1.2)<1e-6&&zero.motor_cmd()[i].tau()==0;
  ok=ok&&compensated.crc()==crc(compensated)&&zero.crc()==crc(zero);
  q[0]=100;ok=ok&&!valid_target(q,0);
  std::cout<<"{\"self_test\":"<<(ok?"true":"false")<<",\"dds_initialized\":false,\"sent\":0}"<<std::endl;return ok?0:1;
 }
 if(argc<3||(std::string(argv[1])!="--observe"&&std::string(argv[1])!="--session"))return 2;
 double max_seconds=90;bool enable_gravity=false,limit_seen=false;
 for(int i=3;i<argc;++i){
  std::string option(argv[i]);
  if(option=="--gravity"&&!enable_gravity){enable_gravity=true;continue;}
  if(option!="--max-seconds"||limit_seen||i+1>=argc||std::string(argv[1])!="--session")return 2;
  limit_seen=true;
  try{size_t used=0;std::string value(argv[++i]);max_seconds=std::stod(value,&used);
   if(used!=value.size()||!std::isfinite(max_seconds)||max_seconds<10||max_seconds>1800||std::floor(max_seconds)!=max_seconds)return 2;
  }catch(...){return 2;}
 }
 std::signal(SIGINT,stop_signal);std::signal(SIGTERM,stop_signal);std::signal(SIGPIPE,SIG_IGN);
 try{Transport transport(argv[2],std::string(argv[1])=="--session",enable_gravity);return transport.run(std::string(argv[1])=="--session"?3600:8,max_seconds);}
 catch(const std::exception& e){std::cerr<<e.what()<<std::endl;return 1;}
}
