// G1 ROS1 SDK — bridge Unitree G1 LocoClient (DDS) to ROS1 topics.
// Subscribes /cmd_vel (geometry_msgs/Twist) -> LocoClient.Move()
// Publishes /sdk/imu (sensor_msgs/Imu) from LowState IMU
// Auto StandUp + Start on boot.

#include <ros/ros.h>
#include <sensor_msgs/Imu.h>
#include <geometry_msgs/Twist.h>
#include <std_msgs/String.h>
#include <nav_msgs/Odometry.h>

#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <unitree/dds_wrapper/robots/g1/g1_sub.h>

#include <thread>
#include <chrono>
#include <atomic>
#include <mutex>

unitree::robot::g1::LocoClient* g_loco = nullptr;
ros::Publisher g_imu_pub;
ros::Publisher g_vel_pub;

std::atomic<bool> g_standing(false);

// LowState 回调：发布 IMU
void LowStateHandler(const void* msg)
{
  const unitree_hg::msg::dds_::LowState_* low = static_cast<const unitree_hg::msg::dds_::LowState_*>(msg);
  if (!low) return;

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
}

// /cmd_vel 回调：linear.z 作为命令字段（0=移动,1=平衡站立,2=卧倒卸力）
void CmdVelCallback(const geometry_msgs::Twist::ConstPtr& msg)
{
  if (!g_loco) return;

  int cmd = static_cast<int>(msg->linear.z);

  if (cmd == 1) {
    g_loco->BalanceStand();
    g_standing = true;
    return;
  } else if (cmd == 2) {
    g_loco->Damp();
    g_standing = false;
    return;
  }

  // cmd == 0：速度控制
  g_loco->Move(msg->linear.x, msg->linear.y, msg->angular.z);
}

int main(int argc, char** argv)
{
  ros::init(argc, argv, "g1_ros1_sdk_node");
  ros::NodeHandle nh;

  std::string net_if;
  ros::NodeHandle pnh("~");
  pnh.param<std::string>("networkInterface", net_if, "enp4s0");
  ROS_INFO("g1_ros1_sdk: network interface = %s", net_if.c_str());

  // 初始化 DDS channel（domain 0）
  unitree::robot::ChannelFactory::Instance()->Init(0, net_if);

  // LocoClient 初始化
  unitree::robot::g1::LocoClient client;
  client.Init();
  client.SetTimeout(10.0f);
  client.SwitchMoveMode(true);  // continuous move mode
  g_loco = &client;

  // 订阅 LowState（IMU 反馈）
  unitree::robot::ChannelSubscriberPtr<unitree_hg::msg::dds_::LowState_> lowstate_sub;
  lowstate_sub.reset(new unitree::robot::ChannelSubscriber<unitree_hg::msg::dds_::LowState_>("rt/lowstate"));
  lowstate_sub->InitChannel(LowStateHandler, 1);

  // ROS 发布者
  g_imu_pub = nh.advertise<sensor_msgs::Imu>("/sdk/imu", 50);
  g_vel_pub = nh.advertise<geometry_msgs::Twist>("/robot/dog_vel", 50);

  // ROS 订阅者
  ros::Subscriber cmd_sub = nh.subscribe<geometry_msgs::Twist>("/cmd_vel", 100, CmdVelCallback);

  // 自动站立：先 StandUp，再 Start（进入运动状态）
  ROS_INFO("g1_ros1_sdk: standing up...");
  client.StandUp();
  std::this_thread::sleep_for(std::chrono::seconds(3));
  client.Start();
  g_standing = true;
  ROS_INFO("g1_ros1_sdk: ready, waiting for /cmd_vel");

  ros::Rate rate(100);
  while (ros::ok()) {
    ros::spinOnce();
    rate.sleep();
  }

  // 退出前卸力
  client.Damp();
  return 0;
}
