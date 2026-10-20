# Quest Teleop

用 Quest 3 裸手遥操 Thor 和 Wuji Hand 2，支持左侧、右侧或双侧，分别选择控制臂、手或臂手一起。默认右臂 + 右手。无需安装 Harness。

## 1. 安装

需要 Python 3.10+ 和头显上的 [Hand Tracking Streamer](https://github.com/wengmister/hand-tracking-streamer)。USB 连接需要 ADB。下面的命令均从本仓库目录执行；Windows 和 Linux 选择对应步骤。

### Windows + WSL

在 PowerShell 安装电脑端接收和预览环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path config.local.json)) { Copy-Item config.example.json config.local.json }
```

安装 Android Platform Tools，并确保 `adb devices` 能运行。手部运行环境使用 Ubuntu 22.04 WSL。在 WSL 中进入同一仓库目录，执行：

```bash
sudo apt-get install python3-venv
python3 -m venv ~/.venv-quest-teleop
~/.venv-quest-teleop/bin/python -m pip install -e '.[hand]'
```

Windows 与 WSL 通过 localhost 通信。需要支持镜像网络的 Windows 11 / WSL 2；在 `%USERPROFILE%\.wslconfig` 中合并以下配置，保存后运行 `wsl --shutdown`，再重新打开 WSL：

```ini
[wsl2]
networkingMode=mirrored
```

使用 [usbipd-win](https://github.com/dorssel/usbipd-win) 将 **Wuji 手的 USB 设备**接入 WSL；Quest 留在 Windows，由 Windows ADB 接收。

### 原生 Linux

电脑端接收、手腕映射和手部跟随可使用同一个 Python 环境，无需 WSL。机器人端只需运行 ROS 目标转发程序：

```bash
sudo apt-get install python3-venv adb
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[hand]'
test -f config.local.json || cp config.example.json config.local.json
```

Wuji 手直接接入 Linux，按 [Wuji SDK](https://pypi.org/project/wuji-sdk/) 要求配置 USB 访问权限。仅用机械臂时，可用 `pip install -e .`，不安装 `[hand]` 依赖。

## 2. 配置

编辑 `config.local.json`：

| 字段 | 填写内容 |
| --- | --- |
| `right_hand_serial` / `left_hand_serial` | 对应侧 Wuji 手的序列号；不用的一侧可留空字符串 |
| `hand_python` | 手部环境的 Python 路径；Windows 使用 WSL 内路径 |
| `wsl_distro` | Windows 上的 WSL 发行版名称；原生 Linux 忽略此项 |
| `scale` | 手腕位移倍率，默认 `1.0`；旋转保持 1:1 |
| `side` | `right`（默认）、`left` 或 `both`；同时选择臂和手的侧别 |
| `thor_host` | Thor 的 IP，默认 `6.6.8.100` |
| `thor_port` | Thor 目标接收端口，默认 `8002` |

Windows 按上述步骤安装时，`hand_python` 保持 `~/.venv-quest-teleop/bin/python`。原生 Linux 改为本仓库环境的绝对路径，例如 `/home/you/quest-teleop/.venv/bin/python`。

不要复制别人的 `.venv`、`logs` 或 `config.local.json`。旧配置请删除 `thor_url` 和 `wuji_url` 字段。

## 3. 准备设备

预览模式可跳过这一步。

### Thor：在机器人上运行目标转发

将仓库源码复制到机器人的独立目录，例如 `/home/user/quest-teleop`，保留原来的 Harness。目标转发程序直接使用已有厂商 Python / ROS 2 / IK 环境，从源码目录运行即可，无需在机器人上安装 Quest 和手部算法依赖。

机器人需已设置模式 `[1,1]`、input=1、IK source=2。程序不会切换模式或 VLA 输入，末端目标直接交给厂商 IK。默认 `ROS_DOMAIN_ID=24`，可通过环境变量修改。

先停止旧的 `quest_teleop.devices.thor` HTTP 服务、旧版 `arm_follow` 或其他发布机械臂目标的程序。本工作区原来的 Harness 服务名为 `tianji-thor-driver.service`；保留厂商 `apex-robot`、`apex-teleop` 等控制和 IK 服务。然后在机器人终端执行：

```bash
cd /home/user/quest-teleop
source /etc/apex/apex_ros_env.sh
python3 -u -m quest_teleop.arm_bridge
```

机器人默认监听 TCP `8002`，需允许电脑访问。电脑配置 `thor_host` 为机器人 IP；也可在电脑命令后加 `--thor-host <机器人IP>`。修改端口时，机器人加 `--port <端口>`，电脑加 `--thor-port <端口>`。

机器人端保持运行即可，接收末端目标并回传当前末端反馈；电脑统一完成手腕标定、位移倍率和旋转映射。电脑上的 `--side`、`--only` 和 `--scale` 无需在机器人重复填写。重新启动电脑端后会重新标定，无需启动自建 HTTP 服务。

仅控制 Wuji 手时跳过这一节。接收与目标转发也可以在同一台具备 ROS 环境的 Linux 机器上运行，此时电脑命令加 `--thor-host 127.0.0.1`。

### Wuji

Wuji 手接入 Linux / WSL USB，并填写对应侧的序列号即可。遥操启动后，手部跟随进程直接调用官方 SDK，无需单独启动 Wuji 服务。左右手分别连接各自的序列号。

如果以前启动过 Wuji HTTP 服务或其他占用灵巧手的程序，请先停止，让遥操进程直接连接 USB。

## 4. 连接 Quest 并运行

### USB

1. Quest 用 USB 接电脑，戴上头显并允许 USB 调试。
2. 打开 Hand Tracking Streamer，设置 **TCP / 127.0.0.1 / 8000**。
3. 打开 **Head Pose、Debug Info**，关闭 **Video Feed**。
4. 在电脑启动下面的遥操命令，出现等待提示后，在头显点 **Start Streaming**。
5. 面向希望对应机器人前方的方向，让所选侧的手保持在头显视野中，等待电脑终端的标定提示后移动；双侧启动时，两只手都需可见。

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -u -m quest_teleop
```

