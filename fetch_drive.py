#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CFReDS / VTO Drone Forensics — Google Drive 数据搬运器
======================================================
用途：在「能访问 Google」的机器上（GitHub Actions runner / 海外 VPS / 挂了代理的本机）
      把某个 DF0xx 的公开 Google Drive 文件夹完整拉下来，并产出可校验的清单。

两个动作：
  list      只枚举文件树 + 探测每个文件大小，不下载正文。产物 manifest.csv / manifest.json
  download  按清单下载（支持断点续传 / 过滤 / 总量上限），下完自动比对同目录 .md5 .sha1

示例：
  python fetch_drive.py list     --folder-id 1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw --out out
  python fetch_drive.py download --folder-id 1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw --out out \
                                --include '\\.001$|README' --max-total-gb 20

设计说明：
  * 文件夹枚举走 embeddedfolderview（公开文件夹免鉴权、不需要 API key），不需要 Google 账号。
  * 下载走 drive.usercontent.google.com + confirm=t，绕开「无法扫描病毒」中间页；
    用 Range 头实现断点续传，服务器忽略 Range 时自动重下。
  * 全程只用 requests，不依赖 gdown（gdown 的文件夹上限 50 个文件、cookie 逻辑会静默少下）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

VIEW_URL = "https://drive.google.com/embeddedfolderview?id={fid}#list"
DL_URL = "https://drive.usercontent.google.com/download"
# 备用下载端点：usercontent 偶尔对某些区域/机房 IP 返回 404 或确认页，退回老接口
DL_FALLBACK = "https://drive.google.com/uc"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

HASH_EXT = {".md5", ".sha1", ".sha256", ".sha512"}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def human(n: int | None) -> str:
    if not n or n < 0:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f}{u}" if u == "B" else f"{f:.2f}{u}"
        f /= 1024
    return str(n)


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


# --------------------------------------------------------------------------- #
# 1. 枚举文件夹（免鉴权）
# --------------------------------------------------------------------------- #
def list_folder(sess: requests.Session, folder_id: str) -> list[dict]:
    """解析 embeddedfolderview 页面，返回该层直接子项。"""
    url = VIEW_URL.format(fid=folder_id)
    for attempt in range(1, 5):
        try:
            r = sess.get(url, timeout=30)
            if r.status_code == 200 and "flip-entry" in r.text:
                break
            if r.status_code == 429:
                raise requests.RequestException("429 rate limited")
            r.raise_for_status()
        except requests.RequestException as e:
            log(f"  ! 列举失败 ({attempt}/4): {e}")
            time.sleep(3 * attempt)
    else:
        raise SystemExit(f"无法枚举文件夹 {folder_id}（Google 侧拒绝或断网）")

    items: list[dict] = []
    for chunk in r.text.split('class="flip-entry"')[1:]:
        m_id = re.search(r'id="entry-([A-Za-z0-9_-]+)"', chunk)
        m_href = re.search(r'href="(https://drive\.google\.com/[^"]+)"', chunk)
        m_title = re.search(r'class="flip-entry-title">(.*?)</div>', chunk, re.S)
        if not (m_id and m_href and m_title):
            continue
        href = htmllib.unescape(m_href.group(1))
        name = htmllib.unescape(_strip_tags(m_title.group(1))).strip()
        rk = re.search(r"[?&]resourcekey=([A-Za-z0-9_-]+)", href)
        items.append(
            {
                "kind": "folder" if "/folders/" in href else "file",
                "id": m_id.group(1),
                "name": name,
                "resourcekey": rk.group(1) if rk else "",
            }
        )
    return items


def walk(sess: requests.Session, folder_id: str, prefix: str = "", depth: int = 0) -> list[dict]:
    """递归展开，返回带 path 的扁平文件列表（folder 项被展开、不保留）。"""
    if depth > 6:
        log(f"  ! 层级过深，跳过 {prefix}")
        return []
    out: list[dict] = []
    for it in list_folder(sess, folder_id):
        path = f"{prefix}/{it['name']}" if prefix else it["name"]
        if it["kind"] == "folder":
            log(f"  [dir ] {path}/")
            out.extend(walk(sess, it["id"], path, depth + 1))
        else:
            it["path"] = path
            out.append(it)
            log(f"  [file] {path}")
    return out


