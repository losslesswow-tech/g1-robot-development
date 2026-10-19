// Separate candidate bridge. No automatic stand/start/damp or mode changes.
#include "guard.hpp"
#include <ros/ros.h>
#include <geometry_msgs/Twist.h>
#include <sensor_msgs/Imu.h>
#include <std_msgs/String.h>
#include <std_srvs/SetBool.h>
#include <topic_tools/shape_shifter.h>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <unitree/dds_wrapper/robots/g1/g1_sub.h>
#include <atomic>
#include <chrono>
#include <fstream>
#include <mutex>
#include <sstream>
#include <thread>
#include <sys/stat.h>
#include <ctime>
#include <unistd.h>

using Clock = std::chrono::steady_clock;
double seconds() { return std::chrono::duration<double>(Clock::now().time_since_epoch()).count(); }
Guard gate;
std::mutex gate_mutex;
ros::Publisher imu_pub;
std::atomic<bool> running{true};
struct JoinThread {
  std::thread& thread;
  ~JoinThread() { running=false; if(thread.joinable()) thread.join(); }
};
bool allow_motion = false;
bool heartbeat_fresh(const std::string& file) {
  struct stat info{};
  return !file.empty() && lstat(file.c_str(),&info)==0 && S_ISREG(info.st_mode) && info.st_nlink==1
    && (info.st_mode & 0777)==0600 && info.st_uid==getuid()
    && std::time(nullptr)>=info.st_mtime && std::time(nullptr)-info.st_mtime<=15;
}

