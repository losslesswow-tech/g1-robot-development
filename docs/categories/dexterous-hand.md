# 灵巧手

主模块：[hand-teleop](../../hand-teleop/README.md)。

- `web/`：三维手指编辑、通道映射、数值输入、离线姿态导入导出。
- `server.py`：本机模拟、只读连接、显式开启的设备写入入口。
- `step_server.py`：逐次发送、范围与通道约束。
- `task_action.py`、`navigation_patch.py`：导航任务中的右手食指动作适配。
- `calibration.example.json`、手动配置：示例映射及软件输入范围，不代表实机标定。
- `tests/`：假设备与离线界面逻辑测试。

相关维护脚本位于根目录 `tools/`。部署地址与身份均为占位示例。
