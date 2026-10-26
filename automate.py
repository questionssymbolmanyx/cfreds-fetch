#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CFReDS 搬运全自动驱动
=====================
本机不能上 Google，但能上 GitHub。这个脚本负责把"手"接到 GitHub Actions 上：
建仓库 -> 推工具包 -> 触发 workflow -> 等结果 -> 把清单/资产拉回本地。

前置：一个 GitHub Personal Access Token（classic，勾 repo + workflow）
用法：
    set GITHUB_TOKEN=ghp_xxxxxxxx
    py -3.12 automate.py list
    py -3.12 automate.py download --max-gb 20

可选参数：
    --repo 仓库名(默认 cfreds-fetch)   --folder-id Drive 文件夹ID
    --include 正则   --no-push 只触发不推代码   --keep  拉完资产不删中间文件
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import requests

API = "https://api.github.com"
WF_FILE = ".github/workflows/fetch.yml"
HERE = Path(__file__).resolve().parent
DL_DIR = HERE / "downloads"

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def log(m: str = "") -> None:
    print(m, flush=True)


def human(n) -> str:
    if n is None:
        return "?"
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{f:.0f}{u}" if u == "B" else f"{f:.2f}{u}"
        f /= 1024
    return str(n)


def api(method: str, path: str, token: str | None = None, **kw):
    url = path if path.startswith("http") else API + path
    headers = kw.pop("headers", {})
    headers.setdefault("Accept", "application/vnd.github+json")
    headers.setdefault("X-GitHub-Api-Version", "2022-11-28")
    headers.setdefault("User-Agent", "cfreds-fetch")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, url, headers=headers, timeout=60, **kw)


def die(msg: str, hint: str = "") -> None:
    log(f"\n!! {msg}")
    if hint:
        log(f"   {hint}")
    sys.exit(1)


# --------------------------------------------------------------------------- #
def whoami(token: str) -> str:
    r = api("GET", "/user", token)
    if r.status_code == 401:
        die("token 无效或已过期", "重新生成：https://github.com/settings/tokens")
    if r.status_code != 200:
        die(f"取用户信息失败 {r.status_code}: {r.text[:200]}")
    return r.json()["login"]


def ensure_repo(token: str, user: str, repo: str) -> None:
    r = api("GET", f"/repos/{user}/{repo}", token)
    if r.status_code == 200:
        info = r.json()
        log(f"== 仓库已存在: {info['full_name']} (private={info['private']})")
        if info["private"]:
            log("   ! 这是私有库：Actions 只有 2000 分钟/月，大数据可能不够。建议改成 Public。")
        return
    if r.status_code != 404:
        die(f"查询仓库失败 {r.status_code}: {r.text[:200]}")
    log(f"== 创建公开仓库 {user}/{repo}")
    r = api("POST", "/user/repos", token, json={
        "name": repo,
        "private": False,
        "description": "CFReDS (NIST) drone forensics data relay via GitHub Actions",
        "auto_init": False,
    })
    if r.status_code not in (200, 201):
        die(f"建仓库失败 {r.status_code}: {r.text[:300]}",
            "token 需要勾选 repo 权限（classic）或 Administration:write（fine-grained）")


UPLOAD_FILES = [
    ".github/workflows/fetch.yml",
    "fetch_drive.py",
    "automate.py",
    "README.md",
    "push.ps1",
    "_selftest.py",
    ".gitignore",
    ".gitattributes",
]


def api_push(token: str, user: str, repo: str) -> None:
    """用 Contents API 直传文件——完全绕开 git 传输层（本机 github.com:443 不通，但 api 通）。"""
    log("== 上传文件（Contents API，逐个 PUT）")
    for rel in UPLOAD_FILES:
        p = HERE / rel
        if not p.exists():
            continue
        content = base64.b64encode(p.read_bytes()).decode()
        body = {"message": f"add {rel}", "content": content, "branch": "main"}

        r = api("GET", f"/repos/{user}/{repo}/contents/{rel}", token)
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, dict) and d.get("sha"):
                if (d.get("content") or "").replace("\n", "").replace("\r", "") == content:
                    log(f"   未变化，跳过  {rel}")
                    continue
                body["sha"] = d["sha"]

        r = api("PUT", f"/repos/{user}/{repo}/contents/{rel}", token, json=body)
        if r.status_code not in (200, 201):
            die(f"上传 {rel} 失败 {r.status_code}: {r.text[:300]}",
                "token 需要 repo + workflow 两个 scope")
        log(f"   已上传  {rel}")


