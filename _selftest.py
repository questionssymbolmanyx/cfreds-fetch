#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：不联网验证解析/断点续传/校验三条主链路。"""
import hashlib
import os
import re
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import fetch_drive as F

SAMPLE = """
<div class="flip-entry-list">
<div class="flip-entry" id="entry-1qHIweIPf_0-LhdssFqUgF_RhEKd_23Gc" tabindex="0"><div class="flip-entry-info">
<a href="https://drive.google.com/drive/folders/1qHIweIPf_0-LhdssFqUgF_RhEKd_23Gc" target="_blank">
<div class="flip-entry-title">2017_June</div><div class="flip-entry-last-modified"><div>12/17/19</div></div></a></div></div>
<div class="flip-entry" id="entry-0B1aBvVt_vSSeUkVoOXJVM3FwSEk" tabindex="0"><div class="flip-entry-info">
<a href="https://drive.google.com/file/d/0B1aBvVt_vSSeUkVoOXJVM3FwSEk/view?usp=drive_web&amp;resourcekey=0-8F_S_JYFzd5Xu86o2VZknA" target="_blank">
<div class="flip-entry-title">DF001_microsd_external.001</div><div class="flip-entry-last-modified"><div>7/1/17</div></div></a></div></div>
</div>
"""


class FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        pass


class FakeSess:
    def __init__(self, text):
        self.text = text

    def get(self, *a, **k):
        return FakeResp(self.text)


def test_parse():
    items = F.list_folder(FakeSess(SAMPLE), "x")
    assert len(items) == 2, items
    assert items[0] == {
        "kind": "folder", "id": "1qHIweIPf_0-LhdssFqUgF_RhEKd_23Gc",
        "name": "2017_June", "resourcekey": "",
    }, items[0]
    assert items[1]["kind"] == "file"
    assert items[1]["name"] == "DF001_microsd_external.001"
    assert items[1]["resourcekey"] == "0-8F_S_JYFzd5Xu86o2VZknA", items[1]
    print("parse            OK")


# --------------------------------------------------------------------------- #
# 断点续传：本地起一个支持 Range 的服务
# --------------------------------------------------------------------------- #
PAYLOAD = os.urandom(3 * 1024 * 1024 + 123)


class RangeHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        total = len(PAYLOAD)
        if rng:
            m = re.match(r"bytes=(\d+)-$", rng)
            start = int(m.group(1))
            if start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total-1}/{total}")
        else:
            body = PAYLOAD
            self.send_response(200)
            self.send_header("Content-Range", f"bytes 0-{total-1}/{total}")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_download():
    srv = HTTPServer(("127.0.0.1", 0), RangeHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    F.DL_URL = f"http://127.0.0.1:{port}/download"
    F._dl_params = lambda fid, rk="": {}

    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "sub dir" / "DF001_microsd_external.001"
    item = {"id": "abc", "path": "sub dir/DF001_microsd_external.001", "size": len(PAYLOAD)}

    assert F.download_one(F.new_session(), item, dest), "首次下载失败"
    assert dest.read_bytes() == PAYLOAD, "内容不一致"

    # 模拟断点：删掉成品，留下 1/3 的 .part
    part = dest.with_suffix(dest.suffix + ".part")
    dest.replace(part)
    with open(part, "r+b") as fh:
        fh.truncate(len(PAYLOAD) // 3)
    assert F.download_one(F.new_session(), item, dest), "续传失败"
    assert dest.read_bytes() == PAYLOAD, "续传内容不一致"

    # 模拟「本地已完整」的 416 分支
    dest.replace(part)
    with open(part, "r+b") as fh:
        fh.write(PAYLOAD)
    assert F.download_one(F.new_session(), item, dest), "416 分支失败"
    assert dest.read_bytes() == PAYLOAD

    # 校验
    dest.write_bytes(b"hello")
    (dest.parent / "DF001_microsd_external.md5").write_text(
        hashlib.md5(b"hello").hexdigest() + " *DF001_microsd_external.001\n"
    )
    checked, probs = F.verify_dir(tmp)
    assert checked == 1 and not probs, (checked, probs)

    dest.write_bytes(b"tampered")
    checked, probs = F.verify_dir(tmp)
    assert checked == 1 and len(probs) == 1, (checked, probs)

    # 裸 hash 的兜底写法
    dest.write_bytes(b"hello")
    (dest.parent / "DF001_microsd_external.md5").write_text(hashlib.md5(b"hello").hexdigest() + "\n")
    (dest.parent / "DF001_microsd_external.001").write_bytes(b"hello")
    checked, probs = F.verify_dir(tmp)
    assert not probs, probs

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print("download/resume  OK")
    print("verify           OK")


if __name__ == "__main__":
    test_parse()
    test_download()
    print("\nALL GREEN")
