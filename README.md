# box 保活方案（不休眠）

目标：让 box `main`（`cb-93fdeb8c6f16`）全天 running。

## 为什么必须从外部驱动

休眠判定在宿主机（Cocoon microVM 平台），不在容器里。容器内核没有任何 idle
计时器可关，`cocoonbox-agent` 只负责 vsock 执行，不参与休眠决策。塞个死循环
进容器没有用——VM 一暂停，里面的循环也停了。唯一可靠的开关是**持续有 MCP 活动**。

## 三层结构

| 层 | 位置 | 频率 | 作用 |
|---|---|---|---|
| 主驱动 | `keepalive.py --loop` | 240s | status → 睡着就 ensure → shell 心跳 |
| 兜底 | 同上 `--once`（Windows 计划任务 / GitHub cron） | 4-5min | 主进程被杀或本机没开也能续上 |
| 遥测 | 容器内 `box-heartbeat.timer` | 120s | 往 `/workspace/keepalive/box-heartbeat.log` 打点，日志出现空洞 = 真休眠过 |

```
box-keepalive/                      ← 本目录就是 git 仓库根
├── keepalive.py            主脚本（--loop / --once，自带循环重试）
├── config.json             本地配置（含 token，已被 .gitignore 挡住）
├── config.example.json     配置模板
├── config.ci.json          CI 配置（token 留空，走 env）
├── box-keepalive.vbs       登录自启，静默不弹窗
├── install-task.ps1        注册计划任务（需管理员 PowerShell）
├── uninstall-task.ps1      撤销 + 杀残留进程
├── box-heartbeat.sh        容器内心跳脚本
├── box-heartbeat.service   systemd 单元
├── box-heartbeat.timer     systemd 定时器（2 分钟）
└── .github/workflows/box-keepalive.yml   完全脱离本机的 24/7 方案
```

## 已装好的部分

- 容器内 `box-heartbeat.timer`：`enabled + active`，已连续触发，`ExecMainStatus=0`
- 本机启动文件夹：`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\box-keepalive.vbs`
  → 下次登录自动拉起 `--loop`

## 需要你手动做的两件事

### 1. 本机现在立刻跑起来
双击 `box-keepalive.vbs`（或 `wscript box-keepalive.vbs`）。之后它自己每 4 分钟一轮。

### 2. 真正的 24/7：挂到 GitHub Actions（不依赖本机开机）
1. 仓库已经推上去了（私有库，`.github/workflows/box-keepalive.yml` 就位）。
2. 仓库 Settings → Secrets and variables → Actions → 新建 `BOX_TOKEN`，值填 `cb_` 开头的密钥。
3. Actions 页面选 `box-keepalive` → Run workflow，确认绿勾。
4. 之后每 5 分钟自动跑，本机关机也不影响。

免费额度：私有仓库 2000 分钟/月，288 次/天 × 约 20 秒 ≈ 100 分钟/月。

### 可选：做成本机系统级计划任务（管理员 CMD）
```bat
schtasks /Create /F /TN "BoxKeepAlive" /SC ONSTART /RU SYSTEM /RL HIGHEST ^
  /TR "\"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe\" \"C:\Users\Administrator\WorkBuddy\2026-11-11-09-52-10\box-keepalive\keepalive.py\" --loop"

schtasks /Create /F /TN "BoxKeepAlive-Watchdog" /SC MINUTE /MO 4 /RU SYSTEM /RL HIGHEST ^
  /TR "\"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe\" \"C:\Users\Administrator\WorkBuddy\2026-11-11-09-52-10\box-keepalive\keepalive.py\" --once"
```

## 验证与回滚

```bash
# 本机看保活日志（每轮一行 alive）
tail -f logs/keepalive-YYYYMMDD.log

# 容器内看心跳，正常是每 2 分钟一条、间隔连续
tail -20 /workspace/keepalive/box-heartbeat.log
```

想恢复默认休眠：`powershell -File uninstall-task.ps1`，删掉启动文件夹里的 vbs，
`sudo systemctl disable --now box-heartbeat.timer`。

## 调参

- 间隔想更保守：`config.json` 的 `interval_seconds` 改 180。
- 不计费：`shell` 不调模型，不消耗免费档每天 1000 Credits。
- 别把间隔调到 15 分钟以上——宿主机 idle 阈值官方没公开，留足余量。
