# 任务语音服务开机自启

适用现场：导航宿主机 `operator@192.0.2.5`，机器人 `operator@192.0.2.18`，导航仍使用既有有线方案。该自启仅管理音频后端，不启动导航、不重启机器人桥接、不改网络或运动模式。

## 一次性安装

安装包已暂存到导航容器：`/tmp/g1-taREDACTED_CREDENTIAL.zip`。安装器为 `/tmp/g1-taREDACTED_CREDENTIAL.sh`。

先在手动语音服务终端按 Ctrl+C，再在导航宿主机执行：

```bash
docker cp nav_robot:/tmp/g1-taREDACTED_CREDENTIAL.sh /tmp/g1-taREDACTED_CREDENTIAL.sh && bash /tmp/g1-taREDACTED_CREDENTIAL.sh
```

现场输入一次机器人 SSH 密码和宿主机 sudo 密码。程序不保存密码。安装器拒绝覆盖占用中的 18768 端口；不会杀导航或其他进程。

安装器保留已有配置 `/home/operator/g1-arrival-audio-test.ZxVYYQe5/hardware/config.json` 和相邻音频文件，不覆盖任务数据。运行代码安装在 `/home/operator/.local/share/g1-task-voice/releases/<UTC时间>/`。

## 认证与启动行为

宿主机生成独立 Ed25519 密钥，私钥仅保存于 `/home/operator/.local/share/g1-task-voice/identity/id_ed25519`，权限 600。机器人 `authorized_keys` 为此公钥配置 `restrict,command="..."`，只执行固定音频工作程序，禁止 shell、终端、端口/代理/X11 转发。工作程序只允许 eth0 的音量查询、TTS、经过格式检查的 WAV，拒绝其他操作。现有通用 SSH 登录不变，主机密钥校验保持 StrictHostKeyChecking=yes，使用先前已验证的 known_hosts 条目。

`g1-task-voice.service` 由系统 systemd 以 operator 身份运行，网络就绪后启动，失败每 10 秒重试。机器人晚开机无需人工重新登录；须继续能以当前地址 192.0.2.18 访问机器人。启动只调用 GetVolume；不提交播报、不启动/恢复导航任务。请求播放失败或结果未知时，不自动重播。任务模块原有持久去重记录继续使用，不重载任务后端。

服务只监听 `127.0.0.1:18768`；原任务编辑器通过现有任务 API 使用它，无需 Windows SSH 端口转发。独立 18768 编辑器远程浏览仍需要原有转发，这与原任务页无关。

## 检查与回退

```bash
systemctl is-enabled g1-task-voice.service
systemctl is-active g1-task-voice.service
journalctl -u g1-task-voice.service -n 40 --no-pager
```

任务页目录接口 `http://192.0.2.5:9000/api/task/voice_catalog` 应返回 200 和 `live:true`。仅表示语音服务已连接，不代表已完成现场听音。

停用自启：`sudo systemctl disable --now g1-task-voice.service`。需要恢复手动模式时，再运行既有 `g1-taREDACTED_CREDENTIAL.sh`。机器人授权备份位于 `/home/operator/.local/share/g1-task-voice/authorized_keys-*.backup`；撤销密钥时仅删除该专用公钥行，切勿整份覆盖当前 authorized_keys。此前 systemd 单元如存在，会备份到宿主机 `~/.local/share/g1-task-voice/backups/`。

## 本次验证与状态

自启受限入口与现有 SSH 传输的 12 项离线测试通过；容器 Python 3.8 语法检查和 Bash 语法检查通过。安装包基于现场已验证的原音频包制作，保留既有 SDK 接入方式。

现场已完成一次性安装，终端输出 `restricted_audio_key_installed:true`、`voice_service_ready:true`、`enabled`、`active`。工作程序 SHA256 为 `8c4dd29690cab06c1c45576c7b7c5eada3a52bbeb9578d99a2b84bbb8c51cea7`，私钥未上传，密码未保存。随后只读核对任务语音目录接口返回 HTTP 200、`live:true`、6 条播报配置，18768 仅监听 `127.0.0.1`，任务后端两文件哈希保持原安装值。本次没有提交播音或导航请求。宿主机真实重启后的自启验收仍未进行，不能将 enabled/active 等同于已完成重启验证。

安装检查附带的 `/lib/systemd/system/snapd.service` 的 `RestartMode` 未识别提示来自系统已有 snapd 单元；不属于本模块服务文件，也未阻止本模块启动。

同日修复 preview.html 缓存链：预览页入口脚本增加内容版本，内嵌 rviz.html 增加任务版本。已在浏览器确认既有 J 步骤显示“语音播报”、文字输入框正常且服务恢复后无 HTTP 503。静态文件备份 `/opt/g1-arrival-voice/state/backups/preview-cache-20261010T072503Z`。访问 `http://192.0.2.5:8000/app/preview.html?g1-task=autostart-20261010`，可避开旧页面缓存。
