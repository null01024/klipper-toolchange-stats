# klipper-toolchange-stats

Klipper 换刀耗时统计扩展。统计每次换刀的总耗时、各阶段耗时（放下旧热端 / 拿取新热端 / 等待加热），按"本次打印"和"历史累计"两个维度汇总，并通过 `save_variables` 持久化。

## 安装

通过 SSH 在打印机上执行下面命令，会把仓库克隆到家目录并把 Python 模块软链到 Klipper 的 `extras` 目录：

```
wget -O - https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | bash
```

随后在 `printer.cfg` 中加入：

```
[toolchange_stats]
```

如果希望通过 Moonraker 自动更新，可在 `moonraker.conf` 中加入：

```
[update_manager klipper-toolchange-stats]
type: git_repo
path: ~/klipper-toolchange-stats
origin: https://github.com/null01024/klipper-toolchange-stats.git
managed_services: klipper
primary_branch: main
```

更新后若有新的 Python 文件，需要再执行一次安装脚本以创建软链：

```
bash ~/klipper-toolchange-stats/install.sh
```

## 命令

终端任意时刻可以执行 `TOOLCHANGE_STATS_HELP` 查看完整命令列表。核心命令：

- `TOOLCHANGE_TIMER_BEGIN` / `TOOLCHANGE_TIMER_END`：单次换刀计时起止（END 自动累加并持久化）
- `TOOLCHANGE_STAGE_BEGIN STAGE=release|pickup|heat_wait` / `TOOLCHANGE_STAGE_END STAGE=...`：阶段计时
- `TOOLCHANGE_STATS_RESET_PRINT`：重置本次打印统计（PRINT_START 时调用）
- `TOOLCHANGE_STATS_RESET_TOTAL`：重置历史累计（慎用）
- `TOOLCHANGE_STATS_REPORT [SCOPE=current|print|total|all]`：输出统计报告

## 数据模型

| 维度 | 内容 | 生命周期 |
|---|---|---|
| `current` | 当前进行中的一次换刀 (各阶段 elapsed) | 单次换刀 |
| `print` | 本次打印累计 (次数 / 总耗时 / 各阶段累计) | `PRINT_START` → `PRINT_END` |
| `total` | 历史累计 (跨打印持久化) | 永久 |

模板可读 `printer.toolchange_stats.tc_print.count`、`tc_total.stages.heat_wait` 等字段。

## 集成示例

在 `change_tool` 宏内：

```
TOOLCHANGE_TIMER_BEGIN

# 释放旧刀
TOOLCHANGE_STAGE_BEGIN STAGE=release
_release_tool TOOL=...
TOOLCHANGE_STAGE_END STAGE=release

# 抓取新刀
TOOLCHANGE_STAGE_BEGIN STAGE=pickup
_pickup_tool TOOL=...
TOOLCHANGE_STAGE_END STAGE=pickup

# 等待加热（可选）
TOOLCHANGE_STAGE_BEGIN STAGE=heat_wait
TEMPERATURE_WAIT SENSOR=... MINIMUM=...
TOOLCHANGE_STAGE_END STAGE=heat_wait

TOOLCHANGE_TIMER_END
```

在 `PRINT_START` 中调用 `TOOLCHANGE_STATS_RESET_PRINT`，`PRINT_END` 中调用 `TOOLCHANGE_STATS_REPORT SCOPE=all` 即可。

## 仓库结构

```
klipper-toolchange-stats/
├── install.sh
├── README.md
└── klipper/
    └── extras/
        └── toolchange_stats.py
```
