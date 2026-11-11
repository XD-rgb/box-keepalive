#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy-github.py —— 一键把本仓库推上 GitHub 并配好 Actions 保活。

用法（在 box-keepalive 目录下）：
    set GITHUB_TOKEN=github_pat_xxx   &&  python deploy-github.py
    set GITHUB_TOKEN=ghp_xxx          &&  python deploy-github.py --repo box-keepalive --public

做的事：
  1. GET /user                             验身份
  2. POST /user/repos                      建私有仓（已存在则复用）
  3. git push                              推送 main（token 只走内存，不写进 .git/config）
  4. 读仓库 public key，用 libsodium sealed box 加密 BOX_TOKEN
  5. PUT actions/secrets/BOX_TOKEN         写入 Actions Secret
  6. POST workflow dispatch                立刻跑一次
  7. 轮询 run 状态                          打印结论

依赖：PyNaCl（只用于第 4 步的 sealed box 加密）
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
HERE = Path(__file__).resolve().parent


def die(msg: str) -> None:
    print(f"[✗] {msg}", file=sys.stderr)
    raise SystemExit(1)


def api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "box-keepalive-deploy",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"message": raw}
        return e.code, payload


def git(*args: str, env: dict | None = None) -> tuple[int, str]:
    e = dict(os.environ)
    e["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        e.update(env)
    p = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, env=e)
    out = (p.stdout + p.stderr).strip()
    return p.returncode, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="box-keepalive")
    ap.add_argument("--public", action="store_true", help="建成公开仓（默认私有）")
    ap.add_argument("--skip-secret", action="store_true")
    ap.add_argument("--no-dispatch", action="store_true")
    ap.add_argument("--box-token", default=os.environ.get("BOX_TOKEN", ""),
                    help="要写进 Actions Secret 的 box 密钥")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        die("先设 GITHUB_TOKEN 环境变量")

    # 1. 验身份
    code, me = api("GET", "/user", token)
    if code != 200:
        die(f"身份校验失败 {code}: {me.get('message')}")
    login = me["login"]
    print(f"[1/7] 身份 ✓ {login}")

    # 2. 建仓
    code, repo = api("GET", f"/repos/{login}/{args.repo}", token)
    if code == 200:
        print(f"[2/7] 仓库已存在，复用 {repo['html_url']}")
    else:
        code, repo = api("POST", "/user/repos", token, {
            "name": args.repo,
            "private": not args.public,
            "description": "让 box by Simular 的云容器全天运行：外部轮询保活 + 容器内心跳 + GitHub Actions 兜底",
            "has_issues": False, "has_wiki": False, "auto_init": False,
        })
        if code not in (200, 201):
            die(f"建仓失败 {code}: {repo.get('message')} / {repo.get('errors')}")
        print(f"[2/7] 建仓 ✓ {repo['html_url']}（{'公开' if args.public else '私有'}）")

    clone_url = repo["clone_url"]
    auth_url = clone_url.replace("https://", f"https://x-access-token:{token}@")

    # 3. 推送（token 不落盘）
    rc, out = git("remote", "get-url", "origin")
    if rc == 0:
        git("remote", "set-url", "origin", clone_url)
    else:
        git("remote", "add", "origin", clone_url)
    rc, out = git("push", auth_url, "main:main", "-f")
    if rc != 0:
        die(f"推送失败：\n{out[-1500:]}")
    print(f"[3/7] 推送 ✓ main -> {login}/{args.repo}")

    # 4+5. Actions Secret
    if not args.skip_secret:
        box_token = args.box_token
        if not box_token:
            cfg = HERE / "config.json"
            if cfg.exists():
                box_token = json.loads(cfg.read_text(encoding="utf-8")).get("token", "")
        if not box_token:
            print("[4/7] 跳过 Secret：没拿到 box 密钥（--box-token 或 config.json）")
        else:
            try:
                from nacl import encoding, public  # type: ignore
            except ImportError:
                die("缺 PyNaCl：pip install pynacl（只用于加密 Secret）")
            code, key = api("GET", f"/repos/{login}/{args.repo}/actions/secrets/public-key", token)
            if code != 200:
                die(f"取 public key 失败 {code}: {key.get('message')}（PAT 需要 repo+workflow 权限）")
            pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
            sealed = public.SealedBox(pk).encrypt(box_token.encode())
            code, res = api("PUT", f"/repos/{login}/{args.repo}/actions/secrets/BOX_TOKEN", token, {
                "encrypted_value": base64.b64encode(sealed).decode(),
                "key_id": key["key_id"],
            })
            if code not in (200, 201, 204):
                die(f"写 Secret 失败 {code}: {res.get('message')}")
            print(f"[4/7] Secret BOX_TOKEN ✓（{box_token[:7]}… 共 {len(box_token)} 字符）")
    else:
        print("[4/7] --skip-secret，跳过")

    # 6. 立刻跑一次
    if not args.no_dispatch:
        code, res = api("POST", f"/repos/{login}/{args.repo}/actions/workflows/box-keepalive.yml/dispatches",
                        token, {"ref": "main"})
        if code not in (204, 201, 200):
            print(f"[5/7] dispatch 失败 {code}: {res.get('message')}（可在网页手动 Run）")
        else:
            print("[5/7] 已触发 workflow_dispatch")

        # 7. 轮询结论
        print("[6/7] 等 run 起来…")
        run = None
        for _ in range(20):
            time.sleep(6)
            code, runs = api("GET", f"/repos/{login}/{args.repo}/actions/runs?per_page=5", token)
            if code == 200 and runs.get("workflow_runs"):
                run = runs["workflow_runs"][0]
                if run["status"] == "completed":
                    break
        if not run:
            print("[7/7] 没等到 run，去网页看")
        else:
            print(f"[7/7] run #{run['run_number']} {run['status']}/{run['conclusion']} "
                  f"{run['html_url']}")
            if run["conclusion"] != "success":
                jid = run.get("id")
                code, jobs = api("GET", f"/repos/{login}/{args.repo}/actions/runs/{jid}/jobs", token)
                if code == 200:
                    for j in jobs.get("jobs", []):
                        for s in j.get("steps", []):
                            print(f"    - {s['name']}: {s['conclusion']}")

    print(f"\n完成。仓库：{repo['html_url']}")
    print(f"密钥设置页：{repo['html_url']}/settings/secrets/actions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
