# Quest Teleop

用 Quest 3 的裸手姿态控制天机机械臂和 Wuji Hand 2，支持单侧、双侧，以及动作采集和回放。

这份教程从设备已上电、网络已接通开始。还没配置网口、WSL 或 Quest的，先看[首次配置](docs/setup.md)。

## 1. 安装（只做一次）

下面按 **Windows 11 + Ubuntu-22.04 WSL，同时控制机械臂和灵巧手** 安装。

### 在 Windows 安装主程序

从 [GitHub](https://github.com/jianzy0730/quest-teleop) 拉取代码。

打开 **PowerShell**，执行：

```powershell
cd C:\path\to\quest-teleop
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

### 在 WSL 安装手部依赖

打开 **Ubuntu-22.04 终端**，执行：

```bash
cd /mnt/c/path/to/quest-teleop
sudo apt-get update
sudo apt-get install -y python3-venv
python3 -m venv ~/.venv-quest-teleop
~/.venv-quest-teleop/bin/python -m pip install -e '.[hand]'
```

### 配置

**目前默认配置：**

| 设备 | 默认配置 |
| --- | --- |
| 天机 | `6.6.8.100:8002` |
| Wuji 左手序列号 | `WH2JA01260902022` |
| Wuji 右手序列号 | `WH2KA01260905017` |

使用上面这套设备时不用修改。换设备或 WSL 发行版时，直接编辑项目根目录的 `config.json`：

| 配置项 | 填什么 |
| --- | --- |
| `thor_host` | 天机的 IP 地址 |
| `left_hand_serial` / `right_hand_serial` | 左右手实际序列号 |
| `wsl_distro` | WSL 发行版名称，默认 `Ubuntu-22.04` |

## 2. 启动遥操（每次使用）

下面先启动默认的**右臂 + 右手**。先在电脑开网页，再在头显开始串流。

1. 给天机和灵巧手上电并连接电脑，确认天机控制软件和遥操桥接已运行。Quest 用 USB 数据线接电脑，在头显里允许 USB 调试。
2. 在 **PowerShell** 执行，保持这个窗口打开：

   ```powershell
   cd C:\path\to\quest-teleop
   .\.venv\Scripts\python.exe -u -m quest_teleop.web
   ```

3. 浏览器打开 **http://127.0.0.1:8766**。在“实时控制”中选择：

   | 项目 | 怎么选 |
   | --- | --- |
   | 设备 | **手臂 + 灵巧手**（默认） |
   | 控制侧 | **右侧**（默认；用双臂双手时选“左右双侧”） |
   | Quest 连接 | USB |

   点 **启动**，程序开始等待 Quest 数据，收到后即可遥操手臂和灵巧手。

4. 戴上 Quest，打开 [Hand Tracking Streamer](https://github.com/wengmister/hand-tracking-streamer)，设置：

   | 项目 | 设置 |
   | --- | --- |
   | 协议 / 地址 / 端口 | **TCP / 127.0.0.1 / 8000** |
   | Head Pose、Debug Info | 开启 |
   | Video Feed | 关闭 |

   面向你希望对应机器人前方的方向，将所选侧的手放在头显视野内，点 **Start Streaming**。先保持起始姿势，需要和目前机器人姿势类似比较方便；程序会将起始手腕姿势对齐到机械臂当前末端。

手臂移动幅度不合适时，停止后在网页“更多设置”里改 **手臂平移比例**。例如 `1.5` 表示人手移动 10 cm，机械臂目标移动 15 cm；旋转比例不变。


## 3. 采集动作

按上一节启动遥操，确认机器人能跟随，再到“采集”区域操作：

1. 点 **开始采集**，做需要录制的动作。
2. 点 **结束采集**，保存这段录制。

**采集期间手臂和灵巧手照常跟随，结束采集也不会停止遥操。**

也可以勾选 **✌ 手势开始录制**（默认关闭）：将所选侧的手保持 ✌ 约 0.7 秒，电脑响一声后开始录制。双侧遥操时任一只手均可触发。结束仍点 **结束采集**；再次触发前先放下手势，再重新做 ✌。

录制文件保存在项目的 `recordings/`，保存后出现在网页回放列表。录的是 Quest 姿态数据，不是摄像头视频或机器人实际运动轨迹。

## 4. 回放动作

1. 点 **停止全部**，结束当前遥操。
2. 在“回放”列表中选一条录制，选择速度，默认 `1×`。
3. 回放方式选 **执行到机器人**，再点 **回放选中动作**。默认的“仅预览”只运行姿态计算，不驱动机器人，也没有三维动画画面。

回放不需要连接 Quest，会使用录制时的设备、控制侧和手臂平移比例。**机械臂从回放开始时的当前位置复现相对动作，不会自动回到录制时的绝对位置。** 中途停止同样点“停止全部”。

## 遇到问题

| 现象 | 先检查什么 |
| --- | --- |
| Quest 一直等待串流 | `adb devices` 应显示 `device`；核对头显的 TCP、地址和端口，再 Stop → Start Streaming |
| 头显报 momo 错误 | 先在网页点“启动”，关闭 Video Feed，再重新串流；App 卡住时重新打开 |
| 收到数据但机器人不动 | 看网页设备状态是等待连接、等待控制模式，还是跟随中，再检查对应设备 |
| 找不到 Wuji 手 | 检查供电、[双网口配置](docs/setup.md#wuji)、序列号；关闭其他占用手的程序 |
| 提示端口占用 | 结束之前运行的网页或命令行进程，再重新启动 |

仍有问题时，查看项目 `logs/` 下最新会话的 `console.log` 和对应设备日志。

## 其他用法

<details>
<summary>只控制手或只控制臂</summary>

先停止当前会话，将网页的“设备”改为“仅灵巧手”或“仅手臂”，再点“启动”。

</details>

<details>
<summary>不用网页，直接命令行遥操</summary>

在项目目录的 PowerShell 中执行；不要与网页会话同时运行：

```powershell
# 右臂 + 右手
.\.venv\Scripts\python.exe -u -m quest_teleop --only both --side right

# 双臂 + 双手
.\.venv\Scripts\python.exe -u -m quest_teleop --only both --side both
```

`--only` 可选 `arm`（只控制臂）、`hand`（只控制手）、`both`；`--side` 可选 `left`、`right`、`both`。出现 `Waiting for Quest` 后在头显开始串流，停止按 `Ctrl+C`。

这些命令会直接控制设备。只想预览计算结果时加 `--preview`；命令行不会自动生成网页中的录制动作。

</details>

<details>
<summary>原生 Ubuntu 安装与启动</summary>

在项目目录执行，不需要 WSL：

```bash
sudo apt-get update
sudo apt-get install -y python3-venv adb
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[hand]'
```

编辑 `config.json`，将 `hand_python` 改成本项目 Python 的绝对路径，例如 `/home/you/quest-teleop/.venv/bin/python`。

启动网页用 `./.venv/bin/python -u -m quest_teleop.web`，后续网页操作与 Windows 相同。手的网卡地址、路由需在 Ubuntu 中配置；原生 Ubuntu 整套流程仍待真机验证。

</details>
