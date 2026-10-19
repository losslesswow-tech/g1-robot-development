// G1 手臂低级控制工具 — 通过 rt/arm_sdk 发布 LowCmd 控制 7DOF×2 手臂
// 用法: g1_arm <networkInterface> <pose> [timeout_s]
//   pose: info   打印当前手臂关节角度
//         forward 双臂向前平伸 (ShoulderPitch ±90°, 肘伸直)
//         side    双臂侧举 (arm7 示例姿态)
//         down    双臂自然下垂 (全部 0)
//         probe   肩 Pitch 小幅 +0.3 rad 试探方向
#include <unitree/idl/hg/LowCmd_.hpp>
#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <iostream>
#include <string>
#include <thread>
#include <chrono>
#include <cmath>
#include <cstring>
#include <atomic>

static const std::string kTopicArmSDK = "rt/arm_sdk";
static const std::string kTopicState = "rt/lowstate";
static const float kPi = 3.141592654f;

// G1 关节索引（35 关节）
enum {
  LSP = 15, LSR = 16, LSY = 17, LEL = 18, LWR = 19, LWP = 20, LWY = 21,
  RSP = 22, RSR = 23, RSY = 24, REL = 25, RWR = 26, RWP = 27, RWY = 28,
  kWeightIdx = 29
};
static const int kArmIdx[14] = {LSP, LSR, LSY, LEL, LWR, LWP, LWY,
                                RSP, RSR, RSY, REL, RWR, RWP, RWY};

static std::atomic<bool> g_got_state(false);
static unitree_hg::msg::dds_::LowState_ g_state;

static void StateHandler(const void* msg) {
  g_state = *(const unitree_hg::msg::dds_::LowState_*)msg;
  g_got_state = true;
}

static void PrintUsage() {
  std::cout << "用法: g1_arm <networkInterface> <pose> [timeout_s]" << std::endl;
  std::cout << "  info     打印当前手臂关节角度" << std::endl;
  std::cout << "  forward  双臂向前平伸 (ShoulderPitch +-90°, 肘伸直)" << std::endl;
  std::cout << "  side     双臂侧举" << std::endl;
  std::cout << "  down     双臂自然下垂 (全 0)" << std::endl;
  std::cout << "  probe    肩 Pitch 小幅 +0.3 rad 试探方向" << std::endl;
}

int main(int argc, char** argv) {
  if (argc < 3) { PrintUsage(); return 1; }
  std::string net_if = argv[1];
  std::string pose = argv[2];
  float timeout_s = (argc >= 4) ? std::stof(argv[3]) : 4.0f;

  unitree::robot::ChannelFactory::Instance()->Init(0, net_if);

  unitree::robot::ChannelPublisherPtr<unitree_hg::msg::dds_::LowCmd_> pub;
  pub.reset(new unitree::robot::ChannelPublisher<unitree_hg::msg::dds_::LowCmd_>(kTopicArmSDK));
  pub->InitChannel();

  unitree::robot::ChannelSubscriberPtr<unitree_hg::msg::dds_::LowState_> sub;
  sub.reset(new unitree::robot::ChannelSubscriber<unitree_hg::msg::dds_::LowState_>(kTopicState));
  sub->InitChannel(StateHandler, 1);

  // 等待 lowstate
  int waited = 0;
  while (!g_got_state && waited < 50) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    waited++;
  }
  if (!g_got_state) {
    std::cout << "未收到 rt/lowstate 状态!" << std::endl;
    return 1;
  }

  float cur[14], cur_all[35];
  for (int i = 0; i < 35; i++) cur_all[i] = g_state.motor_state()[i].q();
  for (int i = 0; i < 14; i++) cur[i] = cur_all[kArmIdx[i]];

  if (pose == "info") {
    const char* names[14] = {"LSP", "LSR", "LSY", "LEL", "LWR", "LWP", "LWY",
                             "RSP", "RSR", "RSY", "REL", "RWR", "RWP", "RWY"};
    std::cout << "当前手臂关节角度 (rad):" << std::endl;
    for (int i = 0; i < 14; i++) {
      std::cout << "  " << names[i] << " = " << cur[i] << std::endl;
    }
    return 0;
  }

  // 目标姿态（14 关节）
  float tgt[14];
  if (pose == "down") {
    for (int i = 0; i < 14; i++) tgt[i] = 0.f;
  } else if (pose == "forward") {
    // 双臂前伸: 肩 Pitch -90°(实测 + 为后摆), 其余 0
    tgt[0] = -kPi / 2; tgt[1] = 0; tgt[2] = 0; tgt[3] = 0;
    tgt[4] = 0; tgt[5] = 0; tgt[6] = 0;
    tgt[7] = -kPi / 2; tgt[8] = 0; tgt[9] = 0; tgt[10] = 0;
    tgt[11] = 0; tgt[12] = 0; tgt[13] = 0;
  } else if (pose == "side") {
    // 侧举 (arm7 示例): L roll+90 肘+90; R roll-90 肘+90
    tgt[0] = 0; tgt[1] = +kPi / 2; tgt[2] = 0; tgt[3] = +kPi / 2;
    tgt[4] = 0; tgt[5] = 0; tgt[6] = 0;
    tgt[7] = 0; tgt[8] = -kPi / 2; tgt[9] = 0; tgt[10] = +kPi / 2;
    tgt[11] = 0; tgt[12] = 0; tgt[13] = 0;
  } else if (pose == "probe") {
    // 肩 Pitch 小幅前摆试探
    for (int i = 0; i < 14; i++) tgt[i] = cur[i];
    tgt[0] = cur[0] - 0.3f;   // 左肩 pitch -0.3 (前摆方向)
    tgt[7] = cur[7] - 0.3f;   // 右肩 pitch -0.3 (前摆方向)
  } else {
    std::cout << "未知姿态: " << pose << std::endl;
    PrintUsage();
    return 1;
  }

  // 平滑插值到目标
  int steps = (int)(timeout_s / 0.02f);
  float kp = 60.f, kd = 1.5f;
  for (int s = 0; s <= steps; s++) {
    float phase = (float)s / steps;
    unitree_hg::msg::dds_::LowCmd_ msg;
    // weight: 用 kNotUsedJoint(29) 的 q 编码, 1.0 = 完全由 arm_sdk 接管
    msg.motor_cmd().at(kWeightIdx).q(1.0f);
    for (int i = 0; i < 14; i++) {
      auto& mc = msg.motor_cmd().at(kArmIdx[i]);
      mc.q(cur[i] + (tgt[i] - cur[i]) * phase);
      mc.dq(0.f);
      mc.kp(kp);
      mc.kd(kd);
      mc.tau(0.f);
    }
    pub->Write(msg);
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }

  // 保持目标 1 秒
  for (int s = 0; s < 50; s++) {
    unitree_hg::msg::dds_::LowCmd_ msg;
    msg.motor_cmd().at(kWeightIdx).q(1.0f);
    for (int i = 0; i < 14; i++) {
      auto& mc = msg.motor_cmd().at(kArmIdx[i]);
      mc.q(tgt[i]); mc.dq(0.f); mc.kp(kp); mc.kd(kd); mc.tau(0.f);
    }
    pub->Write(msg);
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }

  std::cout << "g1_arm: " << pose << " 完成 (保持 " << timeout_s << "s 插值 + 1s 保持)" << std::endl;
  return 0;
}
