// G1 device-state API 1.0.0.2. Only g1_arm_example may be switched.
// No motion publisher, locomotion setters or hand commands.
#include <unitree/robot/b2/robot_state/robot_state_client.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/idl/hg/LowCmd_.hpp>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <net/if.h>

using Clock = std::chrono::steady_clock;
using State = unitree_hg::msg::dds_::LowState_;
using Cmd = unitree_hg::msg::dds_::LowCmd_;
using unitree::robot::b2::RobotStateClient;
using unitree::robot::b2::ServiceState;
constexpr const char* Service = "g1_arm_example";
std::atomic<bool> interrupted{false};
void on_signal(int){ interrupted = true; }
double age(Clock::time_point time){return std::chrono::duration<double>(Clock::now()-time).count();}

template<class T> uint32_t crc(const T& value){
    uint32_t result=0xffffffff;
    const auto* bytes=reinterpret_cast<const unsigned char*>(&value);
    for(size_t i=0;i<sizeof(T)/4-1;++i){
        uint32_t word;std::memcpy(&word,bytes+4*i,4);
        for(uint32_t mask=0x80000000;mask;mask>>=1){
            bool high=result&0x80000000;result<<=1;
            if(high)result^=0x04c11db7;
            if(word&mask)result^=0x04c11db7;
        }
    }
    return result;
}

std::string state_error(const State& s){
    if(s.mode_machine()!=5 || s.mode_pr()!=0)return "machine_mode_unconfirmed";
    const auto& r=s.wireless_remote();
    if((unsigned(r[2])|(unsigned(r[3])<<8))&(1u<<3))return "controller_select_stop";
    for(int i=0;i<29;++i){const auto& m=s.motor_state()[i];
        if(!std::isfinite(m.q())||!std::isfinite(m.dq())||m.motorstate()!=0)return "motor_state_invalid_or_fault";
    }
    return {};
}

template<class T> void detach(unitree::robot::ChannelPtr<T>& channel){
    if(!channel)return;
    auto reader=channel->GetReader()->GetNative();
    reader.listener(nullptr,::dds::core::status::StatusMask::none());reader.close();
    reader=::dds::core::null;channel.reset();
}

class RestoreObservation {
    std::mutex mutex_;State state_{};bool seen_=false;
    Clock::time_point received_{},tick_{},writer_=Clock::now();
    unitree::robot::ChannelPtr<State> state_sub_;
    unitree::robot::ChannelPtr<Cmd> arm_sub_;
public:
    RestoreObservation(){
        state_sub_=unitree::robot::ChannelFactory::Instance()->CreateRecvChannel<State>("rt/lowstate",[this](const void* p){
            const auto& s=*static_cast<const State*>(p);
            if(crc(s)!=s.crc())return;
            std::lock_guard<std::mutex> lock(mutex_);
            if(!seen_||s.tick()!=state_.tick())tick_=Clock::now();
            state_=s;seen_=true;received_=Clock::now();
        },1);
        arm_sub_=unitree::robot::ChannelFactory::Instance()->CreateRecvChannel<Cmd>("rt/arm_sdk",[this](const void*){
            std::lock_guard<std::mutex> lock(mutex_);writer_=Clock::now();
        },16);
    }
    std::string guard(){
        std::lock_guard<std::mutex> lock(mutex_);
        if(interrupted)return "signal_stop";
        if(!seen_||age(received_)>.15||age(tick_)>.15)return "stale_lowstate";
        if(age(writer_)<1)return "another_arm_sdk_writer";
        return state_error(state_);
    }
    ~RestoreObservation(){detach(arm_sub_);detach(state_sub_);}
};

int list(RobotStateClient& client,const char* phase,int& target_status,int& target_protect){
    std::vector<ServiceState> states;
    int rc=client.ServiceList(states);target_status=target_protect=-1;
    std::cout<<"{\"event\":\"service_list\",\"phase\":"<<std::quoted(phase)<<",\"return_code\":"<<rc<<",\"services\":[";
    bool first=true;
    for(const auto& s:states){
        if(s.name!=Service)continue;
        if(!first)std::cout<<',';
        first=false;target_status=s.status;target_protect=s.protect;
        std::cout<<"{\"name\":"<<std::quoted(s.name)<<",\"status_raw\":"<<s.status<<",\"protect_raw\":"<<s.protect<<'}';
    }
    std::cout<<"]}"<<std::endl;
    return rc;
}

