# klipper-toolchange-stats

这是一个 Klipper 多热端 / 多工具头换头插件。它负责注册 `T0`、`T1` 这类换头命令，并在换头时自动处理当前工具状态、偏移、温度等待、耗材检查、断料续打、换头统计等流程。

配合 [mainsail-toolchanger](https://github.com/null01024/mainsail-toolchanger) / [fluidd-toolchange](https://github.com/null01024/fluidd-toolchanger) 网页前端后，可以在 Mainsail / Fluidd 中直观看到多工具头状态、耗材状态和换头统计：

![mainsail-toolchanger 前端预览](img/web_preview.png)

![fluidd-toolchanger 前端预览](img/fluidd_preview.png)

这份 README 面向第一次安装的用户，重点说明怎么安装、安装后要改哪些配置、怎么验证能不能正常工作。

## 适合谁使用

适合：

- Klipper 多热端机器。
- Klipper 多工具头机器。
- 希望用 `T0`、`T1`、`T2` 等命令切换工具。
- 希望把换头流程、偏移、耗材检测、断料续打、统计集中到插件里管理。

不适合：

- 普通单热端机器。
- 还没有完成基础 Klipper 配置、不能正常归零和加热的机器。

## ZDT EMM42 闭环位置误差监控

仓库中的 `klipper/extras/zdt_emm42.py` 可通过 SocketCAN 读取 ZDT EMM42_V5 闭环驱动器的运行参数，并在 Mainsail Dashboard 显示位置误差实时曲线。它不接管 Klipper 的 STEP/DIR/EN 运动控制，只发送读取命令。

### Klipper 配置

将插件安装到 `klippy/extras` 后，在 `printer.cfg` 中加入类似配置：

```ini
[zdt_emm42 shadow_a]
can_interface: can0
addr: 1
can_payload_includes_addr: False
can_filter: ext
checksum_mode: 0x6B
poll_interval: 0.10
error_poll_interval: 0.10
query_timeout: 0.006
offline_timeout: 1.0
rotation_distance: 40
microsteps: 16
full_steps_per_rotation: 200
# csv_path: /tmp/zdt_emm42_shadow_a.csv
```

`error_poll_interval` 是独立的 `0x37` 位置误差采样周期，默认约 100 ms；它不会再受其它读取命令轮询列表影响。`poll_interval` 仍用于电压、电流、位置和状态标志等普通遥测。`offline_timeout` 内没有收到有效的 `0x37` 响应时，状态会变为离线。

固定校验模式下，地址为 `1` 的位置误差请求是串口逻辑报文 `01 37 6B`；CAN 扩展帧使用 `CAN_ID=0x0100`，payload 只有 `37 6B`，地址不重复放入 payload。可以先用下面的命令确认实际总线通信：

```bash
./zdt_emm42_can_diag.sh -i can0 -a 1 --cmds 37 --listen 3
```

### 状态和曲线

插件状态中的 `error_counts` 是带符号的位置误差计数，`error_deg` 按下面公式换算：

```text
error_deg = sign × raw_value × 360 / 65536
```

符号字节 `0x00` 表示正、`0x01` 表示负；数值字段是 32 位大端“幅值”，不是补码。例如 `sign=0x01`、`raw_value=0x00000008` 得到约 `-0.043945°`。

`get_status()` 还提供 `last_update_time`、`online`、`error_count` 和 `error_history`。`error_history` 只保留最近 5 秒内校验通过的位置误差样本；超时、错误响应和校验失败不会追加零值或其它伪造点。5 秒是曲线滚动显示窗口，不是 CSV 累计日志长度，CSV 是否启用仍由 `csv_path` 或 `ZDT_EMM_LOG` 独立控制。

安装对应的 `mainsail-toolchanger` 前端并重启 Klipper 后，Dashboard 会出现“EMM42 位置误差”面板。面板显示当前误差、最近 5 秒最大绝对误差、采样状态、CAN 在线状态和带零线的角度曲线；没有 `[zdt_emm42]` 配置或驱动器离线时会显示明确提示。现有 `ZDT_EMM_STATUS`、`ZDT_EMM_QUERY`、`ZDT_EMM_SNIFF`、`ZDT_EMM_LOG` 和 `ZDT_EMM_POLL` 命令继续可用。
