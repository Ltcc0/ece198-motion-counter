# ECE198 Motion Counter

一个可以在 Mac 上演示的 Arduino 动作经过计数原型：HC-SR04 检测物体经过，UNO R4 Minima 计数并保存摘要，英文网页显示实时状态、次数和历史记录。固件、网页和本地连接程序都在这个文件夹中。

## 在线使用

打开 [STEPS 训练网站](https://sagacopilot.com/) 并用邮箱、密码注册或登录。**My progress** 可按日期查看本账号的训练记录，**Session records** 可导出 CSV。在线账号和记录保存在 Cloudflare D1，和离线演示的本机 SQLite 分开。

做实时训练时，在连接 Arduino 的电脑上使用支持 Web Serial 的桌面浏览器（如 Chrome 或 Edge）。关闭占用串口的本机演示服务或串口监视器，在 **Device setup → Choose USB device** 中选择开发板，然后选择训练模式并开始。保持此标签页打开，直到板端确认保存、记录同步到账号。浏览器会为网络中断时已收到的保存回执重试同步。没有 USB 的设备也可登录查看历史。

## 每次演示的快速启动

**首次按下面第 2 节上传一次固件后，以后每次只需：**

1. 接好传感器，并用 USB 数据线把 Arduino 连接到 Mac。
2. 在项目文件夹运行 `./start-demo.command`，保留终端窗口。脚本启动本地连接程序和网页，并尝试自动识别、连接唯一的 Arduino。
3. 首次进入网页时点击 **Create account**，用邮箱和密码注册即可开始。以后用同一账号登录。设备连接后，在 **Next session** 选训练模式并点击 **Start session**。
4. 自由练习结束时点击 **Stop & save**。目标次数和限时练习会在电脑收到达标读数后自动停止，也可以提前结束。在 **My progress** 查看每日变化，点 **Session records** 查看每一场。

**日常演示不用打开 Arduino IDE，也不用手动输入任何串口命令。** Arduino 中已上传的程序随 USB 供电运行，网页按钮负责开始、停止本场计数；按钮不会重新上传固件。首次安装与详细操作见下文。

**本版本采用 Passage mode：每经过一次计 1 次。向下经过一次、移出后再向上经过一次，合计 2 次。这个数字表示检测到的经过次数，不代表完成了多少个临床规定动作。**

## 训练模式与每日趋势

| 网页模式 | 如何结束 | 用途 |
| --- | --- | --- |
| Free practice（自由练习） | 手动点击 Stop & save | 自选练习时长，记录经过次数 |
| Count goal（目标次数） | 达到设定的经过次数后自动保存 | 比较同一次数目标下的时长和节奏 |
| Timed practice（限时练习） | 达到设定的秒数后自动保存 | 比较同一时长目标下的经过次数 |

三个模式共用原有 FW3 Passage 固件。训练中请保持 USB 和本机服务连接。电脑根据开发板上报的次数或秒数发送 STOP，最终保存开发板确认的实测值，采样与串口延迟可能使结果略高于目标。提前结束也会保存实际结果。

登录后，首页的 **My daily progress** 按训练开始时电脑的本地日期汇总自己的场次、次数、时长和达标场数。用 **From / To** 选择日期范围，起止日都计入；留空查看全部日期。选择同一模式和目标，可比较区间内首个有效训练日和最近训练日的日期、节奏与差值。日期筛选也适用于场次列表和 CSV。平均节奏采用有效场次的总经过次数除以总分钟数。记录为零秒的场次仍计入训练量，只从节奏计算中排除。

例如，同一用户两天都做 60 秒限时练习，每天一场，分别经过 8 次、10 次，页面就会显示节奏从 8 到 10 次/分钟，变化为 +2。比较时应保持动作和传感器摆放一致。仅凭这些记录无法判断动作质量、整体运动能力或康复疗效。

## 获取项目

从[私有 GitHub 仓库](https://github.com/Ltcc0/ece198-motion-counter)下载代码，需要使用已获该仓库访问权限的 GitHub 账号完成认证。在终端运行：

```sh
git clone https://github.com/Ltcc0/ece198-motion-counter.git
cd ece198-motion-counter
```

已经收到完整项目文件夹时，可直接进入该文件夹，无需再次克隆。

## 1. 准备设备

需要 Arduino UNO R4 Minima、HC-SR04、面包板、杜邦线、支持数据传输的 USB-C 线和一台 Mac。电脑需要：

- Arduino IDE 2，用于首次上传固件，以及以后修改固件。
- Python 3.9 或以上，用于网页与 USB 串口之间的本地连接。运行 `python3 --version` 可查看版本；未安装时使用 [Python 官方 macOS 安装包](https://www.python.org/downloads/macos/)。
- 浏览器。网页无需 Node.js、npm 或前端编译。

首次安装开发板支持包和 Python 依赖需要网络；安装完成后，演示在本机运行，可以离线使用。

断开 USB 后，按器件上印刷的引脚名称接线：

| HC-SR04 | Arduino UNO R4 Minima |
| --- | --- |
| VCC | 5V |
| TRIG | 9 |
| ECHO | 10 |
| GND | GND |

传感器四脚应分别占面包板的四个不同行，导线与对应脚位于同一组相通的孔。接线确认后再插 USB。当前 `USE_POT = false`，电位器不需要连接。可对照[接线示意图](docs/wiring.svg)，实际连接始终以两端引脚名称为准。

## 2. 首次上传 Arduino 程序

1. 用 Arduino IDE 打开 [`firmware/ExerciseCounter/ExerciseCounter.ino`](firmware/ExerciseCounter/ExerciseCounter.ino)。
2. 打开 **Boards Manager / 开发板管理器**，搜索 `UNO R4`，安装 **Arduino UNO R4 Boards**。
3. 选择 **Arduino UNO R4 Minima**，并选中实际 USB 端口。Mac 上通常显示为 `/dev/cu.usbmodem...`；不要选择 Bluetooth 或 debug-console。
4. 点击 **Upload / 上传**，等待上传完成。只点击 Verify / 验证不会把程序写入开发板。Arduino 官方的[开发板与端口选择说明](https://support.arduino.cc/hc/en-us/articles/4406856349970-Select-board-and-port-in-Arduino-IDE)可用于对照菜单。
5. **关闭 Arduino IDE 的 Serial Monitor / 串口监视器和 Serial Plotter / 串口绘图器**，再使用网页。它们与网页连接程序不能同时占用同一个串口。

上传后，嵌入式程序会在开发板上自动运行。电脑不需要再启动一个“嵌入式服务”；点击网页的 **Start session** 才开始本场计数。以后演示只要固件没有变化，就不必重复上传。

## 3. 启动英文演示网页

打开 Terminal / 终端，进入本项目文件夹。例如输入 `cd `（包括后面的空格），把 Finder 中的 `ece198-motion-counter` 文件夹拖进终端，然后按回车。

运行：

```sh
./start-demo.command
```

启动脚本会准备本项目的 `.venv` 环境、安装所需依赖并打开浏览器。**保留这个终端窗口运行。** 如果浏览器没有自动打开，访问：

[http://127.0.0.1:8765](http://127.0.0.1:8765)

程序会尝试自动连接唯一检测到的 Arduino。若网页尚未连接，打开 **Device setup**，选择 Arduino 的 USB 端口并点击 **Connect device**。确认设备已连接、状态正在更新后再开始演示。这个网页通过本机 Python 程序访问 USB，不需要另开一个前端服务。

主页集中显示计数、持续时间和距离，按钮下方显示当前检测状态。顶部 **Session records** 打开记录窗口，其中 **Refresh** 更新历史，**Export CSV** 导出记录。

若启动脚本提示没有执行权限，在项目目录中运行 `chmod +x start-demo.command` 后重试。

### 手动启动方式

以下命令也都在项目根目录执行。首次创建环境并安装依赖：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

每次启动：

```sh
.venv/bin/python backend/server.py
```

然后打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。手动方式与启动脚本二选一即可。

## 4. 正确演示一次经过

把传感器固定在侧面，使物体上下移动时穿过它正前方的探测区域。用平整、较大的物体先测试，并避免摇动传感器。

1. 点击 **Start session**，先把物体移出探测区域，等待网页显示 **Ready for your next pass**（固件状态 `ARMED`）。
2. 让物体缓慢穿过传感器前方 **2–25 cm** 的区域。连续三次有效读数进入该范围后，次数加 1。
3. 物体停在前面时，网页显示 **Pass counted · move fully away**（固件状态 `OCCUPIED`），次数保持不变。
4. 完全移出探测区域，或移远到 **35 cm 及以上**，保持至少 **0.3 秒**，等到再次显示 **Ready for your next pass** 后再经过。
5. 点击 **Stop & save**。确认设备已保存本场摘要，再点击顶部 **Session records** 打开记录窗口；需要时使用其中的 **Refresh** 或 **Export CSV**。

**每次反向经过之前也要留出清空时间。** 太快的挥动可能不足三次采样，太快反向且没有清空间隔也不会算成第二次。25–35 cm 中间区域不算进入，也不能完成清空；小于 2 cm 的读数不参与计数或清空。

在 Passage mode 中，持续无回波也可表示区域已清空；网页距离栏会显示 **Clear**，对应串口信息 `NO_TARGET / NO_ECHO`，不一定是故障。但 USB 断开、连接报错或长期无法检测到放在前方的物体，需要排查连接与摆放。无回波本身不会增加次数；静止物体若持续丢失回波又重新出现，仍可能被误认为再次经过。

演示结束先 **Stop & save** 并确认保存成功，然后在启动程序的终端按 **Control+C** 关闭网页服务。

## 5. 数据与项目范围

- 开发板 EEPROM 保存最近 **16 场**有效摘要：场次编号、次数和持续秒数。超过容量后覆盖最旧记录。
- **只有 STOP 成功时才保存本场摘要。** 未停止就断电，正在进行的场次不会恢复。记录检查不能保证任意断电时写入都完整。
- 开发板历史记录没有模式字段；没有另存的可靠模式信息时，模式为 **Not recorded / 未记录**。这些记录可能使用“靠近再返回算一次”等其他规则，不应直接与当前经过次数比较。
- 电脑在本地 SQLite 数据库 `runtime/sessions.sqlite3` 保存账号、训练模式、目标、开始与保存时间、次数和持续秒数。注册仅需邮箱和密码，无需邮件验证。密码以加盐哈希存储。**Sign out** 退出当前登录，重新登录后可继续查看个人历史。数据库已列入 Git 忽略范围。
- 旧版 `runtime/sessions.json` 的有效记录会在启动时导入，原文件保留。开发板日志里的历史场次没有用户或日历日期。旧昵称档案及其记录继续保存在本机数据库，注册邮箱不会自动认领同名档案。个人页面仅展示该账号的已归属场次。若训练中 USB 断开或服务退出，重连后只能从开发板取回已保存的摘要。

原项目提案关注 STEPS 项目结束后的居家运动提示与参与记录，由临床人员决定运动内容与适用对象。当前交付是课堂台架原型：电脑网页承担原设想中 OLED 和按钮的交互功能，计数方式按本次演示需求改为 Passage mode。自动校准尚未实现，电位器默认关闭；阈值来自固件设置。本原型不评估动作质量、康复效果或临床适用性。

## 文件与进一步说明

| 路径 | 用途 |
| --- | --- |
| `firmware/ExerciseCounter/` | 上传到 UNO R4 Minima 的程序 |
| `frontend/` | 英文网页，无需构建 |
| `backend/server.py` | 本地网页服务与 USB 串口连接 |
| `backend/training.py` | SQLite 用户与场次存储、每日训练汇总 |
| `start-demo.command` | Mac 启动入口 |
| `scripts/flash.sh` | 可选的 Arduino CLI 上传入口；新手优先使用 IDE |
| `scripts/test-firmware.sh` | 固件逻辑的电脑模拟测试；不能代替实际传感器测试 |
| `docs/DEMO_GUIDE.md` | 英文演示稿和故障排查 |
| `docs/SERIAL_PROTOCOL.md` | 固件串口命令与输出格式 |

演示步骤和排查见 [Demo guide](docs/DEMO_GUIDE.md)，接口细节见 [Serial protocol](docs/SERIAL_PROTOCOL.md)。

## Cloudflare 部署

前端资源、Worker 入口和 D1 绑定见 `wrangler.jsonc`。首次初始化一个新实例时，创建 D1 并填入绑定 ID，运行 `npx wrangler d1 migrations apply ece198-motion-training --remote`，为 Worker 配置随机的 `PASSWORD_PEPPER` secret，然后运行 `npx wrangler deploy`。已有实例发布新版本时沿用原 secret，替换它会影响已有账号登录。