int run(const char* nic,const std::string& operation){
    unitree::robot::ChannelFactory::Instance()->Init(0,nic);
    const bool restore=operation=="--restore";
    std::unique_ptr<RestoreObservation> observation;
    if(restore)observation.reset(new RestoreObservation());
    RobotStateClient client;client.SetTimeout(3.0f);client.Init();
    const auto local=client.GetApiVersion(),remote=client.GetServerApiVersion();
    std::cout<<"{\"event\":\"api_version\",\"client\":"<<std::quoted(local)<<",\"server\":"<<std::quoted(remote)<<'}'<<std::endl;
    if(local!="1.0.0.2"||remote!=local)return 4;
    int status,protect;
    if(list(client,"before",status,protect)||status<0)return 5;
    if(operation=="--query")return 0;
    const int desired=restore?0:1;
    if(status==desired){
        std::cout<<"{\"event\":"<<std::quoted(restore?"already_running":"already_stopped")<<",\"switch_calls\":0}"<<std::endl;
        return 0;
    }
    if((status!=0&&status!=1)||protect!=0)return 6;
    unitree::robot::g1::LocoClient loco;loco.SetTimeout(3.0f);loco.Init();
    int id=-1,mode=-1;int r1=loco.GetFsmId(id),r2=loco.GetFsmMode(mode);
    std::cout<<"{\"event\":\"mode_before_switch\",\"id_return_code\":"<<r1<<",\"mode_return_code\":"<<r2<<",\"fsm_id\":"<<id<<",\"fsm_mode\":"<<mode<<'}'<<std::endl;
    if(r1||r2||id!=501||(restore?(mode!=0&&mode!=1):mode!=0))return 7;
    if(restore){
        auto deadline=Clock::now()+std::chrono::seconds(3);std::string error;
        do{
            error=observation->guard();
            if(error.empty())break;
            if(error=="controller_select_stop"||error=="motor_state_invalid_or_fault"||error=="signal_stop")break;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }while(Clock::now()<deadline);
        std::cout<<"{\"event\":\"restore_guard\",\"error\":"<<std::quoted(error)<<'}'<<std::endl;
        if(!error.empty())return 10;
    }
    if(interrupted)return 11;
    int reply=-1;int rc=client.ServiceSwitch(Service,restore?1:0,reply);
    std::cout<<"{\"event\":"<<std::quoted(restore?"service_restore":"service_stop")<<",\"name\":\"g1_arm_example\",\"switch\":"<<(restore?1:0)<<",\"return_code\":"<<rc<<",\"status_raw\":"<<reply<<'}'<<std::endl;
    if(rc)return 8;
    auto deadline=Clock::now()+std::chrono::seconds(12);
    while(Clock::now()<deadline){
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        if(!list(client,"after",status,protect)&&status==desired){
            std::cout<<"{\"marker\":"<<std::quoted(restore?"G1_BUILTIN_ARM_RESTORE_VERIFIED":"G1_BUILTIN_ARM_STOP_VERIFIED")<<",\"status_raw\":"<<desired<<",\"switch_calls\":1,\"motion_commands_sent\":0}"<<std::endl;
            return 0;
        }
    }
    return 9;
}

int main(int argc,char** argv){
    if(argc==2&&std::string(argv[1])=="--self-test"){
        State s{};s.mode_machine(5);s.mode_pr(0);
        bool ok=state_error(s).empty();
        s.wireless_remote()[2]=8;ok=ok&&state_error(s)=="controller_select_stop";
        s.wireless_remote()[2]=0;s.motor_state()[0].motorstate(1);
        ok=ok&&state_error(s)=="motor_state_invalid_or_fault";
        std::cout<<"{\"self_test\":"<<(ok?"true":"false")<<",\"dds_initialized\":false,\"service_switch_calls\":0}"<<std::endl;
        return ok?0:1;
    }
    if(argc!=3)return 2;
    std::string operation(argv[1]);
    if(operation!="--query"&&operation!="--stop"&&operation!="--restore")return 2;
    if(!if_nametoindex(argv[2]))return 2;
    std::signal(SIGINT,on_signal);std::signal(SIGTERM,on_signal);
    int result=1;
    try{result=run(argv[2],operation);}catch(const std::exception& e){std::cerr<<e.what()<<std::endl;}
    unitree::robot::ChannelFactory::Instance()->Release();
    return result;
}
