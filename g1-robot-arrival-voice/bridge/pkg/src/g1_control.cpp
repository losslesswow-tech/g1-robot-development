// G1 control CLI — 一次性控制命令（完整 LocoClient 接口 + G1 手臂动作）
#include <iostream>
#include <string>
#include <thread>
#include <chrono>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <unitree/robot/g1/arm/g1_arm_action_client.hpp>
#include <unitree/idl/hg/HandCmd_.hpp>
#include <unitree/idl/hg/HandState_.hpp>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <cstring>
#include <cmath>

// ============ Dex3 灵巧手 (unitree DexHand-3) ============
#define DEX3_MOTOR_MAX 7
#define DEX3_SENSOR_MAX 9

// 左右手关节限位（来自官方 g1_dex3_example.cpp）
static const float dex3_max_left[7]  = { 1.05,  1.05,  1.75,  0.0,   0.0,   0.0,   0.0  };
static const float dex3_min_left[7]  = {-1.05, -0.724, 0.0,  -1.57, -1.75, -1.57, -1.75 };
static const float dex3_max_right[7] = { 1.05,  0.742, 0.0,   1.57,  1.75,  1.57,  1.75 };
static const float dex3_min_right[7] = {-1.05, -1.05, -1.75,  0.0,   0.0,   0.0,   0.0  };

static uint8_t Dex3EncodeMode(int motor_id, uint8_t status, bool timeout) {
  uint8_t mode = 0;
  mode |= (motor_id & 0x0F);
  mode |= (status & 0x07) << 4;
  if (timeout) mode |= 0x80;
  return mode;
}

static unitree_hg::msg::dds_::HandCmd_ MakeHandCmd(const float* q, int motor_count) {
  unitree_hg::msg::dds_::HandCmd_ msg;
  msg.motor_cmd().resize(DEX3_MOTOR_MAX);
  for (int i = 0; i < DEX3_MOTOR_MAX; i++) {
    auto& mc = msg.motor_cmd()[i];
    mc.mode(Dex3EncodeMode(i, 0x01, false));
    mc.tau(0.0f);
    mc.dq(0.0f);
    mc.kp(1.5f);
    mc.kd(0.1f);
    mc.q(i < motor_count ? q[i] : 0.0f);
  }
  return msg;
}

static unitree_hg::msg::dds_::HandCmd_ MakeHandStopCmd() {
  unitree_hg::msg::dds_::HandCmd_ msg;
  msg.motor_cmd().resize(DEX3_MOTOR_MAX);
  for (int i = 0; i < DEX3_MOTOR_MAX; i++) {
    auto& mc = msg.motor_cmd()[i];
    mc.mode(Dex3EncodeMode(i, 0x01, true));  // timeout=1 停止
    mc.tau(0.0f); mc.dq(0.0f); mc.kp(0.0f); mc.kd(0.0f); mc.q(0.0f);
  }
  return msg;
}

// 灵巧手状态快照（回调直接提取标量，避免拷贝 DDS 类对象）
struct Dex3StateSnap {
  float q[DEX3_MOTOR_MAX] = {0}, dq[DEX3_MOTOR_MAX] = {0}, tau[DEX3_MOTOR_MAX] = {0};
  float press[DEX3_SENSOR_MAX][12] = {{0}};
  float power_v = 0, power_a = 0;
  int motor_count = 0, sensor_count = 0;
};
static Dex3StateSnap g_snap;
static bool g_hand_state_received = false;
static void HandStateHandler(const void* message) {
  const auto* st = (const unitree_hg::msg::dds_::HandState_*)message;
  const auto& mstate = st->motor_state();
  for (size_t i = 0; i < mstate.size() && i < DEX3_MOTOR_MAX; i++) {
    g_snap.q[i] = mstate[i].q();
    g_snap.dq[i] = mstate[i].dq();
    g_snap.tau[i] = mstate[i].tau_est();
  }
  const auto& pstate = st->press_sensor_state();
  for (size_t i = 0; i < pstate.size() && i < DEX3_SENSOR_MAX; i++) {
    const auto& p = pstate[i].pressure();
    for (size_t j = 0; j < p.size() && j < 12; j++) g_snap.press[i][j] = p[j];
  }
  g_snap.motor_count = (int)mstate.size();
  g_snap.sensor_count = (int)pstate.size();
  g_snap.power_v = st->power_v();
  g_snap.power_a = st->power_a();
  g_hand_state_received = true;
}

