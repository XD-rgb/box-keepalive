# box 保活方案（不休眠）

目标：让 box `main`（`cb-93fdeb8c6f16`）全天 running。

仓库：https://github.com/XD-rgb/box-keepalive （public，workflow 常驻）

## 为什么必须从外部驱动

休眠判定在宿主机（Cocoon microVM 平台），不在容器里。容器内核没有任何 idle
计时器可关，`cocoonbox-agent` 只负责 vsock 执行，不参与休眠决策。塞个死循环
进容器没有用——VM 一暂停，里面的循环也停了。唯一可靠的开关是**持续有 MCP 活动**。

另外 `GET /mcp` 返回 `405 allow: POST, DELETE`，持长连接也不成立，只能周期性 POST。

## 三层结构

| 层 | 位置 | 频率 | 作用 |
|---|---|---|---|
| 主驱动 | GitHub Actions `box-keepalive` job | 240s | status → 睡着就 ensure → shell 心跳，**不依赖本机开机** |
| 兜底 | 本机 `keepalive.py --loop`（启动文件夹 vbs） | 240s | 本机开机时额外加一层冗余 |
| 遥测 | 容器内 `box-heartbeat.timer` | 120s | 往 `/workspace/keepalive/box-heartbeat.log` 打点，日志出现空洞 = 真休眠过 |

### CI 为什么不是 `*/5` 短 cron

GitHub 的 `schedule` 只保证「大约」，高负载时延迟十几分钟，极端情况直接丢弃；
box 的 idle 阈值官方没公开（文档只写了 "Sleeps when idle"）。靠 cron 打点等于赌运气。

所以 workflow 里是一个**长期在跑循环的 job**：

```
cron 0 * * * *  →  拉起新 job（concurrency cancel-in-progress，顶掉上一个）
                  job 内部每 240s 自己打一次，跑满 65 分钟正常退出
                  job 万一挂掉，最迟 1 小时后被下一个定时顶掉
                  任何时刻最多 1 个 job 在跑
```

打点节奏由 job 内部保证，不受 cron 抖动影响。

**计费**：公开仓库的 standard runner 不计分钟数，24/7 常驻不吃额度。
仓库内容只有保活脚本，没有任何密钥——`BOX_TOKEN` 存在 Actions Secret 里。

## 目录

```
box-keepalive/                      ← 本目录就是 git 仓库根
├── keepalive.py            主脚本（--loop / --once / --max-minutes）
├── config.json             本地配置（含 token，已被 .gitignore 挡住）
├── config.example.json     配置模板
├── config.ci.json          CI 配置（token 留空，走 env）
├── box-keepalive.vbs       登录自启，静默不弹窗
├── install-task.ps1        注册计划任务（需管理员 PowerShell）
├── uninstall-task.ps1      撤销 + 杀残留进程
├── box-heartbeat.sh        容器内心跳脚本
├── box-heartbeat.service   systemd 单元
├── box-heartbeat.timer     systemd 定时器（2 分钟）
├── deploy-github.py        一键建仓 / 推送 / 写 Secret / 触发 / 等绿勾
└── .github/workflows/box-keepalive.yml   常驻循环 job
```

## 已装好的部分

- GitHub Actions：常驻 job 已跑通，`BOX_TOKEN` 已写入 Secret，日志里能看到真实
  `alive tick=... box=running | ... up=...` 后紧跟容器内 uptime
- 容器内 `box-heartbeat.timer`：`enabled + active`，`ExecMainStatus=0`
- 本机启动文件夹：`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\box-keepalive.vbs`
  → 下次登录自动拉起 `--loop`（冗余层，不加也行）

## 私有仓模式（不想公开仓库时）

把仓库改回 private，同时把 workflow 换回短 cron：

```yaml
on:
  schedule:
    - cron: "*/5 * * * *"
```

并把 job 里的 `--loop --max-minutes 65` 改回 `--once`，删掉 `concurrency`。
代价：私有仓 2000 分钟/月，288 次/天 × 约 20 秒 ≈ 100 分钟/月，够用；
但 cron 抖动时可能出现 10 分钟以上的空窗——这也是当初改成常驻 job 的原因。

## 可选：本机系统级计划任务（管理员 CMD）

```bat
schtasks /Create /F /TN "BoxKeepAlive" /SC ONSTART /RU SYSTEM /RL HIGHEST ^
  /TR "\"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe\" \"C:\Users\Administrator\WorkBuddy\2026-11-11-09-52-10\box-keepalive\keepalive.py\" --loop"

schtasks /Create /F /TN "BoxKeepAlive-Watchdog" /SC MINUTE /MO 4 /RU SYSTEM /RL HIGHEST ^
  /TR "\"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe\" \"C:\Users\Administrator\WorkBuddy\2026-11-11-09-52-10\box-keepalive\keepalive.py\" --once"
```

（这台机器的 `schtasks.exe` 被安全策略拦，需你在管理员 CMD 里手动执行。）

## 验证与回滚

```bash
# 容器内看心跳：正常是每 2 分钟一条（本机 timer）+ 每 4 分钟一条（CI），时间戳连续
tail -20 /workspace/keepalive/box-heartbeat.log

# 看 CI 是否在跑
gh run list -R XD-rgb/box-keepalive -L 3

# 本机留意日志（有跑本地层时）
tail -f logs/keepalive-YYYYMMDD.log
```

整体回滚：GitHub 仓库 Settings → Actions → 选 `box-keepalive` → Disable workflow，
再 `sudo systemctl disable --now box-heartbeat.timer`，删掉启动文件夹里的 vbs。

## 调参

- 间隔：`config.ci.json` / `config.json` 的 `interval_seconds`（默认 240）。
- 别调到 15 分钟以上——宿主机 idle 阈值官方没公开，240s 是留了余量的值。
- 不计费：`shell` 不调模型，不消耗免费档每天 1000 Credits。
- 容器闲置 14 天才会被删除，任何一次成功唤醒都重置计数；现在每 4 分钟一次，
  这条风险实际已经消失。