# --------------------------------------------------------------------------- #
# 2. 解析直链 + 探测大小
# --------------------------------------------------------------------------- #
def _dl_params(fid: str, rk: str = "") -> dict:
    p = {"id": fid, "export": "download", "confirm": "t"}
    if rk:
        p["resourcekey"] = rk
    return p


def resolve_url(sess: requests.Session, fid: str, rk: str = "") -> str | None:
    """把 file id 解析成真正的下载直链。

    依次尝试 usercontent 与老 uc 接口；两者都可能返回「无法扫描病毒」的确认页，
    这时从表单里抠出 action 再请求一次。
    """
    for endpoint in (DL_URL, DL_FALLBACK):
        try:
            r = sess.get(endpoint, params=_dl_params(fid, rk), stream=True, timeout=40, allow_redirects=True)
        except requests.RequestException:
            continue
        ctype = r.headers.get("Content-Type", "")
        if "text/html" not in ctype:
            url = r.url
            r.close()
            return url

        body = r.text
        r.close()
        m = re.search(r'<form[^>]*\baction="([^"]+)"', body)
        if not m:
            continue
        action = htmllib.unescape(m.group(1))
        params = {}
        for tag in re.findall(r"<input[^>]*>", body):
            nm = re.search(r'\bname="([^"]+)"', tag)
            vl = re.search(r'\bvalue="([^"]*)"', tag)
            if nm:
                params[nm.group(1)] = htmllib.unescape(vl.group(1)) if vl else ""
        if not action.startswith("http"):
            action = "https://drive.usercontent.google.com" + action
        try:
            r2 = sess.get(action, params=params, stream=True, timeout=40, allow_redirects=True)
            url = r2.url
            r2.close()
            if "text/html" not in r2.headers.get("Content-Type", ""):
                return url
        except requests.RequestException:
            continue
    return None


def probe_size(sess: requests.Session, fid: str, rk: str = "") -> int | None:
    """只取 1 字节，靠 Content-Range 拿总长度。"""
    try:
        r = sess.get(
            DL_URL,
            params=_dl_params(fid, rk),
            headers={"Range": "bytes=0-0"},
            stream=True,
            timeout=40,
            allow_redirects=True,
        )
        if "text/html" not in r.headers.get("Content-Type", ""):
            cr = r.headers.get("Content-Range", "")
            cl = r.headers.get("Content-Length")
            r.close()
            m = re.search(r"/(\d+)\s*$", cr)
            if m:
                return int(m.group(1))
            if cl and cl.isdigit():
                return int(cl)
        else:
            r.close()
    except requests.RequestException:
        pass
    # 兜底：走解析后的直链
    url = resolve_url(sess, fid, rk)
    if not url:
        return None
    try:
        r = sess.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=40, allow_redirects=True)
        cr = r.headers.get("Content-Range", "")
        r.close()
        m = re.search(r"/(\d+)\s*$", cr)
        return int(m.group(1)) if m else None
    except requests.RequestException:
        return None