原生 Linux：

```bash
./.venv/bin/python -u -m quest_teleop
```

默认跟随右臂和右手；机器人需已运行上一节的目标转发命令。电脑按 **Ctrl+C** 停止发送目标并停止本地灵巧手，机器人停止刷新 IK 目标，保留转发进程等待重连。机器人终端按 **Ctrl+C** 结束转发进程。

### Wi-Fi

Quest 与电脑连接同一可互通的网络。头显选择 UDP，填写电脑在该网络上的 IP 和端口 `9000`，电脑命令增加：

```text
--transport udp --port 9000
```

允许防火墙接收该端口的 UDP 数据；其他头显设置与 USB 相同。

## 5. 常用参数

在运行命令后追加：

| 参数 | 用途 |
| --- | --- |
| `--only arm` | 只跟随所选侧的机械臂 |
| `--only hand` | 只跟随所选侧的灵巧手 |
| `--only both` | 所选侧的臂和手一起跟随，默认值 |
| `--side right` | 右侧，默认值 |
| `--side left` | 左侧 |
| `--side both` | 左右双侧，分别跟随对应侧的裸手 |
| `--preview` | 接收和计算姿态，不控制设备 |
| `--scale 1.5` | 将手腕位移放大到 1.5 倍 |
| `--config <文件>` | 使用指定配置文件 |
| `--thor-host <机器人IP>` | 指定机器人地址，默认读取配置 |
| `--thor-port 8003` | 修改机器人目标端口，默认 `8002`；机器人端使用 `--port 8003` |
| `--frame-port 8003` | 修改电脑内部 Quest 数据分发端口，默认 `8001` |
| `--frame-bind <电脑IP>` | 限定电脑内部数据监听网卡，默认 localhost |

Windows 示例（Linux 使用同样参数）：

```powershell
# 默认右臂 + 右手
.\.venv\Scripts\python.exe -u -m quest_teleop

# 左臂 + 左手
.\.venv\Scripts\python.exe -u -m quest_teleop --side left

# 双臂 + 双手
.\.venv\Scripts\python.exe -u -m quest_teleop --side both

# 仅双臂
.\.venv\Scripts\python.exe -u -m quest_teleop --side both --only arm

# 仅双手
.\.venv\Scripts\python.exe -u -m quest_teleop --side both --only hand
```

`--side` 与 `--only` 可自由组合，例如 `--side left --only hand` 只控制左手。增加 `--preview` 可只预览；增加 `--scale 1.5` 可放大手臂位移。

旧版 `--arms` 参数及配置字段已改为 `--side` / `side`。

日志统一保存在电脑 `logs/session_*`。某侧 Quest 输入中断时，对应侧暂停等待，另一侧可继续跟随。手臂末端反馈暂时缺失也会暂停该侧，恢复时对齐当前末端；灵巧手恢复后继续跟随绝对手势。两台机器无需校准系统时钟，反馈时效按传输耗时换算。

正常跟随只记录启动、标定、状态变化、警告与停止摘要，不保存逐帧数据。`--preview` 会每秒显示一次计算结果，不连接设备。机械臂目标交给厂商 IK，包内不再提供额外的软件工作空间或步长限制。

仅控制手时不需要启动机器人端命令；仅控制机械臂时不加载 Wuji SDK。旧配置中的 `arm_limit_policy`、`workspace`、`max_step_m` 已移除，升级时请删除这三个字段。

## 6. 常见问题

- **一直等 USB**：运行 `adb devices`；`unauthorized` 时在头显允许 USB 调试，没有设备时检查数据线和连接。
- **头显报 momo 错误**：先启动电脑端并等到等待输入提示，确认 TCP 地址、端口和 Video Feed 设置，再 Stop / Start Streaming；无法停止时重开 App。
- **端口占用**：用 Ctrl+C 结束之前的遥操进程。修改 Quest 端口时，电脑 `--port` 和头显端口需一致。
- **没有手姿态**：先确认头显本身能识别裸手，再确认 Debug Info 已开启，所选侧的手在视野内。
- **电脑一直等待 Thor**：确认 `thor_host` 是机器人 IP，机器人已运行 `quest_teleop.arm_bridge`，电脑可访问机器人 TCP `8002`，且两端使用同一版代码。
- **手臂一直等模式或反馈**：在机器人终端加载厂商 ROS 环境，核对 `ROS_DOMAIN_ID`、末端反馈和模式 `[1,1]`、input=1、IK source=2。
- **手连不上**：检查序列号、USB 权限和设备是否被其他程序占用。Windows 用户还需确认 Wuji USB 已接入 WSL。
- **Windows 手部一直等帧**：确认 WSL 镜像网络已生效，且没有防火墙规则阻止本地 `8001` 端口。

当前目标转发方案、左手及双侧臂手并行仍需真机标定和跟随验证；原生 Linux 整套运行仍待验证。