// 0=left 1=right
static std::string Dex3CmdTopic(int side)  { return side == 0 ? "rt/dex3/left/cmd"  : "rt/dex3/right/cmd"; }
static std::string Dex3StateTopic(int side) { return side == 0 ? "rt/lf/dex3/left/state" : "rt/lf/dex3/right/state"; }
static const float* Dex3Max(int side) { return side == 0 ? dex3_max_left : dex3_max_right; }
static const float* Dex3Min(int side) { return side == 0 ? dex3_min_left : dex3_min_right; }

// 归一化百分比 [0,1] -> 实际关节角
static void Dex3FillPos(float* out, int side, float pct, int count) {
  const float* mx = Dex3Max(side);
  const float* mn = Dex3Min(side);
  for (int i = 0; i < count; i++) out[i] = mn[i] + pct * (mx[i] - mn[i]);
}

static void PrintUsage() {
  std::cerr << "用法: g1_control <networkInterface> <command> [args...]" << std::endl;
  std::cerr << std::endl;
  std::cerr << "状态机命令:" << std::endl;
  std::cerr << "  stand          StandUp       站立 (fsm 4)" << std::endl;
  std::cerr << "  damp           Damp          阻尼卸力 (fsm 1)" << std::endl;
  std::cerr << "  squat          Squat         蹲下 (fsm 2)" << std::endl;
  std::cerr << "  sit            Sit           坐下 (fsm 3)" << std::endl;
  std::cerr << "  zerotorque     ZeroTorque    零力矩 (fsm 0)" << std::endl;
  std::cerr << "  start          Start         进入行走模式 (fsm 500)" << std::endl;
  std::cerr << "  setfsm <id>    SetFsmId      直接切换 FSM (0/1/2/3/4/500)" << std::endl;
  std::cerr << std::endl;
  std::cerr << "运动命令:" << std::endl;
  std::cerr << "  move <vx> <vy> <vyaw> [continuous]   Move 速度控制 (m/s, rad/s)" << std::endl;
  std::cerr << "  stop           StopMove      停止移动" << std::endl;
  std::cerr << "  balance        BalanceStand  平衡站立" << std::endl;
  std::cerr << "  continuous <0|1>  ContinuousGait  连续/单步步态切换" << std::endl;
  std::cerr << "  switchmove <0|1>  SwitchMoveMode  移动模式开关" << std::endl;
  std::cerr << "  speed <n>      SetSpeedMode  速度档位" << std::endl;
  std::cerr << std::endl;
  std::cerr << "任务命令:" << std::endl;
  std::cerr << "  wave [mode]    ExecuteAction  挥手 (0=头下25 1=转身1 2=头上26, 走 arm action 服务)" << std::endl;
  std::cerr << "  shake [stage]  ShakeHand     握手 (task 2/3, -1 自动)" << std::endl;
  std::cerr << "  task <id>      SetTaskId     设置任务 id" << std::endl;
  std::cerr << std::endl;
  std::cerr << "手臂动作 (G1ArmActionClient, 推荐):" << std::endl;
  std::cerr << "  action <name|id>  执行预设动作 (如 face-wave/high-wave/shake-hand/clap...)" << std::endl;
  std::cerr << "  actions            列出全部动作" << std::endl;
  std::cerr << "  release            释放手臂 (id 99, 动作结束保持位姿时用于复位)" << std::endl;
  std::cerr << std::endl;
  std::cerr << "灵巧手 (Dex3, rt/dex3/<left|right>/cmd):" << std::endl;
  std::cerr << "  handstate <L|R>   订阅灵巧手状态 3s 并打印 (验证在线)" << std::endl;
  std::cerr << "  hand <L|R> grip [pct]    全部手指到中间/指定百分比位置" << std::endl;
  std::cerr << "  hand <L|R> open         手指张开 (0% 位置)" << std::endl;
  std::cerr << "  hand <L|R> close        手指闭合 (100% 位置)" << std::endl;
  std::cerr << "  hand <L|R> pos <p1..p7> 逐指位置 0-1 (7 个值)" << std::endl;
  std::cerr << "  hand <L|R> test         正弦摆动测试" << std::endl;
  std::cerr << "  hand <L|R> stop         安全停止 (timeout)" << std::endl;
  std::cerr << std::endl;
  std::cerr << "查询:" << std::endl;
  std::cerr << "  query          查询 FSM 状态" << std::endl;
}

