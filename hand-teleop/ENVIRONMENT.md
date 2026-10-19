# 灵巧手背负主机环境

2026-10-14：依据“克隆机器人开发容器环境”的接管记录适配。当前主机 `operator@192.0.2.1`，
有线网卡 `eno1 / 192.0.2.2`，容器 `robot-navigation-example`。
宿主机 Ubuntu22.04 / Python3.10；导航容器 Ubuntu20.04 / Python3.8 / x86_64，host网络。

- 手动控制：http://192.0.2.1:8769/
- 只读模型：http://192.0.2.1:8768/
- 持久源码：宿主机 `/home/operator/g1-migration-20261013/cutover/development/hand-teleop`；
  容器只读挂载 `/opt/g1-development/hand-teleop`。
- 服务端凭据位于相邻私有 `connection.token`，不放入配置、代码、URL或报告。
  新地址属于不同浏览器来源，需要首次认证；已认证后自动连接读取。

左右手各六路组合控制，前五路0–1000、拇指旋转950–1000；先准备再编辑并手动发送。
设备地址仍为左192.0.2.3、右192.0.2.4、端口6000，使用原仓库 ModbusTCP驱动，速度300。
目标寄存器与实测反馈分开，不能用反馈替换保持目标，也不能自动忽略“目标被改变”的保护。
更换运行环境不意味着实物角度标定通过，不自动执行动作或更新运动基准。

在工作区使用以下命令，身份文件采用迁移对话建立的SSH密钥路径：

```powershell
python hand-teleop/environment.py check --identity "$env:TEMP/g1-migration-20261013/id_ed25519"
python hand-teleop/environment.py offline-test --identity "$env:TEMP/g1-migration-20261013/id_ed25519"
python hand-teleop/environment.py install-adapter --identity "$env:TEMP/g1-migration-20261013/id_ed25519"
```

`check`仅核对环境并读取手部状态/目标；`offline-test`只运行假设备测试；
`install-adapter`只安装这份说明、环境配置和检查工具，保留备份，不改活动控制程序、计划或认证，
不重启容器或服务，不注册新的写入实例。SSH必须校验已有主机身份，不能关闭严格校验。

手部服务已由迁移对话的 `launch-navigation.sh` 随导航容器启动，容器为 `unless-stopped`。
没有独立的手部失败重试守护；不新增自动动作或重复启动服务。
`tools` 下日期为20261012/20261013、固定192.0.2.5或旧临时目录的安装/动作脚本是历史记录，
不能用于新机部署或动作。后续检查以本适配入口及本环境配置为准。