# --------------------------------------------------------------------------- #
# 3. 下载（断点续传）
# --------------------------------------------------------------------------- #
def download_one(sess: requests.Session, item: dict, dest: Path, tries: int = 5) -> bool:
    total = item.get("size")
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    params = _dl_params(item["id"], item.get("resourcekey", ""))
    resolved: str | None = None  # 收到「病毒扫描」确认页时，缓存解析出的直链

    for attempt in range(1, tries + 1):
        pos = part.stat().st_size if part.exists() else 0
        if total and pos == total:
            part.replace(dest)
            return True
        headers = {"Range": f"bytes={pos}-"} if pos else {}
        url = resolved or DL_URL
        try:
            with sess.get(
                url,
                params=None if resolved else params,
                headers=headers,
                stream=True,
                timeout=60,
            ) as r:
                if "text/html" in r.headers.get("Content-Type", ""):
                    if resolved:
                        log("    ! 直链仍返回 HTML，放弃")
                        return False
                    resolved = resolve_url(sess, item["id"], item.get("resourcekey", ""))
                    log(f"    ~ 命中确认页，已解析直链：{'成功' if resolved else '失败'}")
                    if not resolved:
                        return False
                    continue
                if r.status_code == 416:  # Range 越界 = 本地已完整
                    part.replace(dest)
                    return True
                if r.status_code == 403:
                    wait = 60 * attempt
                    log(f"    ~ 403 配额限制，等待 {wait}s 后重试 ({attempt}/{tries})")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                mode = "ab" if (pos and r.status_code == 206) else "wb"
                if mode == "wb":
                    pos = 0
                got = pos
                last = time.time()
                with open(part, mode) as fh:
                    for chunk in r.iter_content(1024 * 256):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        got += len(chunk)
                        if time.time() - last > 5:
                            pct = f"{got * 100 / total:.1f}%" if total else "?"
                            log(f"    {pct}  {human(got)} / {human(total)}")
                            last = time.time()
            # 防呆：绝不能把错误页当成镜像写进 .001
            if part.exists() and part.stat().st_size < 4096:
                head = part.read_bytes()[:512].lstrip().lower()
                if head.startswith(b"<!doctype") or head.startswith(b"<html"):
                    part.unlink()
                    log("    ! 服务器返回的是 HTML 页面而非文件，已丢弃，重新解析直链")
                    resolved = None
                    continue
            if total is None or part.stat().st_size == total:
                part.replace(dest)
                return True
            log(f"    ! 长度不符 ({part.stat().st_size} != {total})，重试")
        except requests.RequestException as e:
            log(f"    ! 网络错误 ({attempt}/{tries}): {e}")
            time.sleep(5 * attempt)
    log("    x 放弃（可重跑本脚本，会从 .part 续传）")
    return False


# --------------------------------------------------------------------------- #
# 4. 校验
# --------------------------------------------------------------------------- #
def digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_hashfile(p: Path) -> dict[str, str]:
    """支持 `hash *name` / `hash  name` / `hash name` 三种写法。"""
    out: dict[str, str] = {}
    try:
        for line in p.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^([0-9a-fA-F]{16,128})\s+\*?(.+)$", line)
            if m:
                out[m.group(2).strip()] = m.group(1).lower()
    except OSError:
        pass
    return out


