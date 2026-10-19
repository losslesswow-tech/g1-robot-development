// Unitree G1 DDS-to-ROS1 bridge.
// Safe defaults: receive LowState and publish ROS status only. Motion APIs are
// created only when enable_motion is explicitly true; auto_start also requires
// enable_motion and is false by default.

#include <geometry_msgs/Twist.h>
#include <ros/ros.h>
#include <sensor_msgs/Imu.h>
#include <std_msgs/String.h>

#include <unitree/dds_wrapper/robots/g1/g1_sub.h>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>

#include <atomic>
#include <chrono>
#include <memory>
#include <sstream>
#include <string>
#include <thread>

namespace {

std::unique_ptr<unitree::robot::g1::LocoClient> g_loco;
ros::Publisher g_imu_pub;
ros::Publisher g_status_pub;
std::atomic<bool> g_received_lowstate{false};

void PublishStatus(const std::string& value)
{
  std_msgs::String status;
  status.data = value;
  g_status_pub.publish(status);
}

void LowStateHandler(const void* message)
{
  const auto* low =
      static_cast<const unitree_hg::msg::dds_::LowState_*>(message);
  if (low == nullptr) {
    return;
  }

  sensor_msgs::Imu imu;
  imu.header.frame_id = "imu_link";
  imu.header.stamp = ros::Time::now();

  const auto& q = low->imu_state().quaternion();
  const auto& gyro = low->imu_state().gyroscope();
  const auto& acc = low->imu_state().accelerometer();

  imu.orientation.w = q[0];
  imu.orientation.x = q[1];
  imu.orientation.y = q[2];
  imu.orientation.z = q[3];
  imu.angular_velocity.x = gyro[0];
  imu.angular_velocity.y = gyro[1];
  imu.angular_velocity.z = gyro[2];
  imu.linear_acceleration.x = acc[0];
  imu.linear_acceleration.y = acc[1];
  imu.linear_acceleration.z = acc[2];

  g_imu_pub.publish(imu);
  if (!g_received_lowstate.exchange(true)) {
    ROS_INFO("g1_ros1_sdk: first rt/lowstate sample received");
    PublishStatus("dds_lowstate_active");
  }
}

void CmdVelCallback(const geometry_msgs::Twist::ConstPtr& message)
{
  if (!g_loco) {
    ROS_ERROR_THROTTLE(1.0, "motion command rejected: motion client is disabled");
    return;
  }

  const int command = static_cast<int>(message->linear.z);
  if (command == 1) {
    g_loco->BalanceStand();
    return;
  }
  if (command == 2) {
    g_loco->Damp();
    return;
  }
  g_loco->Move(message->linear.x, message->linear.y, message->angular.z);
}

}  // namespace

int main(int argc, char** argv)
{
  ros::init(argc, argv, "g1_ros1_sdk_node");
  ros::NodeHandle nh;
  ros::NodeHandle pnh("~");

  std::string network_interface = "eth0";
  int dds_domain_id = 0;
  bool enable_motion = false;
  bool auto_start = false;
  double auto_start_delay_sec = 3.0;
  pnh.param<std::string>("network_interface", network_interface,
                         network_interface);
  pnh.param("dds_domain_id", dds_domain_id, dds_domain_id);
  pnh.param("enable_motion", enable_motion, enable_motion);
  pnh.param("auto_start", auto_start, auto_start);
  pnh.param("auto_start_delay_sec", auto_start_delay_sec,
            auto_start_delay_sec);

  if (dds_domain_id < 0) {
    ROS_FATAL("g1_ros1_sdk: dds_domain_id must be non-negative");
    return 2;
  }
  if (auto_start && !enable_motion) {
    ROS_FATAL("g1_ros1_sdk: auto_start=true requires enable_motion=true");
    return 2;
  }

  ROS_INFO("g1_ros1_sdk: DDS interface=%s domain=%d",
           network_interface.c_str(), dds_domain_id);
  ROS_INFO("g1_ros1_sdk: enable_motion=%s auto_start=%s",
           enable_motion ? "true" : "false",
           auto_start ? "true" : "false");

  unitree::robot::ChannelFactory::Instance()->Init(dds_domain_id,
                                                    network_interface);

  g_imu_pub = nh.advertise<sensor_msgs::Imu>("/sdk/imu", 50);
  g_status_pub = nh.advertise<std_msgs::String>("/sdk/bridge_status", 1,
                                                 true);

  unitree::robot::ChannelSubscriberPtr<unitree_hg::msg::dds_::LowState_>
      lowstate_sub;
  lowstate_sub.reset(
      new unitree::robot::ChannelSubscriber<
          unitree_hg::msg::dds_::LowState_>("rt/lowstate"));
  lowstate_sub->InitChannel(LowStateHandler, 1);

  ros::Subscriber cmd_sub;
  if (!enable_motion) {
    ROS_WARN("g1_ros1_sdk: SAFE MODE active; no LocoClient and no /cmd_vel subscriber");
    PublishStatus("read_only_waiting_for_dds_lowstate");
  } else {
    ROS_ERROR("g1_ros1_sdk: MOTION MODE explicitly enabled; /cmd_vel is active");
    g_loco = std::make_unique<unitree::robot::g1::LocoClient>();
    g_loco->Init();
    g_loco->SetTimeout(10.0f);
    g_loco->SwitchMoveMode(true);
    cmd_sub = nh.subscribe<geometry_msgs::Twist>("/cmd_vel", 100,
                                                 CmdVelCallback);
    PublishStatus(auto_start ? "motion_enabled_auto_start_pending"
                             : "motion_enabled_auto_start_off");

    if (auto_start) {
      ROS_ERROR("g1_ros1_sdk: AUTO_START explicitly enabled: calling StandUp then Start");
      g_loco->StandUp();
      std::this_thread::sleep_for(
          std::chrono::duration<double>(auto_start_delay_sec));
      g_loco->Start();
      PublishStatus("motion_enabled_auto_start_complete");
    }
  }

  ros::spin();

  // Deliberately do not issue StopMove, Damp, or any other actuator command
  // during shutdown. Container stop/restart must not mutate robot state.
  PublishStatus("stopping_no_actuator_command_sent");
  g_loco.reset();
  return 0;
}