int main(int argc, char** argv) {
  if (argc < 3) {
    PrintUsage();
    return 1;
  }

  std::string net_if = argv[1];
  std::string cmd = argv[2];

  unitree::robot::ChannelFactory::Instance()->Init(0, net_if);

  unitree::robot::g1::LocoClient client;
  client.Init();
  client.SetTimeout(10.0f);

  if (cmd == "stand") {
    std::cout << "G1: StandUp (站立)" << std::endl;
    client.StandUp();
  } else if (cmd == "damp") {
    std::cout << "G1: Damp (阻尼卸力)" << std::endl;
    client.Damp();
  } else if (cmd == "squat") {
    std::cout << "G1: Squat (蹲下)" << std::endl;
    client.Squat();
  } else if (cmd == "sit") {
    std::cout << "G1: Sit (坐下)" << std::endl;
    client.Sit();
  } else if (cmd == "zerotorque") {
    std::cout << "G1: ZeroTorque (零力矩)" << std::endl;
    client.ZeroTorque();
  } else if (cmd == "start") {
    std::cout << "G1: Start (进入行走模式)" << std::endl;
    client.Start();
  } else if (cmd == "setfsm") {
    if (argc < 4) { std::cerr << "setfsm 需要 <id> (0/1/2/3/4/500)" << std::endl; return 1; }
    int id = std::stoi(argv[3]);
    std::cout << "G1: SetFsmId(" << id << ")" << std::endl;
    client.SetFsmId(id);
  } else if (cmd == "stop") {
    std::cout << "G1: StopMove (停止移动)" << std::endl;
    client.StopMove();
  } else if (cmd == "balance") {
    std::cout << "G1: BalanceStand (平衡站立)" << std::endl;
    client.BalanceStand();
  } else if (cmd == "move") {
    if (argc < 6) { std::cerr << "move 需要 <vx> <vy> <vyaw>" << std::endl; return 1; }
    float vx = std::stof(argv[3]);
    float vy = std::stof(argv[4]);
    float vyaw = std::stof(argv[5]);
    bool continuous = (argc >= 7) && (std::stoi(argv[6]) != 0);
    std::cout << "G1: Move(" << vx << ", " << vy << ", " << vyaw
              << ", continuous=" << (continuous ? "true" : "false") << ")" << std::endl;
    client.Move(vx, vy, vyaw, continuous);
  } else if (cmd == "continuous") {
    if (argc < 4) { std::cerr << "continuous 需要 <0|1>" << std::endl; return 1; }
    bool flag = std::stoi(argv[3]) != 0;
    std::cout << "G1: ContinuousGait(" << (flag ? "true" : "false") << ")" << std::endl;
    client.ContinuousGait(flag);
  } else if (cmd == "switchmove") {
    if (argc < 4) { std::cerr << "switchmove 需要 <0|1>" << std::endl; return 1; }
    bool flag = std::stoi(argv[3]) != 0;
    std::cout << "G1: SwitchMoveMode(" << (flag ? "true" : "false") << ")" << std::endl;
    client.SwitchMoveMode(flag);
  } else if (cmd == "speed") {
    if (argc < 4) { std::cerr << "speed 需要 <n>" << std::endl; return 1; }
    int n = std::stoi(argv[3]);
    std::cout << "G1: SetSpeedMode(" << n << ")" << std::endl;
    client.SetSpeedMode(n);
  } else if (cmd == "wave") {
    // 走 arm action 服务（LocoClient 的 WaveHand/SetTaskId 在部分固件上无效）
    int mode = (argc >= 4) ? std::stoi(argv[3]) : 0;
    unitree::robot::g1::G1ArmActionClient arm;
    arm.Init();
    arm.SetTimeout(10.0f);
    int aid = (mode == 1) ? 1 : (mode == 2 ? 26 : 25); // 0=头下挥手25 1=转身挥手1 2=头上挥手26
    std::cout << "G1: ExecuteAction(wave id=" << aid << ")" << std::endl;
    int ret = arm.ExecuteAction(aid);
    std::cout << "Action call ret = " << ret << " (0=成功)" << std::endl;
  } else if (cmd == "shake") {
    int stage = (argc >= 4) ? std::stoi(argv[3]) : -1;
    std::cout << "G1: ShakeHand(stage=" << stage << ")" << std::endl;
    client.ShakeHand(stage);
  } else if (cmd == "task") {
    if (argc < 4) { std::cerr << "task 需要 <id>" << std::endl; return 1; }
    int id = std::stoi(argv[3]);
    std::cout << "G1: SetTaskId(" << id << ")" << std::endl;
    client.SetTaskId(id);
  } else if (cmd == "action") {
    if (argc < 4) {
      std::cerr << "action 需要 <name|id>" << std::endl;
      unitree::robot::g1::G1ArmActionClient arm;
      arm.Init();
      for (const auto& kv : arm.action_map) {
        std::cerr << "  " << kv.first << " = " << kv.second << std::endl;
      }
      return 1;
    }
    unitree::robot::g1::G1ArmActionClient arm;
    arm.Init();
    arm.SetTimeout(10.0f);
    arm.SetTimeout(10.0f);
    std::string arg = argv[3];
    int ret = -1;
    // 数字 id 直接执行
    bool is_num = !arg.empty() && arg.find_first_not_of("0123456789") == std::string::npos;
    if (is_num) {
      int id = std::stoi(arg);
      std::cout << "G1: ExecuteAction(id=" << id << ")" << std::endl;
      ret = arm.ExecuteAction(id);
    } else {
      // 按动作名查表（face-wave -> face wave）
      auto it = arm.action_map.find(arg);
      if (it != arm.action_map.end()) {
        std::cout << "G1: ExecuteAction(" << it->first << " = id " << it->second << ")" << std::endl;
        ret = arm.ExecuteAction(it->second);
      } else {
        std::cout << "G1: ExecuteCustomAction(name=\"" << arg << "\")" << std::endl;
        ret = arm.ExecuteAction(arg);
      }
    }
    std::cout << "Action call ret = " << ret << " (0=成功)" << std::endl;
  } else if (cmd == "actions") {
    unitree::robot::g1::G1ArmActionClient arm;
    arm.Init();
    arm.SetTimeout(10.0f);
    std::cout << "G1 手臂动作列表:" << std::endl;
    for (const auto& kv : arm.action_map) {
      std::cout << "  " << kv.first << " = " << kv.second << std::endl;
    }
  } else if (cmd == "actionlist") {
    unitree::robot::g1::G1ArmActionClient arm;
    arm.Init();
    arm.SetTimeout(10.0f);
    std::string list_data;
    int ret = arm.GetActionList(list_data);
    std::cout << "GetActionList ret = " << ret << " (0=成功)" << std::endl;
    if (ret == 0) std::cout << "动作列表: " << list_data << std::endl;
  } else if (cmd == "release") {
    unitree::robot::g1::G1ArmActionClient arm;
    arm.Init();
    arm.SetTimeout(10.0f);
    std::cout << "G1: ExecuteAction(release arm = id 99)" << std::endl;
    int ret = arm.ExecuteAction(99);
    std::cout << "Action call ret = " << ret << " (0=成功)" << std::endl;
  } else if (cmd == "handstate") {
    if (argc < 4) { std::cerr << "handstate 需要 <L|R>" << std::endl; return 1; }
    int side = (argv[3][0] == 'L' || argv[3][0] == 'l') ? 0 : 1;
    // 候选 topic（官方示例 rt/lf/...，兼容无 lf 前缀变体）
    std::string cands[4];
    if (side == 0) {
      cands[0] = "rt/lf/dex3/left/state";
      cands[1] = "rt/dex3/left/state";
      cands[2] = "rt/dex3/left/status";
      cands[3] = "rt/dex3/left";
    } else {
      cands[0] = "rt/lf/dex3/right/state";
      cands[1] = "rt/dex3/right/state";
      cands[2] = "rt/dex3/right/status";
      cands[3] = "rt/dex3/right";
    }
    bool found = false;
    for (int t = 0; t < 4 && !found; t++) {
      unitree::robot::ChannelSubscriberPtr<unitree_hg::msg::dds_::HandState_> sub;
      sub.reset(new unitree::robot::ChannelSubscriber<unitree_hg::msg::dds_::HandState_>(cands[t]));
      g_hand_state_received = false;
      sub->InitChannel(HandStateHandler, 1);
      std::cout << "尝试订阅 " << cands[t] << " (2s)..." << std::endl;
      int waited = 0;
      while (!g_hand_state_received && waited < 20) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        waited++;
      }
      if (g_hand_state_received) { found = true; break; }
    }
    if (!found) {
      std::cout << "未收到灵巧手状态（已尝试 4 个候选 topic）！请确认: 灵巧手上电、已连接机器人 (MCU)、DDS 网络正常。" << std::endl;
      return 1;
    }
    std::cout << "灵巧手 " << (side == 0 ? "左(L)" : "右(R)") << " 在线!" << std::endl;
    std::cout << "  电机数: " << g_snap.motor_count << std::endl;
    for (int i = 0; i < g_snap.motor_count && i < DEX3_MOTOR_MAX; i++) {
      std::cout << "  motor[" << i << "] q=" << g_snap.q[i] << " dq=" << g_snap.dq[i]
                << " tau=" << g_snap.tau[i] << std::endl;
    }
    std::cout << "  压力传感器数: " << g_snap.sensor_count << " (每传感器 12 通道)" << std::endl;
    for (int i = 0; i < g_snap.sensor_count && i < DEX3_SENSOR_MAX; i++) {
      std::cout << "  press[" << i << "]=[";
      for (int j = 0; j < 12; j++) std::cout << g_snap.press[i][j] << (j < 11 ? " " : "");
      std::cout << "]" << std::endl;
    }
    std::cout << "  电源: V=" << g_snap.power_v << " A=" << g_snap.power_a << std::endl;
  } else if (cmd == "hand") {
    if (argc < 5) {
      std::cerr << "用法: hand <L|R> <grip|open|close|pos|test|stop> [args]" << std::endl;
      return 1;
    }
    int side = (argv[3][0] == 'L' || argv[3][0] == 'l') ? 0 : 1;
    std::string hcmd = argv[4];
    std::string topic = Dex3CmdTopic(side);
    unitree::robot::ChannelPublisherPtr<unitree_hg::msg::dds_::HandCmd_> pub;
    pub.reset(new unitree::robot::ChannelPublisher<unitree_hg::msg::dds_::HandCmd_>(topic));
    pub->InitChannel();
    std::cout << "发布到 " << topic << std::endl;
    if (hcmd == "stop") {
      auto msg = MakeHandStopCmd();
      pub->Write(msg);
      std::cout << "G1: Dex3 stop (timeout)" << std::endl;
    } else if (hcmd == "open") {
      float q[7]; Dex3FillPos(q, side, 0.0f, 7);
      auto msg = MakeHandCmd(q, 7);
      pub->Write(msg);
      std::cout << "G1: Dex3 open (0%)" << std::endl;
    } else if (hcmd == "close") {
      float q[7]; Dex3FillPos(q, side, 1.0f, 7);
      auto msg = MakeHandCmd(q, 7);
      pub->Write(msg);
      std::cout << "G1: Dex3 close (100%)" << std::endl;
    } else if (hcmd == "grip") {
      float pct = (argc >= 6) ? std::stof(argv[5]) : 0.5f;
      float q[7]; Dex3FillPos(q, side, std::clamp(pct, 0.0f, 1.0f), 7);
      auto msg = MakeHandCmd(q, 7);
      pub->Write(msg);
      std::cout << "G1: Dex3 grip (" << pct * 100.0f << "%)" << std::endl;
    } else if (hcmd == "pos") {
      if (argc < 12) { std::cerr << "pos 需要 7 个 0-1 值" << std::endl; return 1; }
      float pct[7];
      for (int i = 0; i < 7; i++) pct[i] = std::clamp(std::stof(argv[5 + i]), 0.0f, 1.0f);
      float q[7]; Dex3FillPos(q, side, 0.0f, 7);
      const float* mx = Dex3Max(side);
      const float* mn = Dex3Min(side);
      for (int i = 0; i < 7; i++) q[i] = mn[i] + pct[i] * (mx[i] - mn[i]);
      auto msg = MakeHandCmd(q, 7);
      pub->Write(msg);
      std::cout << "G1: Dex3 pos [";
      for (int i = 0; i < 7; i++) std::cout << pct[i] << (i < 6 ? " " : "");
      std::cout << "]" << std::endl;
    } else if (hcmd == "test") {
      std::cout << "G1: Dex3 正弦摆动测试 (2s)..." << std::endl;
      for (int c = 0; c < 100; c++) {
        float q[7];
        for (int i = 0; i < 7; i++) {
          float range = Dex3Max(side)[i] - Dex3Min(side)[i];
          q[i] = Dex3Min(side)[i] + range * (0.5f + 0.5f * std::sin(c / 20.0f * 3.14159f));
        }
        auto msg = MakeHandCmd(q, 7);
        pub->Write(msg);
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
      }
      auto stop = MakeHandStopCmd();
      pub->Write(stop);
      std::cout << "G1: Dex3 test done, stop sent" << std::endl;
    } else {
      std::cerr << "未知灵巧手命令: " << hcmd << std::endl;
      return 1;
    }
  } else if (cmd == "query") {
    int fsm_id = -1, fsm_mode = -1;
    client.GetFsmId(fsm_id);
    client.GetFsmMode(fsm_mode);
    std::cout << "G1 FSM: fsm_id=" << fsm_id << " fsm_mode=" << fsm_mode << std::endl;
    if (fsm_id == 0) std::cout << "状态: 零力矩(ZeroTorque)" << std::endl;
    else if (fsm_id == 1) std::cout << "状态: 阻尼(Damp)" << std::endl;
    else if (fsm_id == 2) std::cout << "状态: 蹲下(Squat)" << std::endl;
    else if (fsm_id == 3) std::cout << "状态: 坐(Sit)" << std::endl;
    else if (fsm_id == 4) std::cout << "状态: 站立(StandUp)" << std::endl;
    else if (fsm_id == 500) std::cout << "状态: 已启动(Start/行走)" << std::endl;
    else std::cout << "状态: fsm_id=" << fsm_id << std::endl;
  } else if (cmd == "help" || cmd == "-h" || cmd == "--help") {
    PrintUsage();
    return 0;
  } else {
    std::cerr << "未知命令: " << cmd << std::endl;
    PrintUsage();
    return 1;
  }

  // 等待命令执行完成
  std::this_thread::sleep_for(std::chrono::seconds(1));
  std::cout << "G1 control done." << std::endl;
  return 0;
}