void low_state(const void* raw) {
  const auto* low = static_cast<const unitree_hg::msg::dds_::LowState_*>(raw);
  if (!low) return;
  { std::lock_guard<std::mutex> lock(gate_mutex); gate.low_at = seconds(); }
  sensor_msgs::Imu imu; imu.header.frame_id = "imu_link"; imu.header.stamp = ros::Time::now();
  const auto& q = low->imu_state().quaternion();
  const auto& w = low->imu_state().gyroscope(); const auto& a = low->imu_state().accelerometer();
  imu.orientation.w=q[0]; imu.orientation.x=q[1]; imu.orientation.y=q[2]; imu.orientation.z=q[3];
  imu.angular_velocity.x=w[0]; imu.angular_velocity.y=w[1]; imu.angular_velocity.z=w[2];
  imu.linear_acceleration.x=a[0]; imu.linear_acceleration.y=a[1]; imu.linear_acceleration.z=a[2];
  imu_pub.publish(imu);
}
void lidar(const ros::MessageEvent<topic_tools::ShapeShifter const>& event) {
  const auto connection = event.getConnectionHeaderPtr();
  if (connection && connection->count("latching") && connection->at("latching") == "1") return;
  std::lock_guard<std::mutex> lock(gate_mutex); gate.lidar_at = seconds();
}
void command(const ros::MessageEvent<geometry_msgs::Twist const>& event) {
  const auto connection=event.getConnectionHeaderPtr();
  if(connection && connection->count("latching") && connection->at("latching")=="1") {
    std::lock_guard<std::mutex> lock(gate_mutex); gate.disarm("latched command rejected",true); return;
  }
  const auto msg=event.getMessage();
  std::lock_guard<std::mutex> lock(gate_mutex);
  gate.command(seconds(),msg->linear.x,msg->linear.y,msg->angular.z,msg->linear.z,msg->angular.x,msg->angular.y);
}
bool arm(std_srvs::SetBool::Request& request, std_srvs::SetBool::Response& response) {
  std::lock_guard<std::mutex> lock(gate_mutex);
  if (!request.data) { gate.disarm("operator disarmed"); response.success=true; }
  else if (!allow_motion) { response.success=false; gate.reason="read-only session"; }
  else response.success=gate.arm(seconds());
  response.message=gate.reason; return true;
}
int main(int argc,char** argv) {
  ros::init(argc,argv,"g1_network_bridge"); ros::NodeHandle nh, pnh("~");
  std::string mode, interface, config, imu_topic, heartbeat, stop_file;
  std::thread fsm_thread;
  int session_seconds;
  pnh.param<std::string>("mode",mode,"wired");
  pnh.param<std::string>("networkInterface",interface,"enp132s0");
  pnh.param<std::string>("dds_config",config,"");
  pnh.param<bool>("allow_motion",allow_motion,false);
  pnh.param<std::string>("imu_topic",imu_topic,"/g1_network/imu");
  pnh.param<std::string>("heartbeat_file",heartbeat,"");
  pnh.param<std::string>("stop_file",stop_file,"");
  pnh.param<int>("session_seconds",session_seconds,3600);
  if ((mode!="wired" && mode!="wifi") || session_seconds<10 || session_seconds>3600
      || (mode=="wired" && interface!="enp132s0") || (allow_motion && !heartbeat_fresh(heartbeat))) {
    ROS_FATAL("Invalid session configuration or missing supervisor heartbeat"); return 2;
  }
  try {
    if (mode=="wired") unitree::robot::ChannelFactory::Instance()->Init(0,interface);
    else {
      std::ifstream input(config); std::stringstream buffer; buffer << input.rdbuf();
      if (!input || buffer.str().empty() || buffer.str().size()>32768) throw std::runtime_error("Missing generated Wi-Fi DDS config");
      unitree::common::JsonMap parameters; parameters["DomainId"]=uint32_t(0); parameters["Config"]=buffer.str();
      unitree::robot::ChannelFactory::Instance()->Init(parameters);
    }
    imu_pub=nh.advertise<sensor_msgs::Imu>(imu_topic,50);
    auto status_pub=pnh.advertise<std_msgs::String>("status",1,true);
    auto cmd_sub=nh.subscribe("/cmd_vel",1,command,ros::TransportHints().tcpNoDelay());
    auto lidar_sub=nh.subscribe("/livox/lidar",1,lidar);
    auto arm_service=pnh.advertiseService("arm",arm);
    unitree::robot::ChannelSubscriber<unitree_hg::msg::dds_::LowState_> sub("rt/lowstate"); sub.InitChannel(low_state,1);
    unitree::robot::g1::LocoClient motion, query; motion.Init(); motion.SetTimeout(.2f); query.Init(); query.SetTimeout(.3f);
    JoinThread thread_guard{fsm_thread};
    fsm_thread=std::thread([&] {
      while(running && ros::ok()) {
        int id=-1, code=-1;
        try { code=query.GetFsmId(id); } catch(const std::exception&) { code=-1; }
        { std::lock_guard<std::mutex> lock(gate_mutex); gate.fsm=id; gate.fsm_code=code; gate.fsm_at=seconds(); }
        for(int i=0;i<10 && running && ros::ok();++i) std::this_thread::sleep_for(std::chrono::milliseconds(100));
      }
    });
    ros::AsyncSpinner spinner(1); spinner.start(); ros::WallRate rate(20);
    const double deadline=seconds()+session_seconds; bool commanded=false; double last_status=0;
    while(ros::ok() && seconds()<deadline && (stop_file.empty() || !std::ifstream(stop_file).good())) {
      if(!heartbeat.empty() && !heartbeat_fresh(heartbeat)) { ROS_ERROR("Supervisor heartbeat expired"); break; }
      Guard current; bool ready;
      {
        // Serialize each bounded RPC with arm/command changes to prevent sending
        // a copied command after operator disarm. RPC timeout is 0.2 seconds.
        std::lock_guard<std::mutex> lock(gate_mutex); ready=gate.ready(seconds());
        if(allow_motion && ready) {
          int code=-1;
          try { code=motion.SetVelocity(gate.vx,gate.vy,gate.yaw,.30f); } catch(const std::exception&) { code=-1; }
          commanded=true;
          if(code!=0) gate.disarm("velocity RPC failed or result uncertain",true);
        }
        current=gate;
      }
      if(!(allow_motion && ready) && commanded) {
        // Best effort only: a lost wireless link cannot guarantee receipt of zero.
        const int code=motion.SetVelocity(0,0,0,.30f); commanded=false;
        if(code!=0) ROS_ERROR("Zero-velocity request not acknowledged; use hardware remote stop");
      }
      if(seconds()-last_status>=.5) {
        std_msgs::String message;
        std::ostringstream out; out << "{\"mode\":\"" << mode << "\",\"armed\":" << (current.armed?"true":"false")
          << ",\"healthy\":" << (current.healthy(seconds())?"true":"false") << ",\"fault\":" << (current.fault?"true":"false")
          << ",\"fsm\":" << current.fsm << ",\"reason\":\"" << current.reason << "\"}";
        message.data=out.str(); status_pub.publish(message); last_status=seconds();
      }
      rate.sleep();
    }
    { std::lock_guard<std::mutex> lock(gate_mutex); gate.disarm("session ending"); }
    spinner.stop(); running=false; fsm_thread.join();
    if(commanded && allow_motion && motion.SetVelocity(0,0,0,.30f)!=0) ROS_ERROR("Exit zero-velocity request not acknowledged");
    // No Damp on exit. Firmware duration behavior requires a supervised field test.
    return 0;
  } catch(const std::exception& error) {
    running=false; if(fsm_thread.joinable()) fsm_thread.join();
    ROS_FATAL("%s",error.what()); return 2;
  }
}
