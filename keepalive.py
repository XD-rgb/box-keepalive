#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""box-keepalive —— 让 box by Simular 的云容器全天运行、不休眠。

原理：box 的休眠判定在宿主机侧（Cocoon microVM 平台），内核里没有任何
idle 计时器可以关掉，guest 内跑死循环也拦不住。唯一可靠的开关是「持续有
MCP 活动」。所以本脚本从外部按固定间隔发一次真实工具调用（shell 心跳），
让 box 的 idle 计时器永远归零。

每轮做三件事：
  1. box_status 读状态
  2. 若已 hibernated —— box_ensure 唤醒，并记一条 wake 事件
  3. shell 写心跳到容器内 /workspace/keepalive/box-heartbeat.log

不计费：shell 不打模型，不消耗免费档每天 1000 Credits。

用法：
  pythonw keepalive.py --loop          # 常驻，按 config.interval_seconds 循环
  python  keepalive.py --once          # 只跑一轮（给计划任务当看门狗用）
  python  keepalive.py --once -v       # 带详细输出
退出码：0 正常 / 1 本轮失败（已重试完）/ 2 配置错误
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_HEARTBEAT = (
    "mkdir -p /workspace/keepalive && "
    "f=/workspace/keepalive/box-heartbeat.log && "
    'printf "%s up=%ss\\n" "$(date -u +%FT%TZ)" "$(cut -d. -f1 /proc/uptime)" >> "$f" && '
    'if [ "$(wc -c < "$f")" -gt 262144 ]; then tail -n 2000 "$f" > "$f.t" && mv "$f.t" "$f"; fi && '
    'tail -n 2 "$f"'
)
USER_AGENT = "box-keepalive/1.0 (+workbuddy)"


# ---------------------------------------------------------------- 配置与日志
def load_config() -> dict:
    path = Path(os.environ.get("BOX_KEEPALIVE_CONFIG", HERE / "config.json"))
    if not path.exists():
        print(f"找不到配置文件：{path}", file=sys.stderr)
        raise SystemExit(2)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if os.environ.get("BOX_TOKEN"):
        cfg["token"] = os.environ["BOX_TOKEN"]
    if not cfg.get("token"):
        print("config.json 里没有 token，也没设 BOX_TOKEN", file=sys.stderr)
        raise SystemExit(2)
    log_dir = Path(cfg.get("log_dir") or (HERE / "logs"))
    if not log_dir.is_absolute():
        log_dir = HERE / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    cfg["log_dir"] = log_dir
    cfg.setdefault("endpoint", "https://mcp.box.simular.ai/mcp")
    cfg.setdefault("interval_seconds", 240)
    cfg.setdefault("retention_days", 7)
    cfg.setdefault("heartbeat_command", DEFAULT_HEARTBEAT)
    return cfg


def setup_logging(log_dir: Path, verbose: bool) -> logging.Logger:
    log = logging.getLogger("box-keepalive")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_dir / f"keepalive-{datetime.now():%Y%m%d}.log",
                             encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    if verbose or sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG if verbose else logging.INFO)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


def prune_logs(log_dir: Path, days: int) -> None:
    cutoff = time.time() - days * 86400
    for f in log_dir.glob("keepalive-*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- MCP 传输
class Box:
    def __init__(self, endpoint: str, token: str, box: str = ""):
        self.endpoint = endpoint
        self.token = token
        self.box = box

    def _post(self, payload: dict, timeout: int = 120) -> dict:
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        if not raw.strip():
            return {}
        if raw.lstrip()[:5] in ("event", "data:"):
            buf = []
            for line in raw.splitlines():
                if line.startswith("data:"):
                    buf.append(line[5:].strip())
            raw = "\n".join(buf) or raw
        return json.loads(raw)

    def call(self, name: str, args: dict, timeout: int = 120) -> tuple[bool, dict]:
        """返回 (是否成功, structured 或原始 result)。"""
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": args}}
        out = self._post(payload, timeout)
        if "error" in out:
            return False, out["error"]
        result = out.get("result") or {}
        ok = not result.get("isError")
        data = result.get("structuredContent")
        if data is None:
            blocks = result.get("content") or []
            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                data = {"text": text}
        return ok, data

    def _args(self, **kw) -> dict:
        a = dict(kw)
        if self.box:
            a["box"] = self.box
        return a


# ---------------------------------------------------------------- 一轮保活
def tick(box: Box, cfg: dict, log: logging.Logger) -> bool:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 1. 读状态
    state = None
    try:
        ok, data = box.call("box_status", box._args())
        if not ok:
            log.error("box_status 失败：%s", json.dumps(data, ensure_ascii=False)[:300])
        else:
            state = data.get("state")
            log.debug("状态=%s image=%s 升级可用=%s", state, data.get("image"),
                      data.get("upgrade_available"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as e:
        log.error("box_status 网络错误：%s", e)
        return False

    # 2. 睡着就唤醒
    if state and state != "running":
        log.warning("检测到 %s，执行 box_ensure 唤醒…", state)
        t0 = time.time()
        try:
            ok, data = box.call("box_ensure", box._args(wait=True), timeout=300)
            if ok:
                log.warning("唤醒成功，耗时 %.1fs，新状态=%s", time.time() - t0,
                            data.get("state"))
            else:
                log.error("唤醒失败：%s", json.dumps(data, ensure_ascii=False)[:300])
                return False
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            log.error("唤醒阶段网络错误：%s", e)
            return False

    # 3. 心跳（真实工具调用，重置 idle 计时）
    try:
        ok, data = box.call("shell",
                            box._args(command=cfg["heartbeat_command"], timeout_s=60),
                            timeout=180)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        log.error("心跳网络错误：%s", e)
        return False

    if not ok:
        log.error("心跳被拒：%s", json.dumps(data, ensure_ascii=False)[:300])
        return False

    body = [ln for ln in (data.get("text") or "").splitlines()
            if ln.strip() and not ln.startswith("[exit status")]
    log.info("alive tick=%s box=%s | %s", stamp, state or "?",
             (body[-1] if body else "")[:100])
    return True


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(description="box 云容器保活")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--loop", action="store_true", help="常驻循环（默认）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    log = setup_logging(cfg["log_dir"], args.verbose)
    box = Box(cfg["endpoint"], cfg["token"], cfg.get("box") or "")
    interval = int(cfg["interval_seconds"])

    prune_logs(cfg["log_dir"], int(cfg["retention_days"]))

    if args.once:
        return 0 if tick(box, cfg, log) else 1

    log.info("保活启动，间隔 %ds，box=%s", interval, cfg.get("box") or "default")
    fails = 0
    while True:
        try:
            ok = tick(box, cfg, log)
            fails = 0 if ok else fails + 1
        except Exception as e:  # 循环永不退出
            fails += 1
            log.exception("本轮异常（连续 %d 次）：%s", fails, e)

        # 连续失败就转入快速重试，成功一次自动回到常规节奏
        wait = 60 if 0 < fails < 5 else (300 if fails else interval)
        # 睡眠切片，便于将来加信号
        slept = 0
        while slept < wait:
            time.sleep(min(30, wait - slept))
            slept += 30


if __name__ == "__main__":
    sys.exit(main())
