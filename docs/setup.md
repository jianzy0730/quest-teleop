# 首次配置与设备连接

这页用于**新 Windows 电脑第一次配置**，按天机机械臂和 Wuji 灵巧手一起使用来准备，以现有这套设备和 Quest 3 为例。配置完成后，日常按 [README](../README.md) 启动即可，不用每次重做。

## 1. 电脑准备

安装 [Python 3.10 或更新版本](https://www.python.org/downloads/windows/)（勾选 Add Python to PATH）和 [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools)（ADB）。将解压后的 `platform-tools` 目录加入用户 `Path` 环境变量，重新打开 PowerShell，确认 `python --version`、`adb version` 都能运行。

接着安装 WSL 2，用来运行灵巧手 SDK。在**管理员 PowerShell** 执行：

```powershell
wsl --install -d Ubuntu-22.04
```

按提示重启，打开 Ubuntu-22.04，完成用户名和密码设置。已有这个发行版则跳过安装。

Windows 与 WSL 需要共享网络。在 PowerShell 打开配置文件：

```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

写入下面内容；如果已有 `[wsl2]` 段，就在该段内修改或添加这一项：

```ini
[wsl2]
networkingMode=mirrored
```

保存后，关闭正在使用的 WSL 任务，执行 `wsl --shutdown`。下次打开 Ubuntu 时生效。镜像网络需要 Windows 11 22H2 或更新版本及新版 WSL；旧 WSL 可用 `wsl --update` 更新。

<a id="wuji"></a>

## 2. 连接 Wuji：两根网线，两个电脑网口

两只手分别接配套电源，再连接：

```text
左手 ──网线── 电脑网口 1
右手 ──网线── 电脑网口 2
```

网口不够可以用 USB 转以太网适配器。**不用交换机，也不需要把手作为 USB 设备转接进 WSL。**

### 设置两个电脑网口

按 `Win + R`，输入 `ncpa.cpl`。分别拔插两只手的网线，观察哪个网卡连接状态变化，将对应网卡重命名为 **Wuji左手**、**Wuji右手**，方便后续配置。

分别右键网卡 → 属性 → Internet 协议版本 4（TCP/IPv4）→ 使用下面的 IP 地址：

| 填写项 | Wuji左手网卡 | Wuji右手网卡 |
| --- | --- | --- |
| 电脑 IP 地址 | `192.168.1.100` | `192.168.1.101` |
| 子网掩码 | `255.255.255.0` | `255.255.255.0` |
| 网关、DNS | 留空 | 留空 |

保存。不要改连接天机的网卡，也不要改手的地址：现有左手仍是 `192.168.1.110`，右手仍是 `192.168.1.111`。另一套同型号设备需先确认其实际 IP。

### 指定每只手走哪个网口

因为两个网口在同一网段，还需设置两条固定路由。**管理员 PowerShell** 执行一次：

```powershell
New-NetRoute -DestinationPrefix "192.168.1.110/32" -InterfaceAlias "Wuji左手" -NextHop "0.0.0.0" -RouteMetric 1 -PolicyStore PersistentStore
New-NetRoute -DestinationPrefix "192.168.1.111/32" -InterfaceAlias "Wuji右手" -NextHop "0.0.0.0" -RouteMetric 1 -PolicyStore PersistentStore
```

路由重启后仍保留，以后保持左右手接到对应网卡。执行以下命令检查，两个地址都应有回复：

```powershell
ping 192.168.1.110
ping 192.168.1.111
```

### 安装依赖后确认 SDK 能找到手

按 [README 的安装步骤](../README.md#1-安装只做一次)装好手部依赖后，在 **Ubuntu 终端** 执行：

```bash
~/.venv-quest-teleop/bin/python -c "import wuji_sdk; print([(d.sn, str(d.device_type)) for d in wuji_sdk.SdkManager.instance().scan()])"
```

这是扫描，不会让手运动。现有左手序列号为 `WH2JA01260902022`，右手为 `WH2KA01260905017`，`config.json` 中已填好。换了一套手时，直接修改其中的 `left_hand_serial`、`right_hand_serial`，不要只按扫描输出顺序判断左右。

Windows 能 ping 通、SDK 却找不到手时，先确认 WSL 镜像网络生效，再检查 Windows / Hyper-V 防火墙是否允许 Wuji SDK 通信。

## 3. 连接天机

按[天机使用文档](https://acerobotics.feishu.cn/wiki/VapdwmUi4iHhlFkWfCccBqQVn0b?from=from_copylink)完成机器人上电、电脑与天机的网络连接和控制软件启动。电脑上的 `thor_host` 默认是 `6.6.8.100`。

**原来那台天机已部署遥操桥接并设置自启动，换电脑不用重新部署。** 在 PowerShell 检查：

```powershell
Test-NetConnection 6.6.8.100 -Port 8002
```

`TcpTestSucceeded : True` 表示能连接桥接；不代表机器人已进入可运动状态。启动遥操后，程序会请求控制模式；若网页一直显示“等待控制模式”，需检查天机控制服务及桥接日志。

<details>
<summary>新机器人，或尚未部署遥操桥接</summary>

将与电脑端同一版本的仓库复制到天机，例如 `/home/user/quest-teleop`。在**天机终端**执行，将第一行换成实际目录：

```bash
cd /home/user/quest-teleop
source /etc/apex/apex_ros_env.sh
python3 -u -m quest_teleop.arm.thor --port 8002
```

使用厂商已有的 ROS / Python 环境，需能导入 `rclpy` 和 `numpy`；保留厂商控制服务运行。默认 ROS domain 为 `24`。保持终端运行，并允许电脑访问 TCP `8002`；这里是手动启动，未安装开机自启动服务。不要与已有的桥接重复运行。

</details>

## 4. 连接 Quest（推荐 USB）

1. 在 Meta Horizon 手机 App 中为 Quest 开启开发者模式。尚未开通开发者账号时，按 [Meta 开发者设置说明](https://developers.meta.com/horizon/documentation/native/android/mobile-device-setup/)完成账号和设备设置。
2. 用 USB **数据线**连接电脑，戴上头显，允许 USB 调试。PowerShell 执行 `adb devices`，设备后面应显示 `device`；显示 `unauthorized` 时需在头显授权。
3. 从 [Hand Tracking Streamer Releases](https://github.com/wengmister/hand-tracking-streamer/releases) 下载 APK，使用 ADB 安装。将下面路径换成实际文件路径：

   ```powershell
   adb install -r "C:\Downloads\hand-tracking-streamer.apk"
   ```

4. 在头显应用库的“未知来源”中打开 App。确认 Quest 系统已开启手部追踪，能识别裸手，然后按 [README](../README.md#2-启动遥操每次使用)设置串流并启动。

USB 端口转发由程序自动设置，不需要手动运行 `adb reverse`。

<details>
<summary>改用 Wi-Fi</summary>

Quest 与电脑连接同一个、设备之间可互通的 Wi-Fi。在电脑执行 `ipconfig`，找到 **Wi-Fi 网卡的 IPv4 地址**；不要填天机或 Wuji 网卡的地址。

- 网页选择 Quest 连接 **Wi-Fi**，端口 `9000`，点“启动”。
- 头显选择 **UDP**，地址填电脑 Wi-Fi IP，端口 `9000`；Head Pose、Debug Info 开启，Video Feed 关闭，再点 Start Streaming。
- 允许电脑防火墙接收 UDP `9000`。如果网络启用了客户端隔离，即使连同一个 Wi-Fi 也可能收不到数据。

此方式不需要 USB 和 ADB。换网络后重新确认电脑 IP，不要照抄旧电脑的地址。

</details>

配置完成，回到 [README](../README.md) 安装并启动程序。
