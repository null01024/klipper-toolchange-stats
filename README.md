# klipper-toolchange-stats

通用的 Klipper **多热端 / 多头切换插件**。一站式提供：换头编排、夹紧检测、
偏移管理、换头计时统计。把"机型无关的编排"全部托管在 Python extras 里，
用户只需写**两个钩子宏**即可接入任意多头机器。

> 本仓库由旧版 `klipper-toolchange-stats`（仅含 `[toolchange_stats]` 计时统计）
> 演进而来。计时模块已合并为 `[toolchanger_stats]`，**历史持久化数据自动延续**。

---

## 目录

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [配置文件说明](#配置文件说明)
- [验证安装](#验证安装)
- [命令一览](#命令一览)
- [moonraker 自动更新](#moonraker-自动更新可选)
- [旧版用户迁移](#旧版用户迁移)
- [故障排查](#故障排查)
- [项目结构](#项目结构)

---

## 功能特性

| 模块 | 必填 | 作用 |
|---|---|---|
| `[toolchanger]` | ✅ | 主模块，自动注册 `T0..T{n-1}` / `UNTOOL` / `CHANGE_TOOL`，并维护 `current_tool` 状态恢复 |
| `[toolchanger_offsets]` | 可选 | 各热端 XYZ 偏移管理，支持 Z 自适应基准热端 |
| `[toolchanger_clamp]` | 可选 | 基于 buttons helper 的夹紧检测，自动前后置校验 |
| `[toolchanger_stats]` | 可选 | 换头计时统计，零钩子嵌入 |

亮点：

- **声明即启用**：在 `printer.cfg` 中写一行 `[toolchanger_xxx]` 即开启对应能力。
- **极简钩子**：只实现 `toolchanger_release_tool` / `toolchanger_pickup_tool` 两个宏，写纯机械动作即可，夹紧检测/计时/偏移/落盘**全部由插件自动嵌入**。
- **T\* 自动注册**：`tool_count: 4` 即注册 T0~T3；改一个数字即可适配 8 头机器，无需手写 `[gcode_macro Tn]`。
- **状态外露**：`printer.toolchanger.current_tool` 等可被 Mainsail / Fluidd / 任意宏直接读取。
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
- 把默认配置 [toolchange_config.cfg](toolchange_config.cfg) 复制到 `~/printer_data/config/toolchange/`（已存在则保留不覆盖）
- 在 `printer.cfg` 顶部插入 `[include toolchange/*.cfg]`（已存在则跳过；首次注入会备份为 `printer.cfg.bak.toolchange`）
- 重启 `klipper` 服务

> 默认路径 `KLIPPER_PATH=~/klipper`、`INSTALL_PATH=~/klipper-toolchange-stats`、`CONFIG_PATH=~/printer_data/config`，可通过环境变量覆盖。

---

## 配置文件说明

安装脚本部署的 `~/printer_data/config/toolchange/toolchange_config.cfg` 内容如下（节选）：

```cfg
#####################################################################
# 1. 插件 section（声明即启用）
#####################################################################
[toolchanger]
tool_count: 4
z_hop: 0.4
feed_z: 600
accel_toolchange: 8000
untool_safe_z: 10        # 上次为"无热端"时第一动作要抬到的安全 Z

# 偏移管理（可选）
[toolchanger_offsets]
z_offset_adaptive: False

# 夹紧检测（可选）—— 不需要时整段删除
[toolchanger_clamp]
pin: ^!toolhead:TOOL_CLAMP
settle_ms: 50

# 换头计时统计（可选）
[toolchanger_stats]


#####################################################################
# 2. 用户钩子：放回热端  (默认实现 = 报错，必须替换)
#####################################################################
[gcode_macro toolchanger_release_tool]
gcode:
    {% set tool = params.TOOL|int %}
    {action_raise_error(
        "[toolchanger] 钩子 toolchanger_release_tool 未实现！...")}

    # ====== 删除上方 action_raise_error 后填入你的动作 ======
    # G90
    # G0 X... Y... F...
    # M400


#####################################################################
# 3. 用户钩子：抓取热端  (默认实现 = 报错，必须替换)
#####################################################################
[gcode_macro toolchanger_pickup_tool]
gcode:
    {% set tool = params.TOOL|int %}
    {action_raise_error(
        "[toolchanger] 钩子 toolchanger_pickup_tool 未实现！...")}

    # ====== 删除上方 action_raise_error 后填入你的动作 ======
    # G90
    # G0 X... Y... F...
    # M400
```

### 你需要做什么

1. **修改 `[toolchanger]` 字段**：按你的机器调整 `tool_count` / `z_hop` / `accel_toolchange` 等。
2. **替换两个钩子宏**：默认实现会直接 `action_raise_error` 报错——这是故意设计的，强制你在使用前完成钩子实现。
   - `toolchanger_release_tool`：写"把 T{tool} 热端放回坞"的运动序列
   - `toolchanger_pickup_tool`：写"从坞中抓起 T{tool} 热端"的运动序列
3. 不需要的子模块（如 `[toolchanger_clamp]` / `[toolchanger_offsets]` / `[toolchanger_stats]`）整段删除即可。

### 字段含义

各 section 详细字段（默认值、可选项、状态对象、持久化字段）见
[`toolchange_config.cfg`](toolchange_config.cfg) 内的注释，以及源码 [`klipper/extras/`](klipper/extras/) 各模块开头的 docstring。

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
QUERY_CLAMP_STATUS         # 启用了 [toolchanger_clamp] 时
```

---

## 命令一览

| 来源 | 命令 | 说明 |
|---|---|---|
| `[toolchanger]` | `T0..T{n-1}` | 切换到对应热端 |
| `[toolchanger]` | `UNTOOL` | 卸下当前热端 |
| `[toolchanger]` | `CHANGE_TOOL T=<n>` | 兼容入口（`-1` 表示卸下） |
| `[toolchanger]` | `QUERY_TOOL_STATUS` | 查询当前热端 / 持久化值 |
| `[toolchanger_clamp]` | `QUERY_CLAMP_STATUS` | 查询夹紧开关状态 |
| `[toolchanger_offsets]` | — | 完全自动，无对外命令 |
| `[toolchanger_stats]` | — | 完全自动，无对外命令 |

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

之前在用 `[toolchange_stats]`（仅计时统计的旧版）的用户：

1. 把 `printer.cfg` 中的 `[toolchange_stats]` 改为 `[toolchanger_stats]`。
2. 重新跑一次 `install.sh`（旧的 `toolchange_stats.py` 已被新的 `toolchanger_stats.py` 取代）。
3. 持久化字段 `tc_total_*` 保持不变，**历史累计数据自动延续**。
4. 不再需要在 `change_tool` 中手动调 `TOOLCHANGE_TIMER_*` —— 主模块会自动嵌入计时。

---

## 故障排查

| 现象 | 解决方法 |
|---|---|
| 启动报 `[toolchanger] 以下命令已被其他 section 注册...` | 删除 `printer.cfg` 中残留的 `[gcode_macro T0..Tn]` / `[gcode_macro UNTOOL]` / `[gcode_macro CHANGE_TOOL]` |
| 启动报 `section 'toolchanger' has no field 'tool_count'` | `tool_count` 必填 |
| 切换报"夹紧检测未收到任何状态上报" | buttons helper 只在电平变化时回调；先手动按一下夹紧开关或 `QUERY_CLAMP_STATUS` 触发；检查 `[toolchanger_clamp] pin:` 是否正确 |
| 切换报夹紧自检失败 | 检查 pin 的电平修饰符（用 `!` 反相）；不要同时声明 `[gcode_button tool_clamp]` 与 `[toolchanger_clamp]` 共用同一 pin |
| `M109 S200` 不带 T 时不作用于当前热端 | 本插件**不重写** M109。如需此行为，自行写 `[gcode_macro M109] rename_existing: M99109`，根据 `printer.toolchanger.current_tool` 把 T 补全后调 `M99109` |
| 重启后 `current_tool` 丢失 | 检查 `[save_variables]` 是否正确配置 |

---

## 项目结构

```
klipper-toolchange-stats/
├── install.sh                          # 软链 .py 到 ~/klipper/klippy/extras/
│                                       # 并把默认配置部署到用户配置目录
├── toolchange_config.cfg               # 默认配置（含两个钩子的报错占位实现）
└── klipper/extras/
    ├── toolchanger.py                  # 主模块（T*/UNTOOL/CHANGE_TOOL 编排）
    ├── toolchanger_clamp.py            # 夹紧检测（可选）
    ├── toolchanger_offsets.py          # 偏移 + Z 自适应（可选）
    └── toolchanger_stats.py            # 换头计时统计（可选）
```

---

## License

仓库内未声明的源码遵循其原始上游许可证；本仓库新增内容默认 MIT。
