# 一键把 CFReDS 搬运工具推到你自己的 GitHub 仓库，并告诉你怎么触发
# 用法（在 PowerShell 里，于本文件所在目录执行）：
#   powershell -ExecutionPolicy Bypass -File .\push.ps1
# 或指定参数：
#   .\push.ps1 -User 你的GitHub用户名 -Repo cfreds-fetch

param(
    [string]$Repo  = "cfreds-fetch",
    [string]$User  = "",
    [string]$Email = ""
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "没找到 git，先装 Git for Windows"
}

if (-not $User)  { $User  = (git config --global user.name) }
if (-not $User)  { $User  = Read-Host "你的 GitHub 用户名" }
if (-not $Email) { $Email = "$User@users.noreply.github.com" }

Write-Host ""
Write-Host "== 目标仓库: https://github.com/$User/$Repo" -ForegroundColor Cyan
Write-Host "== 提交身份: $User <$Email>"
Write-Host ""

# 1) 本地初始化
if (-not (Test-Path ".git")) { git init -b main | Out-Null }
git config user.name  $User
git config user.email $Email

# 2) 提交
git add -A
$dirty = (git status --porcelain)
if ($dirty) {
    git commit -m "chore: CFReDS DF001 fetch toolkit" | Out-Null
    Write-Host "== 已提交本地文件" -ForegroundColor Green
} else {
    Write-Host "== 工作区干净，跳过提交"
}

# 3) 远端
$url = "https://github.com/$User/$Repo.git"
git remote remove origin 2>$null
git remote add origin $url

Write-Host ""
Write-Host "== 如果仓库还没建，先在浏览器建一个【空的公开仓库】：" -ForegroundColor Yellow
Write-Host "   https://github.com/new?name=$Repo&visibility=public" -ForegroundColor Yellow
Write-Host "   （不要勾 README / .gitignore / license，否则推送会被拒）" -ForegroundColor Yellow
Read-Host "建好后按回车继续"

# 4) 推送（Git Credential Manager 会弹浏览器让你登录一次）
git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "推送失败。逐个排查：" -ForegroundColor Red
    Write-Host "  a) 仓库不存在或名字不对 -> 先建空仓库（不要勾 README）"
    Write-Host "  b) 登录弹窗没出来/登录失败 -> 用 PAT 当密码："
    Write-Host "     https://github.com/settings/tokens 建一个 classic token，勾 repo 权限，"
    Write-Host "     然后 git push 时用户名填 $User，密码填那个 token"
    Write-Host "  c) 仓库里已有 README/文件导致 non-fast-forward -> git pull --rebase origin main 后重试"
    throw "推送未完成"
}

Write-Host ""
Write-Host "== 推送成功" -ForegroundColor Green
Write-Host ""
Write-Host "接下来（浏览器里点三下）：" -ForegroundColor Cyan
Write-Host "  1. 打开 https://github.com/$User/$Repo/actions"
Write-Host "  2. 左边点 fetch-cfreds -> 右边 Run workflow"
Write-Host "     mode=list, publish_release=false  -> 先看清单（1 分钟）"
Write-Host "  3. 再跑一次 mode=download, publish_release=true -> 完成后在 Releases 页取件"
Write-Host ""
Write-Host "Release 页: https://github.com/$User/$Repo/releases" -ForegroundColor Cyan
