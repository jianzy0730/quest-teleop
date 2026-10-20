# Quest Teleop

用 Quest 3 裸手遥操 Thor 和 Wuji Hand 2。支持左侧、右侧或双侧，可单独控制臂或手，默认右臂 + 右手。

## 1. 安装

需要 Python 3.10+ 和头显上的 [Hand Tracking Streamer](https://github.com/wengmister/hand-tracking-streamer)。USB 连接需要 ADB。下面的命令均从本仓库目录执行；Windows 和 Linux 选择对应步骤。

### Windows + WSL

在 PowerShell 安装电脑端接收和预览环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path config.local.json)) { Copy-Item config.example.json config.local.json }
```

安装 [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools)，并确保 `adb devices` 能运行。

控制 Wuji 手还需要 Ubuntu 22.04 WSL；仅控制机械臂可跳过以下 WSL 步骤。在 WSL 中进入同一仓库目录，执行：

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

```bash
sudo apt-get install python3-venv adb
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[hand]'
test -f config.local.json || cp config.example.json config.local.json
```

Wuji 手直接接入 Linux，按 [Wuji SDK](https://pypi.org/project/wuji-sdk/) 要求配置 USB 访问权限。仅用机械臂时，将安装命令中的 `'.[hand]'` 改为 `.`。

## 2. 配置

编辑 `config.local.json`：

| 字段 | 填写内容 |
| --- | --- |
| `right_hand_serial` / `left_hand_serial` | 对应侧 Wuji 手的序列号；不用的一侧可留空字符串 |
| `hand_python` | 手部环境的 Python 路径；Windows 使用 WSL 内路径 |
| `wsl_distro` | Windows 上的 WSL 发行版名称；原生 Linux 忽略此项 |
| `thor_host` | Thor 的 IP，默认 `6.6.8.100` |
| `thor_port` | Thor 目标接收端口，默认 `8002` |

Windows 按上述步骤安装时，`hand_python` 保持 `~/.venv-quest-teleop/bin/python`。原生 Linux 改为本仓库环境的绝对路径，例如 `/home/you/quest-teleop/.venv/bin/python`。

## 3. 准备设备

预览模式可跳过这一步。

### Thor：在机器人上运行目标转发

仅控制 Wuji 手时跳过本节。将仓库源码复制到机器人，例如 `/home/user/quest-teleop`，使用机器上已有的厂商 Python / ROS 2 / IK 环境运行。

机器人需已设置模式 `[1,1]`、input=1、IK source=2，程序不会自动切换。默认 `ROS_DOMAIN_ID=24`，可通过环境变量修改。

关闭其他发送机械臂目标的程序，保留厂商 `apex-robot`、`apex-teleop` 服务。在机器人终端执行：

```bash
cd /home/user/quest-teleop
source /etc/apex/apex_ros_env.sh
python3 -u -m quest_teleop.arm.thor
```

保持该终端运行，允许电脑访问机器人 TCP `8002`。修改端口时，机器人加 `--port <端口>`，电脑加 `--thor-port <端口>`。

### Wuji

将手接入 Linux / WSL，填写对应序列号，关闭其他占用该手的程序。无需单独启动 Wuji 服务。

## 4. 连接 Quest 并运行

### USB

1. Quest 用 USB 接电脑，戴上头显并允许 USB 调试。
2. 打开 Hand Tracking Streamer，设置 **TCP / 127.0.0.1 / 8000**。
3. 打开 **Head Pose、Debug Info**，关闭 **Video Feed**。
4. 在电脑启动下面的遥操命令，出现等待提示后，在头显点 **Start Streaming**。
5. 让所选侧的手保持在头显视野中；控制机械臂时，面向希望对应机器人前方的方向，等待电脑终端的标定提示后移动。

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -u -m quest_teleop
```

原生 Linux：

```bash
./.venv/bin/python -u -m quest_teleop
```

电脑按 **Ctrl+C** 停止遥操；Thor 转发程序保留运行，等待下次连接。在机器人终端按 **Ctrl+C** 可结束转发程序。

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
| `--only <选项>` | `arm`、`hand` 或 `both`，默认 `both` |
| `--side <选项>` | `left`、`right` 或 `both`，默认 `right` |
| `--preview` | 接收和计算姿态，不控制设备 |
| `--scale 1.5` | 手腕位移放大到 1.5 倍；默认 `1.0`，旋转保持 1:1 |
| `--thor-host <机器人IP>` | 指定机器人地址，默认读取配置 |

Windows 示例（Linux 使用同样参数）：

```powershell
# 仅左手
.\.venv\Scripts\python.exe -u -m quest_teleop --side left --only hand

# 双臂 + 双手
.\.venv\Scripts\python.exe -u -m quest_teleop --side both

# 仅双臂
.\.venv\Scripts\python.exe -u -m quest_teleop --side both --only arm
```

`--side` 与 `--only` 可自由组合。完整参数可在上述运行命令后加 `--help` 查看。

## 6. 常见问题

日志保存在电脑 `logs/session_*`。输入中断时程序暂停等待；恢复后，机械臂重新对齐当前末端，灵巧手继续跟随当前手势。

- **一直等 USB**：运行 `adb devices`；`unauthorized` 时在头显允许 USB 调试，没有设备时检查数据线和连接。
- **头显报 momo 错误**：先启动电脑端并等到等待输入提示，确认 TCP 地址、端口和 Video Feed 设置，再 Stop / Start Streaming；无法停止时重开 App。
- **端口占用**：用 Ctrl+C 结束之前的遥操进程。修改 Quest 端口时，电脑 `--port` 和头显端口需一致。
- **没有手姿态**：先确认头显本身能识别裸手，再确认 Debug Info 已开启，所选侧的手在视野内。
- **电脑一直等待 Thor**：确认 `thor_host` 是机器人 IP，机器人已运行 `quest_teleop.arm.thor`，电脑可访问机器人 TCP `8002`，且两端使用同一版代码。
- **手臂一直等模式或反馈**：在机器人终端加载厂商 ROS 环境，核对 `ROS_DOMAIN_ID`、末端反馈和模式 `[1,1]`、input=1、IK source=2。
- **手连不上**：检查序列号、USB 权限和设备是否被其他程序占用。Windows 用户还需确认 Wuji USB 已接入 WSL。
- **Windows 手部一直等帧**：确认 WSL 镜像网络已生效，且没有防火墙规则阻止本地 `8001` 端口。

当前目标转发方案、左手及双侧臂手并行仍需真机标定和跟随验证；原生 Linux 整套运行仍待验证。
