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

仓库中的 `klipper/extras/zdt_emm42.py` 可通过 SocketCAN 读取 ZDT EMM42_V5 闭环驱动器的运行参数，并在 Mainsail Dashboard 显示位置误差实时曲线。正常监控不接管 Klipper 的 STEP/DIR/EN 运动控制；只有显式执行 `ZDT_EMM_AUTOTUNE` 时，才会在用户确认的轴上执行测试运动并写入位置环 PID。

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
error_poll_interval: 0.05
pid_poll_interval: 30.0
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

`get_status()` 还提供 `last_update_time`、`online`、`error_count` 和 `error_history`。`error_history` 只保留最近 10 秒内校验通过的位置误差样本，每项包含角度、原始计数和毫米误差；超时、错误响应和校验失败不会追加零值或其它伪造点。10 秒是曲线滚动显示窗口，不是 CSV 累计日志长度，CSV 是否启用仍由 `csv_path` 或 `ZDT_EMM_LOG` 独立控制。

安装对应的 `mainsail-toolchanger` 前端并重启 Klipper 后，Dashboard 会出现“EMM42 位置误差”面板。面板自动发现全部 `[zdt_emm42 <name>]` 实例，通过标签页切换；可用角度或毫米绘制最近 10 秒误差，并显示电气、运动、PID、驱动状态和 CAN 诊断信息。没有配置或驱动器离线时会显示明确提示。现有 `ZDT_EMM_STATUS`、`ZDT_EMM_QUERY`、`ZDT_EMM_SNIFF`、`ZDT_EMM_LOG` 和 `ZDT_EMM_POLL` 命令继续可用。

PID 参数通过 `0x21` 多帧回复独立读取：Klipper 连接后自动读取一次，此后默认每 30 秒异步刷新。可用 `pid_poll_interval` 调整周期。读取失败时保留上次有效 Kp/Ki/Kd，并通过 `pid_error` 暴露失败原因，不影响位置误差的 CAN 在线判定。

### 闭环 PID 自动调参

在确认电机有安全运动空间、轴已经回零且打印机空闲后，可以让插件通过 CAN 在线搜索 EMM42 的位置环 PID：

```gcode
ZDT_EMM_AUTOTUNE NAME=shadow_a AXIS=X DISTANCE=10 SPEED=20 ACCEL=200 ITERATIONS=20 CONFIRM=1
```

上述 `PROFILE=AXIS`（默认）模式保留原有单轴正反往返行为。CoreXY 打印机建议使用打印工况模式：

```gcode
ZDT_EMM_AUTOTUNE NAME=shadow_a PROFILE=COREXY_PRINT DISTANCE=100 SPEED=200 ACCEL=5000 ITERATIONS=20 REPEATS=3 MAX_ERROR_DEG=6.5 CONFIRM=1
```

以上数值也是 CoreXY 模式的默认值；当前位置向正 X、正 Y 各需保留 100 mm 安全空间。`SPEED`、`ACCEL` 应填写切片中实际使用的最高打印值。插件自动使用 40%、70%、100% 三档速度/加速度，并在每档执行 100 mm 长线/对角线、10 mm 连续直角折线和 32 段圆周三类轨迹。每类轨迹整体进入 Klipper 前瞻队列，只在轨迹块结束时等待，因此拐角速度与真实打印更接近。每个“速度档 × 轨迹”工况默认重复三次取中位数，所有工况等权评分。调参使用独立 20 ms 采样缓冲，不受 Dashboard 10 秒历史窗口影响，评分由运动 RMS、P95、峰值和停止后 RMS 组成。

候选 PID 使用 `0x4A STORE=0` 写入并回读，Kp/Kd 默认限制在原始值的 `0.5–2.0` 倍，Ki 默认限制在 `0–max(原始值×10, 原始值+10×KI_STEP)`；也可用 `KP_MIN/KP_MAX`、`KI_MIN/KI_MAX`、`KD_MIN/KD_MAX` 覆盖。候选至少改善 2% 才会接受，搜索结束后还会使用 55%、85% 档和不同轨迹进行独立验证，通过后才以 `STORE=1` 写入电机。这个过程不修改 `printer.cfg`，也不需要重启 Klipper。

`MAX_ERROR_DEG` 默认 `6.5°`，是 CoreXY 模式的安全门槛；任一采样越线、CAN 离线或驱动器报告堵转时会拒绝或终止测试并恢复参数。可发送 `ZDT_EMM_AUTOTUNE_CANCEL NAME=shadow_a` 请求取消，当前轨迹块结束后恢复原始 PID。默认步长可通过 `autotune_kp_step`、`autotune_ki_step`、`autotune_kd_step` 调整，`pid_write_settle_time` 控制 CAN 写入后的等待时间。

命令必须使用 CAN 正确的地址承载方式：

```ini
can_payload_includes_addr: False
```

如果仍为 `True`，插件会在运动前拒绝调参，避免把 `01 4A ...` 这种串口形态误发到 CAN payload。首次调参会先读取当前 PID，再使用相同 PID 做一次 `STORE=0` 长帧写入和回读验证；`0x21` 长响应或 `0x4A` 长请求验证失败时不会开始运动。
