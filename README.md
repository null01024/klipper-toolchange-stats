# klipper-toolchange-stats

Klipper 多热端 / 多工具头换头插件。它负责换头流程编排、当前热端状态保存、偏移应用、夹紧检测、XY 防撞检测、换头统计、耗材检测、断料续打和自动对刀校准；用户只需要在配置中实现两个机械动作钩子。

## 目录

- [1. 项目介绍](#1-项目介绍)
- [1.1 适用范围](#11-适用范围)
- [1.2 功能列表](#12-功能列表)
- [2. 安装方法](#2-安装方法)
- [3. 功能配置](#3-功能配置)
- [3.1 核心配置](#31-核心配置)
- [3.2 M104/M109 温度命令覆写](#32-m104m109-温度命令覆写)
- [3.3 换头钩子配置](#33-换头钩子配置)
- [3.4 偏移管理配置](#34-偏移管理配置)
- [3.5 夹紧检测配置](#35-夹紧检测配置)
- [3.6 换热端过程 XY 防撞检测配置](#36-换热端过程-xy-防撞检测配置)
- [3.7 换头统计配置](#37-换头统计配置)
- [3.8 耗材检测与断料续打配置](#38-耗材检测与断料续打配置)
- [3.9 自动对刀校准配置](#39-自动对刀校准配置)
- [4. 常用命令](#4-常用命令)
- [5. 安装验证](#5-安装验证)
- [6. 故障排查](#6-故障排查)
- [7. 更新迁移与许可证](#7-更新迁移与许可证)

## 1. 项目介绍

`klipper-toolchange-stats` 是一组 Klipper extras 插件和默认配置模板，用于管理多热端、多工具头机器的换头流程。

插件的核心思路是把机型无关的流程交给 Python 模块处理：

- 注册 `T0..T{n-1}`、`UNTOOL`、`CHANGE_TOOL` 等命令。
- 在换头时自动抬 Z、临时切换加速度、调用用户机械钩子、等待热端温度、恢复状态。
- 自动维护 `current_tool`，并通过 `[save_variables]` 持久化。
- 可选启用偏移、夹紧、XY 防撞、统计、耗材检测、断料续打、自动对刀等模块。

用户只需要在 `multitool_release_tool` 和 `multitool_pickup_tool` 两个宏里写自己机器的真实机械动作。

### 1.1 适用范围

适合：

- Klipper 多热端或多工具头的机器。
- 需要用 `T0`、`T1` 这类命令切换工具的机器。
- 希望在换头流程中自动处理夹紧检测、XY 防撞检测、XYZ 偏移、耗材检查和换头计时的配置。
- 使用接触式传感器做多工具头自动对刀的机器。

不适合：

- 只有单热端且不需要工具切换的普通机器。

### 1.2 功能列表

| 模块 | 是否必需 | 功能 |
|---|---:|---|
| `[multitool]` | 必需 | 主模块，注册换头命令并编排换头流程 |
| `[multitool_offsets]` | 可选 | 自动应用各热端 XYZ 偏移，支持 Z 自适应基准 |
| `[multitool_clamp]` | 可选 | 夹紧开关检测，换头前后自动校验 |
| `[multitool_xy_guard]` | 可选 | 换热端 release/pickup 过程中监听 XY DIAG，检测撞车或严重卡顿 |
| `[multitool_stats]` | 可选 | 自动统计换头次数、总耗时、阶段耗时 |
| `[multitool_filament]` | 可选 | 各热端耗材检测、打印前检查、断料续打 |
| `[tools_calibrate]` | 可选 | 接触式自动对刀校准，配合 `calibration.cfg` 写入偏移 |

未配置某个可选 section 时，对应功能不会加载，也不会参与换头流程。

## 2. 安装方法（单独本插件）

### 一键安装

在打印机 SSH 终端执行：

```bash
wget -O - https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | bash
```

如果 GitHub 访问不稳定，可为插件安装脚本启用 HTTP 下载代理：

```bash
GH_PROXY=https://v6.gh-proxy.org/ wget -O - https://v6.gh-proxy.org/https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | GH_PROXY=https://v6.gh-proxy.org/ bash
```

### 手动安装

```bash
git clone https://github.com/null01024/klipper-toolchange-stats ~/klipper-toolchange-stats
cd ~/klipper-toolchange-stats
bash install.sh
```

安装脚本会询问：

```text
是否为新安装？新安装会生成 multitool/multihotend.cfg [y/N]:
```

默认 `n`，按升级处理：只更新插件、复制缺失的默认配置，并保留已有用户配置。输入 `y` 时会进入新安装流程：

- 询问热端数量，生成 `~/printer_data/config/multitool/multihotend.cfg`。
- 询问 `dock_fan` 模式：一个共享风扇监听所有 `extruder`，或每个 `extruder` 一个风扇。
- 询问换头方案：
  - `0) 自定义：自定义换头/换热端移动路径。`
  - `1) CxChanger：https://github.com/cx330-TXY/CxChanger`
- 选择自定义时，继续询问硬件模式：
  - `多热端`：多个热端复用一个挤出机步进，`T1..Tn` 只生成温控配置。
  - `多工具头`：每个工具头都有独立挤出机步进，每个 `extruder` 都生成 step/dir/enable 等配置。

选择 CxChanger 时，安装脚本会按 `多热端` 模式生成配置，把 `schemes/CxChanger/change_tool.cfg` 复制到 `multitool/` 目录，并自动把 `multitool_config.cfg` 中的两个换头钩子改为调用 `_release_tool` / `_pickup_tool`。

新生成的 `multihotend.cfg` 含有 `TODO_*` 占位，必须填写 CAN UUID、引脚、热敏类型、挤出机参数等硬件信息后再使用。若 `multihotend.cfg` 或 `change_tool.cfg` 已存在，安装脚本不会覆盖。

### 手动更新

再次运行安装脚本会自动更新已有插件目录：脚本会在当前 Git 分支上执行 fast-forward 更新，然后重新软链 Klipper extras，并保留已存在的用户配置文件。

```bash
bash ~/klipper-toolchange-stats/install.sh
```

如果插件目录存在未提交修改，或当前分支已经和远端分叉，脚本会中止并提示先手动提交、stash、清理或 merge/rebase。

### 安装完成后，主要修改这个文件：

```text
~/printer_data/config/multitool/multitool_config.cfg
```

## 一键安装 本插件及配套mainsail前端

### 普通安装

```bash
wget -qO- https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install_toolchanger_stack.sh | bash
```

### 代理安装

```bash
GH_PROXY=https://v6.gh-proxy.org/ wget -qO- https://v6.gh-proxy.org/https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install_toolchanger_stack.sh | GH_PROXY=https://v6.gh-proxy.org/ bash
```

## 3. 功能配置

默认配置文件已经包含所有模块示例。建议先保留 `[multitool]` 和两个钩子宏，把不需要的可选 section 整段删除。

### 3.1 核心配置

`[multitool]` 是必需 section：

```cfg
[multitool]
tool_count: 4
z_hop: 0.4
feed_z: 600
accel_swap: 8000
untool_safe_z: 10
sync_active_spool: True
sync_active_extruder: True
# 共用一个物理 E 步进、T1..Tn 只做温控时保持 True；独立 E 步进机器设 False
sync_extruder_motion: True
extruder_motion_sync_stepper: extruder
default_pressure_advance_extruder: extruder
extrude_compensation_length: 0.0
extrude_compensation_speed: 1800
```

字段说明：

| 字段 | 说明 |
|---|---|
| `tool_count` | 工具数量，必填。`4` 表示注册 `T0..T3` |
| `z_hop` | 换头前相对抬 Z 的高度，单位 mm |
| `feed_z` | Z 运动速度，单位 mm/min |
| `accel_swap` | 换头期间临时使用的加速度 |
| `untool_safe_z` | 当前为无热端时，抓取第一个热端前先移动到的安全 Z |
| `sync_active_spool` | 换头后自动把 Spoolman 当前料盘切到该工具绑定的料盘，默认 `True` |
| `sync_active_extruder` | 换头后自动同步 Klipper active extruder，默认 `True` |
| `sync_extruder_motion` | 是否执行 `SYNC_EXTRUDER_MOTION`。单物理 E 步进多热端设 `True`，独立多 E 步进多热端设 `False`，默认 `True` |
| `extruder_motion_sync_stepper` | `sync_extruder_motion=True` 时要同步的共享 E 步进名，默认 `extruder` |
| `default_pressure_advance_extruder` | 未指定 `EXTRUDER=` 的 `SET_PRESSURE_ADVANCE` 会作用到该挤出步进，默认 `extruder` |
| `extrude_compensation_length` | 自动回抽/挤出补偿共用长度，单位 mm，默认 `0` 关闭；释放旧工具前按负 E 回抽，抓取新工具并等温后按正 E 补偿，执行前检查对应 extruder 的 `min_extrude_temp` |
| `extrude_compensation_speed` | 自动回抽/挤出补偿共用速度，单位 mm/min，默认 `1800` |

主模块会自动维护：

- `current_tool = -1` 表示当前无热端。
- `SAVE_VARIABLE VARIABLE=current_tool ...` 用于重启后恢复状态；若开机时工具头上已挂载热端，插件会按该值自动同步 active extruder 和物理 E 步进队列。
- `T0..T{n-1}`、`UNTOOL`、`CHANGE_TOOL T=<n>`、`QUERY_TOOL_STATUS` 命令。

不要再额外定义 `[gcode_macro T0]`、`[gcode_macro UNTOOL]` 或 `[gcode_macro CHANGE_TOOL]`，否则启动时会报命令冲突。

### 3.2 M104/M109 温度命令覆写

默认配置中的 `[gcode_macro M104]` / `[gcode_macro M109]` 会覆写温度命令，原始 Klipper 命令分别保留为 `M99104` / `M99109`。宏只负责默认工具选择和调用辅助命令，断料续打组实际工具解析由 `[multitool]` Python 模块统一完成。

行为：

- `M104 S200` / `M109 S200` 未传 `T` 时，默认作用于当前工具；若当前无工具，则作用于 `T0`。
- `M104 T1 S200` / `M109 T1 S200` 显式传 `T` 时，使用该目标工具。
- `S` 大于 0 时使用 `S` 作为目标温度；否则使用 `R`，兼容 `M109 R...`。
- 若目标工具无耗材且位于 `[multitool_filament] continuation_groups` 中，`MULTITOOL_SET_TEMPERATURE` / `MULTITOOL_WAIT_TEMPERATURE` 会复用 `multitool_filament.resolve_tool_for_pickup()` 的同一套续打组解析逻辑选择实际工具；普通降温也会作用于实际工具。组内无可用工具时，按传入工具执行。只有断料续打内部关闭旧热端时会显式绕过该解析。
- `M109` 会先设置实际工具温度，再等待实际工具温度进入目标温度 `±1.5°C`。
- `M109` 目标温度低于 `50°C` 时只设温不等待，避免 `M109 S0` 长时间等待冷却。

需要绕过覆写时，可直接调用原始命令：

```gcode
M99104 T1 S200
M99109 T1 S200
```

工具到 heater 名称的映射固定为：`T0 -> extruder`，`T1 -> extruder1`，`T2 -> extruder2`，依此类推。

### 3.3 换头钩子配置

必须实现两个宏：

```cfg
[gcode_macro multitool_release_tool]
gcode:
    {% set tool = params.TOOL|int %}
    # 在这里写把 T{tool} 放回工具坞的机械动作
    M400

[gcode_macro multitool_pickup_tool]
gcode:
    {% set tool = params.TOOL|int %}
    # 在这里写从工具坞抓取 T{tool} 的机械动作
    M400
```

默认配置中的两个钩子会直接 `action_raise_error`，这是为了提醒你必须替换为真实动作。

安装脚本新安装时可选择换头方案：

- `自定义`：自定义换头/换热端移动路径，用户自己实现 `multitool_release_tool` 和 `multitool_pickup_tool`。
- `CxChanger`：参考 [CxChanger](https://github.com/cx330-TXY/CxChanger) 的磁吸停靠坞路径模板。脚本会复制 `schemes/CxChanger/change_tool.cfg`，并把钩子自动改为：

```cfg
[gcode_macro multitool_release_tool]
gcode:
    _release_tool TOOL={params.TOOL}

[gcode_macro multitool_pickup_tool]
gcode:
    _pickup_tool TOOL={params.TOOL}
```

使用 CxChanger 模板前，必须按机器实测修改 `change_tool.cfg` 中 `_multitool_cfg` 的各工具停靠坞坐标、避让距离和速度。

钩子里只写机械运动，例如移动到坞口、进入坞位、横移、退出、等待运动队列清空等。不要在钩子里手动处理这些事项：

- 不要写 `current_tool` 或 `SAVE_VARIABLE VARIABLE=current_tool`。
- 不要调用 `SET_GCODE_OFFSET` 处理工具偏移。
- 不要手动处理换头统计。
- 不要手动做夹紧状态落盘或状态切换。
- 不要在钩子里嵌套调用 `Tn`、`CHANGE_TOOL`、`UNTOOL`。

主流程已经负责：

- 换头前保存 G-code 状态。
- 切换到 `accel_swap`。
- 抬升 `z_hop`。
- 若配置了 `extrude_compensation_length`，释放旧工具前在温度达到该 extruder 的 `min_extrude_temp` 后自动回抽同等长度。
- 调用释放和抓取钩子。
- 若配置了 `extrude_compensation_length`，抓取新工具并等温后在温度达到该 extruder 的 `min_extrude_temp` 后自动挤出补偿同等长度。
- 根据可选模块做夹紧检查、耗材检查、统计和偏移应用。
- 恢复 G-code 状态和原加速度。

自动回抽/补偿会临时使用相对挤出 (`M83`) 并依赖换头流程的 `RESTORE_GCODE_STATE` 恢复原始挤出模式；温度不足时只跳过该 E 动作，不中止换头。

### 3.4 偏移管理配置

启用 `[multitool_offsets]` 后，插件会在换头完成后自动调用 `SET_GCODE_OFFSET`：

```cfg
[multitool_offsets]
z_offset_adaptive: False
# save_prefix: t
```

字段说明：

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `z_offset_adaptive` | `False` | 开启后，每次打印首次使用的热端作为 Z 基准，后续热端使用相对 Z 差值，主要用于Tap/压力等接触式Z限位机器 |
| `save_prefix` | `t` | 偏移变量前缀 |

默认读取的保存变量名为：

```text
t0_offset_x / t0_offset_y / t0_offset_z
t1_offset_x / t1_offset_y / t1_offset_z
...
```

如果 `save_prefix: tool_`，则变量名变为 `tool_0_offset_x` 这类格式。

`z_offset_adaptive: True` 时，打印进入 `printing` 状态会重置基准热端，首次应用偏移的工具会成为本次打印的基准。基准热端会持久化到 `base_tool`。

### 3.5 夹紧检测配置

启用 `[multitool_clamp]` 后，插件会使用 Klipper `buttons` helper 读取一个夹紧开关：

```cfg
[multitool_clamp]
pin: ^!toolhead:TOOL_CLAMP
settle_ms: 50
```

字段说明：

| 字段 | 说明 |
|---|---|
| `pin` | 夹紧开关输入引脚，使用 Klipper 的 `^`、`!`、`~` 修饰符调整电平 |
| `settle_ms` | 每次校验前 `M400` 后额外等待的去抖时间 |

模块约定：

- `PRESSED = 已夹紧`
- `RELEASED = 已释放`

如果实际状态相反，请通过 `pin` 的 `!` 反相修饰符调整，不需要额外配置 `pressed_value`。

启用后主流程会自动检查：

- 入口校验：当前有热端时应为已夹紧，当前无热端时应为已释放。
- 释放旧热端后应为已释放。
- 抓取新热端后应为已夹紧。

排查命令：

```gcode
QUERY_CLAMP_STATUS
```

### 3.6 换热端过程 XY 防撞检测配置

启用 `[multitool_xy_guard]` 后，插件会在 `multitool_release_tool` 和 `multitool_pickup_tool` 两个换热端机械钩子运行期间监听 X/Y TMC DIAG：

```cfg
[multitool_xy_guard]
x_diag_pin: ^mcu:X_DIAG
y_diag_pin: ^mcu:Y_DIAG
settle_ms: 20
```

字段说明：

| 字段 | 说明 |
|---|---|
| `x_diag_pin` | X 轴 TMC DIAG 输入引脚，使用 Klipper 的 `^`、`!`、`~` 修饰符调整电平 |
| `y_diag_pin` | Y 轴 TMC DIAG 输入引脚，使用 Klipper 的 `^`、`!`、`~` 修饰符调整电平 |
| `settle_ms` | 校验时额外等待 buttons 回调的时间 |

模块约定 `PRESSED = DIAG 已触发`。如果实际状态相反，请通过 pin 的 `!` 反相修饰符调整。

StallGuard 触发阈值不在本模块设置，而是在 `[tmc2209 stepper_x]`、`[tmc2209 stepper_y]` 等 TMC 驱动配置中设置，例如 `driver_SGTHRS`；不同驱动型号使用对应的 StallGuard 参数。

触发后模块只会像夹紧检测一样输出错误并抛出 `command_error`，不会执行 `CANCEL_PRINT`、`G0/G1`、`G28` 或其他移动命令。它只检测换热端过程中的撞车或严重卡顿，不检测普通打印过程，也不检测已经发生但当前不再卡顿的 XY 偏移。

排查命令：

```gcode
QUERY_XY_GUARD_STATUS
```

### 3.7 换头统计配置

启用 `[multitool_stats]` 后，换头统计完全自动运行：

```cfg
[multitool_stats]
# persist_keys_prefix: tc_total_
# boot_banner_delay_s: 5.0
```

字段说明：

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `persist_keys_prefix` | `tc_total_` | 历史累计统计保存变量前缀 |
| `boot_banner_delay_s` | `5.0` | 启动后延迟输出历史累计提示的秒数，设为 `0` 可关闭 |

统计模块没有手动 G-code 命令。它会自动：

- 每次成功换头后累计次数、总耗时、释放阶段、抓取阶段、等温阶段耗时。
- 进入 `printing` 时重置本次打印统计。
- 打印结束、取消或错误时输出本次打印和历史累计统计。
- 每次成功换头后保存历史累计到 `[save_variables]`。

### 3.8 耗材检测与断料续打配置

启用 `[multitool_filament]` 后，每个工具头需要一个耗材检测 pin。通道数量直接复用 `[multitool] tool_count`。

```cfg
[multitool_filament]
boot_grace_s: 5
continuation_groups: [1,2],[0],[3]
runout_continue_length: 50
runout_continue_poll_s: 0.3
pin_0: ^multihotend:IO0
pin_1: ^multihotend:IO1
pin_2: ^multihotend:IO2
pin_3: ^multihotend:IO3
```

字段说明：

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `pin_0..pin_n` | 必填 | 每个工具头的耗材检测 pin |
| `boot_grace_s` | `5` | 启动后等待 buttons 上报状态的时间 |
| `continuation_groups` | 空 | 断料续打组，不配置则断料只提示 |
| `runout_continue_length` | `0` | 断料后继续消耗多少 mm 净送料再触发续打 / 暂停 |
| `runout_continue_poll_s` | `0.3` | 延后续打期间轮询挤出机位置的间隔 |
| `runout_event_delay` | `3` | 断料事件去抖窗口 |

模块约定：

- `PRESSED = 耗材已装载`
- `RELEASED = 耗材已卸载`

换头到某个工具前，主流程会自动检查对应通道是否有料。若目标工具在 `continuation_groups` 中且自身无料，主流程会按组顺序改用同组下一个有料工具，并把原目标工具的目标温度复制到实际使用的工具；只有该组所有其它工具也无料时才报错。未启用 `[multitool_filament]` 时，换头不会做耗材检查。

#### Spoolman 通道映射

该能力由 `[multitool]` 主模块提供，不依赖 `[multitool_filament]`。前端可通过 `SET_TOOL_SPOOL_ID` 为每个工具通道关联 Spoolman 料盘 ID：

```gcode
SET_TOOL_SPOOL_ID TOOL=0 SPOOL_ID=123
SET_TOOL_SPOOL_ID TOOL=0 SPOOL_ID=0   # 清除关联
```

映射会持久化到 `[save_variables]`，变量名为 `tool_0_spool_id`、`tool_1_spool_id` 等；`0` 表示未分配料盘。`QUERY_TOOL_STATUS` 会同时输出每个工具通道的 Spoolman ID。

`[multitool] sync_active_spool` 默认为开启。启用后，`T0..Tn`、`CHANGE_TOOL`、`UNTOOL` 或断料续打引发的当前工具变化都会通过 Moonraker 的 `spoolman_set_active_spool` 远程方法同步 Spoolman 当前料盘：切到已绑定通道时设置对应 spool id，卸下工具或切到未绑定通道时清空当前料盘，避免耗材用量继续记到上一卷料。该功能需要 Moonraker 启用 `[spoolman]`。

#### 打印前耗材检查

可在 `PRINT_START` 开头调用：

```cfg
[gcode_macro PRINT_START]
gcode:
    {% set tools = params.TOOLS|default('') %}
    CHECK_PRINT_FILAMENT TOOLS={tools}
    # 其余原有 PRINT_START 逻辑
```

切片器 start gcode 传入本次使用的工具列表，例如 OrcaSlicer / PrusaSlicer 可使用类似写法：

```gcode
PRINT_START TOOLS="{if is_extruder_used[0]}0,{endif}{if is_extruder_used[1]}1,{endif}{if is_extruder_used[2]}2,{endif}{if is_extruder_used[3]}3,{endif}"
```

命令行为：

- `CHECK_PRINT_FILAMENT TOOLS=0,1,2` 会检查指定通道。
- `TOOLS` 允许空白和尾随逗号。
- 任一指定通道明确无耗材时，命令报错并中止 `PRINT_START`。
- 通道状态未知时会警告，但按有耗材放行。

#### 断料续打

`continuation_groups` 的格式是 `[a,b,...],[c],[d]`，每个方括号是一组有序续打工具。

示例：

```cfg
continuation_groups: [1,2],[0],[3]
```

含义：

- T1 打印中断料时，优先切到 T2。
- T2 打印中断料时，环绕回查 T1。
- T1 断料后已经续打到 T2 时，后续切片器再次发 `T1` 会继续使用同组有料的 T2，并把 T1 的目标温度复制到 T2；只有 T1/T2 都无料才会报错。
- T0 和 T3 各自成组，没有其他可续打工具，断料时正常暂停。
- 没出现在任何组里的工具，断料时正常暂停。
- 同一个工具不能出现在多个组里。

断料触发后，框架会自动：

```text
PAUSE
M104 T<新热端> S<旧热端温度>
multitool_filament_before_swap FROM=<旧> TO=<新>   # 可选
CHANGE_TOOL T=<新>
M104 T<旧热端> S0
multitool_filament_after_swap FROM=<旧> TO=<新>    # 可选
RESUME
```

`multitool_filament_before_swap` 和 `multitool_filament_after_swap` 都是可选宏。前者适合写换头前的额外动作，后者适合写换头后的上料、排废、prime 动作。框架已自动复制温度、等温、关闭旧热端，并在 `RESUME` 后重新应用新热端偏移。若已启用 `[multitool] extrude_compensation_length`，请避免在 `multitool_filament_after_swap` 中再做重复过量挤出。

`runout_continue_length` 用于消耗传感器到喷嘴之间的残余耗材。设置为 `50` 表示断料信号出现后，继续打印到挤出机净送料增加 50 mm，再触发暂停或续打。中途如果手动暂停、补料、换头或打印结束，延后续打会自动取消。

排查命令：

```gcode
QUERY_FILAMENT_STATUS
```

### 3.9 自动对刀校准配置

`calibration.cfg` 会由安装脚本复制到：

```text
~/printer_data/config/multitool/calibration.cfg
```

它包含两部分：

- `[tools_calibrate]`：接触式对刀模块配置。
- `CALIBRATE_TOOL` / `CALIBRATE_ALL_TOOLS`：本仓库提供的校准编排宏。

基础配置示例：

```cfg
[tools_calibrate]
pin: ^PD11
travel_speed: 15
spread: 4.5
lower_z: 0.6
speed: 2
lift_speed: 4
final_lift_z: 1
sample_retract_dist: 2
samples_tolerance: 0.1
samples: 5
samples_result: median

[gcode_macro _TOOL_CALIB_VARS]
variable_sensor_x: 112.5
variable_sensor_y: -4
variable_safe_x: 112
variable_safe_y: 10.0
variable_safe_z: 10.0
variable_tool_count: 4
gcode:
```

必须按机器修改：

- `[tools_calibrate] pin`：对刀器 / 喷嘴接触传感器引脚。
- `variable_sensor_x` / `variable_sensor_y`：传感器中心坐标。
- `variable_safe_x` / `variable_safe_y` / `variable_safe_z`：校准前后移动用安全坐标。
- `variable_tool_count`：工具数量，应与 `[multitool] tool_count` 一致。

常用命令：

```gcode
CALIBRATE_TOOL TOOL=0
CALIBRATE_TOOL TOOL=1
CALIBRATE_ALL_TOOLS
```

校准逻辑：

- T0 作为基准，保存 `t0_offset_x/y/z = 0`。
- 其他工具通过接触式探测计算相对 T0 的 XYZ 偏移。
- 结果写入 `t{n}_offset_x/y/z`，可被 `[multitool_offsets]` 自动读取。

不需要自动对刀时，可以删除或不 include `calibration.cfg`；未声明 `[tools_calibrate]` 时，对刀模块不会加载。

## 4. 常用命令

| 命令 | 来源 | 说明 |
|---|---|---|
| `T0..T{n-1}` | `[multitool]` | 切换到对应工具 |
| `UNTOOL` | `[multitool]` | 卸下当前工具 |
| `CHANGE_TOOL T=<n>` | `[multitool]` | 切换工具，`T=-1` 表示卸下 |
| `M104 [T<n>] S<temp>` | `multitool_config.cfg` | 加热指定工具；未传 `T` 时使用当前工具，无当前工具时使用 T0 |
| `M109 [T<n>] S<temp>` | `multitool_config.cfg` | 先设温再等待实际工具到目标温度 ±1.5°C；低于 50°C 不等待 |
| `MULTITOOL_SET_TEMPERATURE TOOL=<n> S/R=<temp>` | `[multitool]` | 配置宏内部使用；解析续打组实际工具并调用原始 `M99104` |
| `MULTITOOL_WAIT_TEMPERATURE TOOL=<n> S/R=<temp>` | `[multitool]` | 配置宏内部使用；解析续打组实际工具并等待温度 |
| `QUERY_TOOL_STATUS` | `[multitool]` | 查询当前工具、持久化值、基准工具 |
| `QUERY_CLAMP_STATUS` | `[multitool_clamp]` | 查询夹紧开关状态 |
| `QUERY_XY_GUARD_STATUS` | `[multitool_xy_guard]` | 查询换热端过程 XY 防撞检测状态 |
| `QUERY_FILAMENT_STATUS` | `[multitool_filament]` | 查询各通道耗材和续打组 |
| `CHECK_PRINT_FILAMENT TOOLS=0,1` | `[multitool_filament]` | 打印前检查指定通道耗材 |
| `SET_TOOL_SPOOL_ID TOOL=<n> SPOOL_ID=<id>` | `[multitool]` | 设置通道的 Spoolman 料盘 ID，`0` 表示清除 |
| `CALIBRATE_TOOL TOOL=<n>` | `calibration.cfg` | 校准单个工具 |
| `CALIBRATE_ALL_TOOLS` | `calibration.cfg` | 依次校准全部工具 |
| `TOOL_LOCATE_SENSOR` | `[tools_calibrate]` | 用 T0 定位对刀传感器 |
| `TOOL_CALIBRATE_TOOL_OFFSET` | `[tools_calibrate]` | 测当前工具相对 T0 的偏移 |
| `TOOL_CALIBRATE_QUERY_PROBE` | `[tools_calibrate]` | 查询对刀探针状态 |

`[multitool_offsets]` 和 `[multitool_stats]` 没有手动命令，行为由换头流程和打印状态自动驱动。

## 5. 安装验证

安装并修改配置后，执行：

```gcode
FIRMWARE_RESTART
QUERY_TOOL_STATUS
```

确认输出里包含当前热端、持久化值、基准热端和工具数量。

然后按你的机器安全顺序测试：

```gcode
T0
QUERY_TOOL_STATUS
T1
QUERY_TOOL_STATUS
UNTOOL
QUERY_TOOL_STATUS
```

启用了可选模块时，再分别测试：

```gcode
QUERY_CLAMP_STATUS
QUERY_XY_GUARD_STATUS
QUERY_FILAMENT_STATUS
CHECK_PRINT_FILAMENT TOOLS=0,1
CALIBRATE_TOOL TOOL=0
```

首次测试建议降低运动速度、手放急停位置，并确认两个钩子宏的每一步机械动作都不会撞机。

## 6. 故障排查

| 现象 | 处理方式 |
|---|---|
| 启动报 `以下命令已被其他 section 注册` | 删除旧的 `[gcode_macro T0..Tn]`、`[gcode_macro UNTOOL]`、`[gcode_macro CHANGE_TOOL]` |
| 启动报 `[multitool]` 缺少 `tool_count` | 在 `[multitool]` 中填写 `tool_count` |
| 执行 `T0` 时报钩子未实现 | 替换默认 `multitool_release_tool` / `multitool_pickup_tool` 中的 `action_raise_error` |
| 新安装后启动报 `TODO_*` 或 pin 错误 | 填写 `multitool/multihotend.cfg` 中所有 `TODO_*` 占位 |
| CxChanger 方案报 `Unknown command "_release_tool"` | 确认 `multitool/change_tool.cfg` 存在，且 `printer.cfg` 包含 `[include multitool/*.cfg]` |
| CxChanger 换头坐标不对 | 修改 `change_tool.cfg` 中 `_multitool_cfg` 的 dock 坐标、避让距离和速度 |
| 夹紧状态相反 | 调整 `[multitool_clamp] pin` 的 `!` 反相修饰符，目标是 `PRESSED = 已夹紧` |
| 夹紧检测启动后状态未知 | buttons 只在电平变化时回调；检查接线和 pin 修饰符，必要时手动触发一次开关 |
| XY 防撞检测不触发或误触发 | 检查 DIAG 接线、pin 反相修饰符，以及 TMC StallGuard 阈值（如 `driver_SGTHRS`） |
| 换头前提示目标工具无耗材 | 检查 `[multitool_filament] pin_n` 接线、电平修饰符和实际装料状态 |
| `CHECK_PRINT_FILAMENT` 未检查任何通道 | 切片器没有传入 `TOOLS`，检查 start gcode 和 `PRINT_START` 参数转发 |
| 偏移没有生效 | 确认启用了 `[multitool_offsets]`，并且 `[save_variables]` 中存在 `t{n}_offset_x/y/z` |
| 重启后当前工具丢失 | 检查 `[save_variables]` 是否配置正确，变量文件是否可写 |
| 切到 T1/T2 后仍按 T0 温度报冷挤出 | 确认 `[multitool] sync_active_extruder` 未关闭，且存在对应 `[extruder1]`、`[extruder2]` section |
| `SET_PRESSURE_ADVANCE` 报 `Active extruder does not have a stepper` | 共用物理 E 步进且 T1..Tn 不带 stepper 时，在 `[multitool]` 设置 `default_pressure_advance_extruder: extruder`；独立多 E 步进机器应设置 `sync_extruder_motion: False` |
| 启动报 `M104` / `M109` 命令冲突 | 默认配置已覆写 `M104` / `M109`；删除其它同名 `[gcode_macro]`，或改用本模板中的版本 |
| 需要绕过温度命令覆写 | 直接调用原始命令 `M99104` / `M99109` |

## 7. 更新、迁移与许可证

### Moonraker 更新管理

可在 `moonraker.conf` 中加入：

```ini
[update_manager klipper-toolchange-stats]
type: git_repo
path: ~/klipper-toolchange-stats
origin: https://github.com/null01024/klipper-toolchange-stats.git
managed_services: klipper
primary_branch: main
install_script: install.sh
```

之后可以在 Mainsail / Fluidd 的更新管理中升级。安装脚本再次执行时也会尝试 fast-forward 更新当前分支；更新后会重新软链 extras，并保留已存在的用户配置文件。

### 旧版迁移

旧版如果使用过名称拼写错误的 `[multitoolr_stats]`，请改为：

```cfg
[multitool_stats]
```

历史累计字段默认仍是 `tc_total_*`，启用 `[multitool_stats]` 后会自动延续。新版统计由主模块自动调用，不再需要在换头宏中手动调用旧的计时命令。

如果从旧配置迁移到新版主模块，还需要删除旧的 `Tn`、`UNTOOL`、`CHANGE_TOOL` 宏，避免与 `[multitool]` 自动注册的命令冲突。

### 项目结构

```text
klipper-toolchange-stats/
├── install.sh
├── multitool_config.cfg
├── calibration.cfg
└── klipper/extras/
    ├── multitool.py
    ├── multitool_offsets.py
    ├── multitool_clamp.py
    ├── multitool_xy_guard.py
    ├── multitool_stats.py
    ├── multitool_filament.py
    └── tools_calibrate.py
```

### 许可证

`klipper/extras/tools_calibrate.py` vendoring 自 `viesturz/klipper-toolchanger`，遵循其原始 GPLv3 许可证。

本仓库新增内容默认 MIT。
