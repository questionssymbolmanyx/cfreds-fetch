# CFReDS DF001 → 国内落地（零成本方案）

目标：把 NIST CFReDS 页面上 **DF001（DJI Phantom 3）** 的取证镜像，从只读的 Google Drive 搬到你能下载的地方，
**不需要 VPS、不需要梯子、不需要 Google 账号**，只需要一个免费 GitHub 账号 + 浏览器。

---

## 0. 我已经替你摸清的 DF001 结构（2026-10-26 实测）

页面上的 `DF001` 指向文件夹 `1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw`，名字 `df001_DJI_Phantom_3`，里面**只有两层嵌套、3 个真正的数据文件**：

```
df001_DJI_Phantom_3/                         1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw
├── 2017_June/                               1qHIweIPf_0-LhdssFqUgF_RhEKd_23Gc
│   ├── SDCard_external/                     1Rd4nPrzPO-YgdtVEDei92lOUFWw4APFW
│   │   ├── DF001_microsd_external.001       ← 机身 TF 卡的 dd/E01 全盘镜像（主检材）
│   │   ├── DF001_microsd_external.md5       ← 校验值
│   │   └── DF001_microsd_external.sha1      ← 校验值
│   ├── README_DF001_Drone_Forensics_Program.txt      ← 采集说明（必读）
│   ├── README_..._20170822.txt / .md5 / .sha1
└── 2018_June/                               1S2K0FARsqc2aHKFRFUVwELsOa83iYv2N
    ├── mobile_android/Media01.zip           1y6w4r2dXl34PD9z5_PQ59MYHRGWWn1W-
    ├── mobile_iOS/ios_backup.zip            1rHzC1ExIkhYNq_fT6duEDJixUnOetuFc
    └── README_DF001_..._June_2018.txt / .MD5 / .SHA1
```

要点：

- **只要 3 个数据文件**，所以整架搬完是可行的（不像整站 79 架是 TB 级）。
- 每个数据文件旁边**都配了 md5 / sha1**，这是取证场景必须用的——中转过程任何位翻转都会毁掉结论。
- `README_*.txt` 里写明采集方式、设备状态；先看它再动手分析。
- 注意 `.001` 是 **E01/分段镜像**风格命名，拿到后别直接双击，用 FTK Imager / `ewfmount` / `xmount` 打开。

---

## 1. 方案：借 GitHub Actions 的免费机器当"海外中转"

思路：GitHub 的 runner 在境外，能直连 Google Drive；让它跑脚本下载 → 打包成分卷 → 发布成 **Release**；
你在国内从 `github.com` 下 Release 资产（github.com 本身能通，慢的话加加速前缀）。

### 路线 A（推荐，有 git 就走这条）：本机一键推送

本机已装 git（2.55），且到 GitHub 的 HTTPS 实测通畅，所以不用网页上传：

```powershell
cd cfreds-df001
powershell -ExecutionPolicy Bypass -File .\push.ps1
```

脚本会：本地 `git init` → 提交 → 提示你在浏览器点一下
`https://github.com/new?name=cfreds-fetch&visibility=public` 建空仓库（**别勾 README**）→ 回车后推送。
第一次推送时 Git Credential Manager 会弹浏览器让你登录一次 GitHub。

推完直接跳到下面 **③ 先跑一次"只看清单"**。

### 路线 B（没有 git / 不想开终端）：网页上传

**① 注册/登录 GitHub** → 新建一个 **Public** 仓库（公开库的 Actions 不计分钟数，白嫖关键），名字随意如 `cfreds-fetch`。

**② 上传文件**（网页操作即可）

- `fetch_drive.py` —— 点 `Add file → Upload files` 拖进去
- `.github/workflows/fetch.yml` —— **不要**拖（点开头的目录容易被系统隐藏），用 `Add file → Create new file`，
  文件名直接输入 `.github/workflows/fetch.yml`（斜杠会自动建目录），把内容粘进去，Commit。

### 然后（两条路线都一样）

**③ 先跑一次"只看清单"**（约 1 分钟，不下数据）

`Actions` → 左侧 `fetch-cfreds` → 右侧 `Run workflow`：

| 参数 | 填 |
|---|---|
| folder_id | `1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw`（已默认） |
| mode | `list` |
| include | 留空 |
| publish_release | `false` |

跑完在该次 run 页面底部下载 **artifact `manifest-1`**，里面 `manifest.csv` 会列出每个文件的**准确大小**。
先看这个数，决定要不要全下（runner 磁盘约 30G 可用）。

**④ 再跑一次真下载**

同样入口，改成：

| 参数 | 填 |
|---|---|
| mode | `download` |
| max_total_gb | `20`（按第③步的实际大小调） |
| publish_release | `true` |

跑完 `Releases` 页面会出现 `cfreds-...-2`，里面是分卷 + `manifest.csv`。

**⑤ 下回本地**（关键：怎么下得快）

Release 资产单文件 <2GB（脚本已自动切 1.8GB 分卷），国内直连 `github.com` 常常只有几十 KB/s，两个办法：

- **加加速前缀**（最快最省事），把直链前面拼上当时可用的 GitHub 加速域名，例如：
  ```
  https://gh-proxy.com/https://github.com/<你的用户名>/cfreds-fetch/releases/download/<tag>/2017_June_SDCard_external_DF001_microsd_external.001.part.000
  ```
  （`gh-proxy.com` / `ghfast.top` / `ghps.cc` 这类站点会轮换，哪个通用哪个，直接扔浏览器地址栏或 IDM 里。）
- **Motrix / aria2c 多线程**：`aria2c -x16 -s16 -c <上面的链接>`

> 分卷名带上了目录前缀（`2017_June_SDCard_external_...`），因为 Release 资产不能有子目录。

