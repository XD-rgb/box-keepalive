#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""poll-device-token.py —— 轮询 GitHub 设备授权码，拿到 token 就落盘。

由主流程后台启动；token 写到 .gh-token（已 gitignore）。
成功退出 0，过期待授权退出 3。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

CLIENT_ID = "Iv1.b507a08c87ecfe98"
PROXY = os.environ.get("GH_PROXY", "http://127.0.0.1:7897")
DEVICE_CODE = sys.argv[1]
OUT = os.environ.get("GH_TOKEN_OUT", ".gh-token")


def post(data: dict) -> dict:
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    req = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=urllib.parse.urlencode(data).encode(),
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    with op.open(req, timeout=30) as r:
        return json.loads(r.read().decode())


deadline = time.time() + 890
interval = 5
while time.time() < deadline:
    try:
        res = post({"client_id": CLIENT_ID, "device_code": DEVICE_CODE,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    except Exception as e:
        print(f"网络错误，重试：{e}", flush=True)
        time.sleep(interval)
        continue

    if res.get("access_token"):
        tok = res["access_token"]
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(tok)
        print(f"TOKEN_OK len={len(tok)} scope={res.get('scope')}", flush=True)
        sys.exit(0)
    err = res.get("error")
    if err == "authorization_pending":
        time.sleep(interval)
    elif err == "slow_down":
        interval += 5
        time.sleep(interval)
    elif err in ("expired_token", "access_denied", "incorrect_device_code"):
        print(f"STOP {err}", flush=True)
        sys.exit(3)
    else:
        print(f"未知响应 {res}", flush=True)
        time.sleep(interval)

print("STOP timeout", flush=True)
sys.exit(3)