def verify_dir(root: Path) -> tuple[int, list[str]]:
    """按目录内 *.md5 / *.sha1 校验同名文件。返回 (校验项数, 失败列表)。"""
    checked, problems = 0, []
    for hf in sorted(root.rglob("*")):
        if hf.suffix.lower() not in HASH_EXT:
            continue
        algo = hf.suffix.lower().lstrip(".")
        base = hf.parent

        pairs = parse_hashfile(hf)
        if pairs:
            targets = [(base / name, h) for name, h in pairs.items()]
        else:
            # 兜底：hash 文件名叫 xxx.001.md5，正文只有一个裸 hash → 校验 xxx.001
            raw = hf.read_text(errors="ignore").strip().split()
            stem = hf.name[: -len(hf.suffix)]
            if not raw or not re.fullmatch(r"[0-9a-fA-F]{16,128}", raw[0]):
                continue
            targets = [(base / stem, raw[0].lower())]

        for target, expect in targets:
            if not target.is_file():
                continue
            checked += 1
            actual = digest(target, algo)
            if actual == expect.lower():
                log(f"  OK   {algo.upper():<6} {target.name}")
            else:
                problems.append(f"{target}\n  期望 {algo}: {expect}\n  实得 {algo}: {actual}")
                log(f"  FAIL {algo.upper():<6} {target.name}")
    return checked, problems


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="CFReDS Google Drive 搬运器")
    ap.add_argument("action", choices=["list", "download"])
    ap.add_argument("--folder-id", required=True, help="Google Drive 文件夹 ID")
    ap.add_argument("--out", default="out", help="输出目录")
    ap.add_argument("--include", default="", help="只下载匹配该正则的路径")
    ap.add_argument("--exclude", default="", help="跳过匹配该正则的路径")
    ap.add_argument("--skip-hashes", action="store_true", help="跳过下载 .md5/.sha1 校验文件本身")
    ap.add_argument("--max-total-gb", type=float, default=0, help="总量上限（GB），0 为不限")
    ap.add_argument("--timeout-probe", type=int, default=0, help="探测大小超时秒数，0 为不限")
    args = ap.parse_args()

    sess = new_session()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    log(f"== 枚举 {args.folder_id}")
    files = walk(sess, args.folder_id)
    log(f"== 共 {len(files)} 个文件，开始探测大小")

    for i, it in enumerate(files, 1):
        it["size"] = probe_size(sess, it["id"], it.get("resourcekey", ""))
        log(f"  [{i}/{len(files)}] {human(it['size']):>10}  {it['path']}")
        if args.timeout_probe and i >= args.timeout_probe:
            log("  (达到探测数量上限，其余大小留空)")
            for rest in files[i:]:
                rest.setdefault("size", None)
            break

    total = sum(x["size"] or 0 for x in files)
    unknown = sum(1 for x in files if not x.get("size"))

    (out / "manifest.json").write_text(
        json.dumps({"folder_id": args.folder_id, "count": len(files), "total_bytes": total,
                    "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with open(out / "manifest.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "size_bytes", "size_human", "id", "resourcekey"])
        for it in files:
            w.writerow([it["path"], it["size"] or "", human(it["size"]), it["id"], it.get("resourcekey", "")])

    log("")
    log(f"== 合计 {human(total)}（{total} 字节），未知大小 {unknown} 个")
    log(f"== 清单：{out/'manifest.csv'}")
    if unknown:
        log("   ! 有文件大小探测失败（多为 Google 限流），可稍后重跑 list")

    if args.action == "list":
        return 0

    # ---- 过滤 ----
    inc = re.compile(args.include) if args.include else None
    exc = re.compile(args.exclude) if args.exclude else None
    picked = []
    for it in files:
        is_hash = Path(it["path"]).suffix.lower() in HASH_EXT
        # 过滤时始终捎上 .md5/.sha1，否则没法校验
        if inc and not inc.search(it["path"]) and not is_hash:
            continue
        if exc and exc.search(it["path"]):
            continue
        if args.skip_hashes and Path(it["path"]).suffix.lower() in HASH_EXT:
            continue
        picked.append(it)

    want = sum(x["size"] or 0 for x in picked)
    log(f"== 选中 {len(picked)} 个文件，预计 {human(want)}")
    if args.max_total_gb and want > args.max_total_gb * 1024**3:
        log(f"!! 超过 --max-total-gb {args.max_total_gb}GB，已中止（调大上限或用 --include 分批）")
        return 2

    ok = 0
    for i, it in enumerate(picked, 1):
        dest = out / it["path"]
        if dest.exists() and (not it.get("size") or dest.stat().st_size == it["size"]):
            log(f"== [{i}/{len(picked)}] 已存在，跳过 {it['path']}")
            ok += 1
            continue
        log(f"== [{i}/{len(picked)}] 下载 {it['path']}  ({human(it.get('size'))})")
        if download_one(sess, it, dest):
            ok += 1

    log(f"== 下载完成 {ok}/{len(picked)}")

    # ---- 校验 ----
    log("== 校验 hash 文件")
    checked, problems = verify_dir(out)
    log(f"== 已校验 {checked} 个文件")
    if problems:
        (out / "VERIFY_FAILED.txt").write_text("\n\n".join(problems), encoding="utf-8")
        log(f"!! {len(problems)} 项校验失败，详见 {out/'VERIFY_FAILED.txt'}")
        return 3
    log("== 校验通过（若目录内无 hash 文件则视为跳过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