**⑥ 合并分卷 + 校验**（务必做）

Release 里附带 **`MERGE.ps1`** 和 **`MERGE.sh`**——它们由 workflow 按实际文件清单生成，会把分卷按顺序合并、把文件放回原始目录结构。**别手敲 `copy /b`**，分卷多了要命。

```powershell
# Windows：把所有资产下到同一个空目录，然后
powershell -ExecutionPolicy Bypass -File .\MERGE.ps1

# 校验（md5/sha1 文件也在同一批资产里）
certutil -hashfile "2017_June\SDCard_external\DF001_microsd_external.001" MD5
```

```bash
# Linux / macOS
bash MERGE.sh
md5sum -c 2017_June/SDCard_external/DF001_microsd_external.md5   # 或 sha1sum -c
```

这套 MERGE 逻辑我已经在本地实测过：造 2.5MB 假镜像 → 切 3 卷 → 用生成的 `MERGE.ps1` 还原 → md5 与原文件一致。

校验不过 → 别分析，重下那一段。取证数据宁可多花两小时，不可带一个错位字节。

---

## 1.5 路线 C：全自动（一个 token，剩下的都由脚本/AI 跑）

本机不能上 Google、但能上 GitHub（`api.github.com` 实测 0.4s 响应）。所以只要给一个 GitHub PAT，
`automate.py` 就能把整条链路跑完：**建仓库 → 推代码 → 触发 workflow → 等结果 → 把 Release 资产拉回本地**。

### 建 token（一次，30 秒）

1. 打开 https://github.com/settings/tokens → `Generate new token (classic)`
2. Note 随便写 `cfreds`，Expiration 选 **7 days**
3. 勾选 **`repo`** 和 **`workflow`** 两个 scope（`workflow` 是触发 Actions 必须的）
4. 生成后复制 `ghp_...`（只显示一次）

### 用法

```powershell
cd cfreds-df001
$env:GITHUB_TOKEN = "ghp_你的token"

# 第一步：只看清单，拿每个文件的准确大小（1 分钟）
py -3.12 automate.py list

# 第二步：真下，跑完自动把资产拉到 .\downloads
py -3.12 automate.py download --max-gb 20
```

脚本会打印每一步状态、失败时把日志尾部打出来。`list` 结束后会直接打印 `manifest.csv` 内容。

**token 安全**：只在环境变量里活一次，不落盘；用完去同一个页面 `Delete` 掉。别把 token 提交进仓库
（`.gitignore` 已挡 `*.part` / `out/`，但 token 请永远只用环境变量传）。

---

## 2. 备选：以后拿到任何代理/海外机器，本地一条命令搞定

`fetch_drive.py` 是独立脚本，只要机器能访问 Google 就能跑，不依赖本仓库的 workflow：

```bash
pip install requests

# 只看清单 + 大小（不下载）
python fetch_drive.py list --folder-id 1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw --out D:\cfreds\DF001

# 全下（断点续传、下完自动校验 md5/sha1）
python fetch_drive.py download --folder-id 1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw --out D:\cfreds\DF001 --max-total-gb 30

# 只下主检材和说明
python fetch_drive.py download --folder-id 1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw --out D:\cfreds\DF001 --include "\.001$|README"
```

有代理时（PowerShell，Clash 默认 7890 之类按实际改）：

```powershell
$env:HTTPS_PROXY="http://127.0.0.1:7890"; $env:HTTP_PROXY="http://127.0.0.1:7890"
py -3.12 fetch_drive.py download --folder-id 1prdK0e5eEefzCShMOw2WfwBtO8EL8bzw --out D:\cfreds\DF001
```

脚本行为：

- 文件夹枚举走 `embeddedfolderview`，**公开文件夹不需要任何凭据/API key**；
- 下载走 `drive.usercontent.google.com` + `confirm=t`，碰到"无法扫描病毒"确认页会自动解析表单再下一跳；
- 全部 `Range` 断点续传，中断后重跑即续上；403 配额限制自动退避重试 5 次；
- 下完自动找同目录 `*.md5/*.sha1` 校验，失败写 `VERIFY_FAILED.txt`。

离线自测（不需要网络）：`py -3.12 _selftest.py` → 应输出 `ALL GREEN`。

---

## 3. 已知的坑

| 坑 | 处理 |
|---|---|
| `403 Too many users have viewed or downloaded this file` | Google 对该文件的每日配额，脚本会退避重试；仍失败就隔 24h 再跑，或换 `folder_id` 顺序分批 |
| Actions 单 job 6 小时上限 | workflow 已设 `timeout-minutes: 350`；真超时就把 `include` 拆成两次跑 |
| runner 磁盘只有 ~30G | 先跑 `list` 看总大小，`max_total_gb` 卡住；超了就只能分批或改用海外 VPS |
| Release 单文件 2GB | 脚本已按 1.8GB 自动 `split`；合并见 ⑥ |
| 90 天无 commit 的仓库 Actions 会自动停用 | 跑之前去 Actions 页面点一下 `Enable` |
| 仓库必须是 Public | 私有库 Actions 只有 2000 分钟/月且 Release 资产占用配额；这个数据本身是公开的，公开库即可 |

---

## 4. 还是搞不定的话：走官方渠道（最正规，值得同时发一封）

数据所有者是 VTO（DHS 资助项目），页面底部就有联系方式：

- `droneforensics@vtoinc.com`
- 抄送 NIST CFReDS 维护人

说明「中国境内高校/司法鉴定机构，Google Drive 不可达，请求提供 FTP / S3 / Azure Blob / 硬盘寄送任一替代分发」，并说明仅用于研究、会正常引用。政府资助的取证数据集，维护方通常有内部 FTP 或愿意临时开个对象存储桶。
