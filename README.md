# klipper-toolchange-stats

通用的 Klipper **多热端 / 多头切换插件**。一站式提供：换头编排、夹紧检测、
偏移管理、换头计时统计。把"机型无关的编排"全部托管在 Python extras 里，
用户只需写**两个钩子宏**即可接入任意多头机器。

> 本仓库由旧版 `klipper-toolchange-stats`（仅含 `[multitool_stats]` 计时统计）
> 演进而来。计时模块已合并为 `[multitool_stats]`，**历史持久化数据自动延续**。

---

## 目录

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [配置文件说明](#配置文件说明)
- [验证安装](#验证安装)
- [命令一览](#命令一览)
- [自动对刀校准](#自动对刀校准tools_calibrate)
- [moonraker 自动更新](#moonraker-自动更新可选)
- [旧版用户迁移](#旧版用户迁移)
- [故障排查](#故障排查)
- [项目结构](#项目结构)

---

## 功能特性

| 模块 | 必填 | 作用 |
|---|---|---|
| `[multitool]` | ✅ | 主模块，自动注册 `T0..T{n-1}` / `UNTOOL` / `CHANGE_TOOL`，并维护 `current_tool` 状态恢复 |
| `[multitool_offsets]` | 可选 | 各热端 XYZ 偏移管理，支持 Z 自适应基准热端 |
| `[multitool_clamp]` | 可选 | 基于 buttons helper 的夹紧检测，自动前后置校验 |
| `[multitool_stats]` | 可选 | 换头计时统计，零钩子嵌入 |
| `[multitool_filament]` | 可选 | 各热端耗材检测；换头前校验，**支持断料续打**（断料自动切到同组下一个有料热端） |
| `[tools_calibrate]` | 可选 | 喷嘴接触式自动对刀校准（来自上游 viesturz/klipper-toolchanger），配合 `CALIBRATE_TOOL` 宏自动写入各热端偏移 |

亮点：

- **声明即启用**：在 `printer.cfg` 中写一行 `[multitool_xxx]` 即开启对应能力。
- **极简钩子**：只实现 `multitool_release_tool` / `multitool_pickup_tool` 两个宏，写纯机械动作即可，夹紧检测/计时/偏移/落盘**全部由插件自动嵌入**。
- **T\* 自动注册**：`tool_count: 4` 即注册 T0~T3；改一个数字即可适配 8 头机器，无需手写 `[gcode_macro Tn]`。
- **状态外露**：`printer.multitool.current_tool` 等可被 Mainsail / Fluidd / 任意宏直接读取。
- **不重写 M109**：M109 处理保留给用户自行决定（如需"无 T 时作用于当前热端"，自行写宏覆盖）。

---

## 快速开始

### 前置条件

1. 已经安装并运行 Klipper（任何近版本）。
2. `printer.cfg` 中配置了 `[save_variables]`（用于持久化 current_tool / 偏移 / 统计）：

   ```cfg
   [save_variables]
   filename: ~/printer_data/config/myvariables.cfg
   ```

### 一键安装（推荐）

通过 SSH 在打印机上执行：

```bash
wget -O - https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | bash
```

### 手动安装

```bash
git clone https://github.com/null01024/klipper-toolchange-stats ~/klipper-toolchange-stats
cd ~/klipper-toolchange-stats
./install.sh
```

`install.sh` 会自动：
- 软链 `klipper/extras/*.py` 到 `~/klipper/klippy/extras/`
- 把默认配置 [multitool_config.cfg](multitool_config.cfg) 复制到 `~/printer_data/config/multitool/`（已存在则保留不覆盖）
- 在 `printer.cfg` 顶部插入 `[include multitool/*.cfg]`（已存在则跳过；首次注入会备份为 `printer.cfg.bak.multitool`）
- 重启 `klipper` 服务

> 默认路径 `KLIPPER_PATH=~/klipper`、`INSTALL_PATH=~/klipper-toolchange-stats`、`CONFIG_PATH=~/printer_data/config`，可通过环境变量覆盖。

---

## 配置文件说明

安装脚本部署的 `~/printer_data/config/multitool/multitool_config.cfg` 内容如下（节选）：

```cfg
#####################################################################
# 1. 插件 section（声明即启用）
#####################################################################
[multitool]
tool_count: 4
z_hop: 0.4
feed_z: 600
accel_swap: 8000
untool_safe_z: 10        # 上次为"无热端"时第一动作要抬到的安全 Z

# 偏移管理（可选）
[multitool_offsets]
z_offset_adaptive: False

# 夹紧检测（可选）—— 不需要时整段删除
[multitool_clamp]
pin: ^!toolhead:TOOL_CLAMP
settle_ms: 50

# 换头计时统计（可选）
[multitool_stats]


#####################################################################
# 2. 用户钩子：放回热端  (默认实现 = 报错，必须替换)
#####################################################################
[gcode_macro multitool_release_tool]
gcode:
    {% set tool = params.TOOL|int %}
    {action_raise_error(
        "[multitool] 钩子 multitool_release_tool 未实现！...")}

    # ====== 删除上方 action_raise_error 后填入你的动作 ======
    # G90
    # G0 X... Y... F...
    # M400


#####################################################################
# 3. 用户钩子：抓取热端  (默认实现 = 报错，必须替换)
#####################################################################
[gcode_macro multitool_pickup_tool]
gcode:
    {% set tool = params.TOOL|int %}
    {action_raise_error(
        "[multitool] 钩子 multitool_pickup_tool 未实现！...")}

    # ====== 删除上方 action_raise_error 后填入你的动作 ======
    # G90
    # G0 X... Y... F...
    # M400
```

### 你需要做什么

1. **修改 `[multitool]` 字段**：按你的机器调整 `tool_count` / `z_hop` / `accel_swap` 等。
2. **替换两个钩子宏**：默认实现会直接 `action_raise_error` 报错——这是故意设计的，强制你在使用前完成钩子实现。
   - `multitool_release_tool`：写"把 T{tool} 热端放回坞"的运动序列
   - `multitool_pickup_tool`：写"从坞中抓起 T{tool} 热端"的运动序列
3. 不需要的子模块（如 `[multitool_clamp]` / `[multitool_offsets]` / `[multitool_stats]`）整段删除即可。

### 字段含义

各 section 详细字段（默认值、可选项、状态对象、持久化字段）见
[`multitool_config.cfg`](multitool_config.cfg) 内的注释，以及源码 [`klipper/extras/`](klipper/extras/) 各模块开头的 docstring。

### 钩子里**不要**做的事

- ❌ 调 `_assert_clamp` / `_tc_clamp_*`（框架已自动前后置）
- ❌ 调 `SET_GCODE_OFFSET`（框架统一处理偏移）
- ❌ 调 `TOOLCHANGE_TIMER_*` / `TOOLCHANGE_STAGE_*`（框架自动调）
- ❌ 写 `current_tool`（落盘是框架的职责）

---

## 验证安装

### 1) 重启确认

```
FIRMWARE_RESTART
QUERY_TOOL_STATUS
```

输出应包含：当前热端编号 / 持久化值 / 工具数量。

### 2) 完整链路

```
T0
T1
UNTOOL
T2
QUERY_CLAMP_STATUS         # 启用了 [multitool_clamp] 时
```

---

## 命令一览

| 来源 | 命令 | 说明 |
|---|---|---|
| `[multitool]` | `T0..T{n-1}` | 切换到对应热端 |
| `[multitool]` | `UNTOOL` | 卸下当前热端 |
| `[multitool]` | `CHANGE_TOOL T=<n>` | 兼容入口（`-1` 表示卸下） |
| `[multitool]` | `QUERY_TOOL_STATUS` | 查询当前热端 / 持久化值 |
| `[multitool_clamp]` | `QUERY_CLAMP_STATUS` | 查询夹紧开关状态 |
| `[multitool_offsets]` | — | 完全自动，无对外命令 |
| `[multitool_stats]` | — | 完全自动，无对外命令 |
| `[multitool_filament]` | `QUERY_FILAMENT_STATUS` | 查询各通道耗材装载状态与续打组 |
| `[multitool_filament]` | `CHECK_PRINT_FILAMENT TOOLS=<list>` | 打印前检查 `TOOLS` 指定通道是否都有耗材，缺料则报错中止打印 |
| `calibration.cfg` | `CALIBRATE_TOOL TOOL=<n>` | 校准单个工具（T0 设基准，其余测相对偏移并落盘） |
| `calibration.cfg` | `CALIBRATE_ALL_TOOLS` | 批量校准全部工具 |
| `[tools_calibrate]` | `TOOL_LOCATE_SENSOR` | 定位对刀传感器中心（用 T0 调用） |
| `[tools_calibrate]` | `TOOL_CALIBRATE_TOOL_OFFSET` | 测当前工具相对 T0 的偏移 |

---

## 打印前耗材检查（CHECK_PRINT_FILAMENT）

打印开始前，先确认本次任务用到的每个通道都装好了料：缺料就直接报错中止，
避免打到一半才发现某通道没料。

### 命令

```
CHECK_PRINT_FILAMENT TOOLS=0,1,2
```

- `TOOLS`：本次打印用到的通道列表，逗号分隔（允许尾随逗号 / 空白）。
- 逐通道输出状态总览（`已装载 / 已卸载 / 未知`）。
- 任一所需通道明确**无耗材** → 抛错，使调用它的 `PRINT_START` 宏中断，打印不会开始。
- 通道状态**未知**（启动后从未上报电平，实际打印时几乎不出现）→ 仅警告、不阻塞。
- `TOOLS` 为空 → 跳过检查（兼容单色 / 未传参）。

### 与切片器 / PRINT_START 集成

切片器（OrcaSlicer / PrusaSlicer 等）的 start gcode 把用到的通道拼成 `TOOLS`：

```gcode
PRINT_START ... TOOLS="{if is_extruder_used[0]}0,{endif}{if is_extruder_used[1]}1,{endif}{if is_extruder_used[2]}2,{endif}{if is_extruder_used[3]}3,{endif}"
```

在你自己的 `PRINT_START` 宏开头加一行（仅一行，不影响原有逻辑）：

```cfg
[gcode_macro PRINT_START]
gcode:
    {% set tools = params.TOOLS|default('') %}
    CHECK_PRINT_FILAMENT TOOLS={tools}
    # ... 其余原有逻辑 ...
```

---

## 断料续打（continuation_groups）

`[multitool_filament]` 在为每个热端注册耗材开关的同时，支持**断料续打**：
打印中当前热端断料时，自动切到同组的下一个有料热端继续打印；同组没有可用
热端时正常暂停。

### 配置续打组

在 `[multitool_filament]` 下加一行 `continuation_groups`，格式 `[a,b,...],[c],[d]`：
每个方括号是一个**有序**续打组。

```cfg
[multitool_filament]
continuation_groups: [1,2],[0],[3]
pin_0: ^multihotend:IO0
pin_1: ^multihotend:IO1
pin_2: ^multihotend:IO2
pin_3: ^multihotend:IO3
```

含义（对应上面的 `[1,2],[0],[3]`）：

- T1 打印中断料 → 自动续打到 T2。
- T2 打印中断料 → 环绕回查 T1（若已补料则续打，否则暂停）。
- T0 自成一组，没有可切换对象 → 断料直接暂停打印。
- T3 同理，自成一组 → 断料直接暂停打印。
- 没列进任何组的热端 → 断料直接暂停打印。
- 不配置 `continuation_groups` → 断料只在控制台打印提示，不自动续打/暂停（向后兼容）。

> 同一个热端不能出现在多个组里（语义二义，启动会报错）。
> 组内查找用 `skip` 语义：跳过同样没料的成员，全组都没料才暂停。

### 断料后延后续打（runout_continue_length）

断料传感器多装在料管入口，触发时传感器到喷嘴之间的料管里仍有一段可用耗材。
默认行为是断料**立即**触发暂停/续打，这段残料会被浪费。配置
`runout_continue_length`（mm）后，断料触发不立即处理，而是**让打印继续**，
直到挤出机净送料达到该长度，再触发原有的暂停/续打流程，从而把料管里的残料用完。

```cfg
[multitool_filament]
continuation_groups: [1,2],[0],[3]
runout_continue_length: 50       # 断料后再消耗 50mm 耗材才触发续打/暂停 (0=立即)
# runout_continue_poll_s: 0.3    # 延后期间轮询挤出机位置的间隔(秒)，默认 0.3
pin_0: ^multihotend:IO0
```

- **测量方式**：用挤出机轴（toolhead E）绝对坐标的增量，即**净送料量**（回抽会
  自动抵消）。该值在打印过程中连续累积，不受切片器每层 `G92 E0` 影响。
- 它是运动规划的**命令位置**（含前瞻缓冲），相对喷嘴实际出料略提前一点点，
  无需非常精确的场景足够用。
- 默认 `0` = 关闭，行为与旧版完全一致（立即触发）。
- 仅在配置了 `continuation_groups` 时生效（与断料处理同一条路径）。
- 延后期间若**手动暂停 / 补料 / 换头 / 打印结束**，会自动取消倒计时。
- `runout_continue_length` 不要超过料管实际残余长度，否则会空打（喷嘴抽空）。

> 可用 `QUERY_FILAMENT_STATUS` 查看当前延后续打长度配置。

### 续打编排与可选钩子

触发续打时，框架自动按下面顺序编排（先暂停，再判断）：

```
PAUSE
→ M104 T<新热端> S<旧热端温度>                  # 框架自动：复制旧热端温度到新热端
→ [multitool_filament_before_swap FROM TO]   # 可选钩子
→ CHANGE_TOOL T=<下一个热端>                   # 含等温（此时新热端已有目标温度）
→ M104 T<旧热端> S0                            # 框架自动：换头后关闭旧热端
→ [multitool_filament_after_swap FROM TO]    # 可选钩子
→ RESUME
```

新热端的加热**无需手写钩子**：框架会自动把旧热端的目标温度复制到新热端，
随后的 `CHANGE_TOOL` 会等温；换头完成后旧热端会被自动关闭（设为 0）。

每热端的 **gcode 偏移也无需手写钩子**：续打用 `PAUSE`/`RESUME` 包裹，而
`RESUME` 的 `RESTORE_GCODE_STATE` 会把偏移还原成暂停时（旧热端）的值，框架
已在 `RESUME` 之后自动重新应用新热端偏移加以抵消（需启用 `[multitool_offsets]`）。

> 注意：`RESTORE_GCODE_STATE` **不会**恢复加速度/速度限制（`SET_VELOCITY_LIMIT`
> 的 `max_accel` 等属于 toolhead 层，不在 gcode state 内）。为此框架已在续打结尾
> 自动用 `SET_VELOCITY_LIMIT` 把加速度/速度限制写回断料前的值作为兜底，钩子内的
> 临时改动不会残留到续打后的打印（仍建议钩子尽量不改）。

两个钩子宏都是**可选**的（未定义则跳过），入参 `FROM=<旧热端> TO=<新热端>`：

- `multitool_filament_before_swap`：换头**前**的额外动作（框架已自动设温/等温，无需再设温）。
- `multitool_filament_after_swap`：换头**后**、`RESUME` 前。常用于新热端
  上料 / 吹料 / 排废 / Prime，确保续打前出料正常。

> 默认配置 [multitool_config.cfg](multitool_config.cfg) 中给出了两个钩子的注释模板。

---

## 自动对刀校准（tools_calibrate）

本项目内置了喷嘴接触式自动对刀能力，由两部分组成：

- `klipper/extras/tools_calibrate.py`：**vendoring 自上游
  [viesturz/klipper-toolchanger](https://github.com/viesturz/klipper-toolchanger)**
  （`klipper/extras/tools_calibrate.py`，GPLv3）。自包含，仅依赖 Klipper 标准对象，
  **不需要**上游的 `toolchanger.py` / `tool.py` 等其它文件。
- [calibration.cfg](calibration.cfg)：本仓库提供的校准编排宏（`CALIBRATE_TOOL` /
  `CALIBRATE_ALL_TOOLS`），`install.sh` 会自动把它部署到
  `~/printer_data/config/multitool/`，由 `[include multitool/*.cfg]` 自动加载。

### 工作流程

1. 编辑 `calibration.cfg`：替换 `[tools_calibrate] pin` 为你的对刀器引脚，并按机器
   实测填写 `_TOOL_CALIB_VARS` 里的传感器中心坐标、安全坐标、`tool_count`。
2. 执行 `CALIBRATE_ALL_TOOLS`（或逐个 `CALIBRATE_TOOL TOOL=<n>`）。
3. T0 作为基准（偏移锁 0），其余工具测出相对 T0 的 XYZ 偏移。

### 与偏移系统的衔接

校准结果通过 `SAVE_VARIABLE` 写入 `t{n}_offset_x/y/z`，这正是
[`klipper/extras/multitool_offsets.py`](klipper/extras/multitool_offsets.py)
读取偏移所用的字段（默认前缀 `t`）。因此**校准完成后无需手动搬运数据**，启用
`[multitool_offsets]` 即可在换头时自动应用各热端偏移。

> 不需要对刀校准时，删除 `multitool/calibration.cfg` 即可；`tools_calibrate.py`
> 不被任何 cfg 引用时不会加载，无副作用。

---

## moonraker 自动更新（可选）

在 `moonraker.conf` 中添加：

```ini
[update_manager klipper-toolchange-stats]
type: git_repo
path: ~/klipper-toolchange-stats
origin: https://github.com/null01024/klipper-toolchange-stats.git
managed_services: klipper
primary_branch: main
install_script: install.sh
```

之后即可在 Mainsail / Fluidd 的 Update Manager 一键升级。

---

## 旧版用户迁移

之前在用 `[multitool_stats]`（仅计时统计的旧版）的用户：

1. 把 `printer.cfg` 中的 `[multitool_stats]` 改为 `[multitool_stats]`。
2. 重新跑一次 `install.sh`（旧的 `multitool_stats.py` 已被新的 `multitool_stats.py` 取代）。
3. 持久化字段 `tc_total_*` 保持不变，**历史累计数据自动延续**。
4. 不再需要在 `change_tool` 中手动调 `TOOLCHANGE_TIMER_*` —— 主模块会自动嵌入计时。

---

## 故障排查

| 现象 | 解决方法 |
|---|---|
| 启动报 `[multitool] 以下命令已被其他 section 注册...` | 删除 `printer.cfg` 中残留的 `[gcode_macro T0..Tn]` / `[gcode_macro UNTOOL]` / `[gcode_macro CHANGE_TOOL]` |
| 启动报 `section 'multitool' has no field 'tool_count'` | `tool_count` 必填 |
| 切换报"夹紧检测未收到任何状态上报" | buttons helper 只在电平变化时回调；先手动按一下夹紧开关或 `QUERY_CLAMP_STATUS` 触发；检查 `[multitool_clamp] pin:` 是否正确 |
| 切换报夹紧自检失败 | 检查 pin 的电平修饰符（用 `!` 反相）；不要同时声明 `[gcode_button tool_clamp]` 与 `[multitool_clamp]` 共用同一 pin |
| `M109 S200` 不带 T 时不作用于当前热端 | 本插件**不重写** M109。如需此行为，自行写 `[gcode_macro M109] rename_existing: M99109`，根据 `printer.multitool.current_tool` 把 T 补全后调 `M99109` |
| 重启后 `current_tool` 丢失 | 检查 `[save_variables]` 是否正确配置 |

---

## 项目结构

```
klipper-toolchange-stats/
├── install.sh                          # 软链 .py 到 ~/klipper/klippy/extras/
│                                       # 并把默认配置部署到用户配置目录
├── multitool_config.cfg               # 默认配置（含两个钩子的报错占位实现）
├── calibration.cfg                    # 对刀校准编排宏（可选，自动部署）
└── klipper/extras/
    ├── multitool.py                  # 主模块（T*/UNTOOL/CHANGE_TOOL 编排）
    ├── multitool_clamp.py            # 夹紧检测（可选）
    ├── multitool_offsets.py          # 偏移 + Z 自适应（可选）
    ├── multitool_filament.py         # 耗材检测 + 断料续打（可选）
    ├── multitool_stats.py            # 换头计时统计（可选）
    └── tools_calibrate.py            # 自动对刀校准（vendoring 自 viesturz/klipper-toolchanger, GPLv3）
```

---

## License

仓库内未声明的源码遵循其原始上游许可证；本仓库新增内容默认 MIT。

`klipper/extras/tools_calibrate.py` vendoring 自
[viesturz/klipper-toolchanger](https://github.com/viesturz/klipper-toolchanger)，
遵循其原始 **GPLv3** 许可证，版权归原作者所有。