def git_push(token: str, user: str, repo: str) -> None:
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    url = f"https://github.com/{user}/{repo}.git"
    cmds = [
        ["git", "config", "user.name", user],
        ["git", "config", "user.email", f"{user}@users.noreply.github.com"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "chore: CFReDS DF001 fetch toolkit"],
        ["git", "remote", "remove", "origin"],
        ["git", "remote", "add", "origin", url],
    ]
    for c in cmds:
        subprocess.run(c, cwd=HERE, capture_output=True, text=True)  # 允许失败（已提交/无 origin）

    log("== 推送代码")
    p = subprocess.run(
        ["git", "-c", "credential.helper=", "-c", f"http.extraheader=AUTHORIZATION: basic {b64}",
         "push", "-u", "origin", "HEAD:main"],
        cwd=HERE, capture_output=True, text=True,
    )
    if p.returncode != 0:
        die("推送失败", (p.stderr or p.stdout).strip()[:600])
    log("   推送成功")


def find_workflow(token: str, user: str, repo: str) -> int:
    for i in range(12):
        r = api("GET", f"/repos/{user}/{repo}/actions/workflows", token)
        if r.status_code == 200:
            for wf in r.json().get("workflows", []):
                if wf["path"] == WF_FILE:
                    return wf["id"]
        log(f"   等待 workflow 注册… ({(i + 1) * 5}s)")
        time.sleep(5)
    die("找不到 workflow 文件", "确认 .github/workflows/fetch.yml 已推到默认分支 main")


def dispatch(token: str, user: str, repo: str, wf_id: int, inputs: dict) -> None:
    for i in range(10):
        r = api("POST", f"/repos/{user}/{repo}/actions/workflows/{wf_id}/dispatches", token,
                json={"ref": "main", "inputs": inputs})
        if r.status_code == 204:
            log("== 已触发 workflow")
            return
        if r.status_code == 422:
            log(f"   参数被拒：{r.text[:200]}")
            break
        if r.status_code == 404:
            log(f"   404，等待 workflow 在 main 上就绪… ({(i + 1) * 6}s)")
            time.sleep(6)
            continue
        die(f"触发失败 {r.status_code}: {r.text[:300]}")
    die("无法触发 workflow")


def wait_run(token: str, user: str, repo: str, since: float, timeout: int = 3600) -> dict:
    log("== 等待运行（首次可能要 10s 才出现在列表里）")
    run = None
    for _ in range(60):
        r = api("GET", f"/repos/{user}/{repo}/actions/runs?per_page=10", token)
        if r.status_code == 200:
            for it in r.json().get("workflow_runs", []):
                created = datetime.fromisoformat(it["created_at"].replace("Z", "+00:00")).timestamp()
                if created >= since - 60 and it["path"] == WF_FILE:
                    run = it
                    break
        if run:
            break
        time.sleep(4)
    if not run:
        die("没看到新的 run", "去 Actions 页面看一眼是否被手动禁用（Enable workflow）")

    rid = run["id"]
    log(f"   run #{run['run_number']} id={rid}")
    log(f"   {run['html_url']}")
    t0 = time.time()
    last_state = None
    while time.time() - t0 < timeout:
        r = api("GET", f"/repos/{user}/{repo}/actions/runs/{rid}", token)
        it = r.json()
        state = (it["status"], it.get("conclusion"))
        if state != last_state:
            log(f"   状态: {it['status']} / {it.get('conclusion')}")
            last_state = state
        if it["status"] == "completed":
            return it
        time.sleep(10)
    die("等待超时")
    return {}


def dump_logs(token: str, user: str, repo: str, rid: int, tail: int = 40) -> Path | None:
    """下载运行日志 zip 并打印关键片段。"""
    r = api("GET", f"/repos/{user}/{repo}/actions/runs/{rid}/logs", token, allow_redirects=False)
    if r.status_code not in (301, 302):
        log(f"   ! 取日志失败 {r.status_code}")
        return None
    loc = r.headers["Location"]
    z = requests.get(loc, timeout=120)  # 签名 URL，不需要 auth
    if z.status_code != 200:
        log(f"   ! 日志下载失败 {z.status_code}")
        return None
    lp = HERE / f"run-{rid}.log.zip"
    lp.write_bytes(z.content)
    shown = 0
    with zipfile.ZipFile(lp) as zf:
        for name in zf.namelist():
            if not name.endswith(".txt"):
                continue
            text = zf.read(name).decode("utf-8", "replace")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            log(f"\n---------- {name} ----------")
            for ln in lines[-tail:]:
                log("  " + ln)
            shown += 1
            if shown >= 2:
                break
    return lp


def show_manifest(token: str, user: str, repo: str, rid: int) -> None:
    r = api("GET", f"/repos/{user}/{repo}/actions/runs/{rid}/artifacts", token)
    arts = [a for a in r.json().get("artifacts", []) if a["name"].startswith("manifest-")]
    if not arts:
        log("   ! 没有 manifest 产物")
        return
    aid = arts[-1]["id"]
    r = api("GET", f"/repos/{user}/{repo}/actions/artifacts/{aid}/zip", token, allow_redirects=False)
    if r.status_code not in (301, 302):
        log(f"   ! 产物下载失败 {r.status_code}")
        return
    z = requests.get(r.headers["Location"], timeout=300)
    zp = HERE / "manifest.zip"
    zp.write_bytes(z.content)
    with zipfile.ZipFile(zp) as zf:
        for inner in zf.namelist():
            if inner.endswith("manifest.csv"):
                zf.extract(inner, HERE)
                csv_path = HERE / inner
                log("\n== 文件清单 ==")
                for line in csv_path.read_text(encoding="utf-8-sig").splitlines():
                    log("  " + line)
                return
    log("   ! 产物里没有 manifest.csv")


def fetch_release(token: str, user: str, repo: str, outdir: Path) -> None:
    r = api("GET", f"/repos/{user}/{repo}/releases?per_page=5", token)
    rels = r.json()
    if not isinstance(rels, list) or not rels:
        log("   ! 没有 Release（可能下载失败了）")
        return
    rel = rels[0]
    log(f"\n== Release: {rel['tag_name']}  资产 {len(rel['assets'])} 个")
    outdir.mkdir(parents=True, exist_ok=True)
    total = sum(a["size"] for a in rel["assets"])
    free = shutil.disk_usage(outdir).free
    log(f"   合计 {human(total)}，本地剩余 {human(free)}")
    if total > free * 0.95:
        die("本地磁盘不够", f"腾出 {human(total - free)} 再来，或先用 --include 分批")

    for a in rel["assets"]:
        dest = outdir / a["name"]
        if dest.exists() and dest.stat().st_size == a["size"]:
            log(f"   跳过（已存在） {a['name']}")
            continue
        log(f"   下载 {a['name']}  ({human(a['size'])})")
        with requests.get(a["browser_download_url"], stream=True, timeout=120) as resp:
            resp.raise_for_status()
            done = 0
            last = time.time()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(1024 * 256):
                    if chunk:
                        fh.write(chunk)
                        done += len(chunk)
                        if time.time() - last > 5:
                            log(f"      {done * 100 / a['size']:.1f}%  {human(done)}")
                            last = time.time()
            tmp.replace(dest)
    log(f"\n== 全部资产已落到 {outdir}")
    log("== 下一步：进入 downloads 目录，用随包生成的脚本还原目录结构：")
    log("   powershell -ExecutionPolicy Bypass -File .\\MERGE.ps1")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="CFReDS 搬运全自动驱动")
    ap.add_argument("mode", choices=["list", "download"])
    ap.add_argument("--repo", default="cfreds-fetch")
    ap.add_argument("--folder-id", default="1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw")
    ap.add_argument("--include", default="")
    ap.add_argument("--max-gb", default="20")
    ap.add_argument("--no-push", action="store_true", help="仓库已有代码时只触发")
    ap.add_argument("--use-git", action="store_true", help="走 git push 而不是 Contents API")
    args = ap.parse_args()

    if not TOKEN:
        die("没有 token", "先 set GITHUB_TOKEN=ghp_xxx（classic token，勾 repo + workflow）")

    user = whoami(TOKEN)
    log(f"== 已认证为 {user}")
    ensure_repo(TOKEN, user, args.repo)
    if not args.no_push:
        if args.use_git:
            git_push(TOKEN, user, args.repo)
        else:
            api_push(TOKEN, user, args.repo)

    wf = find_workflow(TOKEN, user, args.repo)
    log(f"== workflow id={wf}")

    r = api("GET", f"/repos/{user}/{args.repo}/actions/workflows/{wf}/runs?per_page=1", TOKEN)
    since = time.time()
    if r.status_code == 200 and r.json().get("workflow_runs"):
        last = r.json()["workflow_runs"][0]
        since = datetime.fromisoformat(last["created_at"].replace("Z", "+00:00")).timestamp()

    inputs = {
        "folder_id": args.folder_id,
        "mode": args.mode,
        "include": args.include,
        "max_total_gb": str(args.max_gb),
        "publish_release": "true",
    }
    log(f"== 参数 {json.dumps(inputs, ensure_ascii=False)}")
    dispatch(TOKEN, user, args.repo, wf, inputs)

    run = wait_run(TOKEN, user, args.repo, since)
    ok = run.get("conclusion") == "success"
    dump_logs(TOKEN, user, args.repo, run["id"])

    if args.mode == "list":
        show_manifest(TOKEN, user, args.repo, run["id"])
        log("\n== 看清大小后，跑：")
        log(f"   py -3.12 automate.py download --max-gb <合适的值>")
        return 0 if ok else 1

    if ok:
        fetch_release(TOKEN, user, args.repo, DL_DIR)
    else:
        log("\n!! 运行失败，上面是日志尾部。把这段贴给我。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
