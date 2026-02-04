# setup_laptop.ps1 — Full laptop environment setup
# Run: powershell -ExecutionPolicy Bypass -File setup_laptop.ps1

$ErrorActionPreference = "Stop"

Write-Host "`n=== TG Web Auth: Laptop Setup ===`n" -ForegroundColor Cyan

# Check prerequisites
Write-Host "[1/8] Checking prerequisites..." -ForegroundColor Yellow
$python = python --version 2>&1
$node = node --version 2>&1
$git = git --version 2>&1
Write-Host "  Python: $python"
Write-Host "  Node:   $node"
Write-Host "  Git:    $git"

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python, Node.js, or Git not found. Install them first." -ForegroundColor Red
    exit 1
}

# Clone (skip if already exists)
Write-Host "`n[2/8] Cloning project..." -ForegroundColor Yellow
$projectDir = "D:\TGFRAG\tg-web-auth"
if (Test-Path $projectDir) {
    Write-Host "  Project already exists at $projectDir, pulling latest..."
    Set-Location $projectDir
    git pull
} else {
    $parentDir = Split-Path $projectDir -Parent
    if (-not (Test-Path $parentDir)) { New-Item -ItemType Directory -Path $parentDir | Out-Null }
    Set-Location $parentDir
    git clone https://github.com/convyrtech/tg124124.git tg-web-auth
    Set-Location $projectDir
}

# Python venv
Write-Host "`n[3/8] Setting up Python venv..." -ForegroundColor Yellow
if (-not (Test-Path "venv")) {
    python -m venv venv
}
& ".\venv\Scripts\activate.ps1"
pip install -r requirements.txt
Write-Host "  Downloading Camoufox browser..."
python -m camoufox fetch

# Node.js
Write-Host "`n[4/8] Installing Node.js dependencies..." -ForegroundColor Yellow
npm install

# Tests
Write-Host "`n[5/8] Running tests..." -ForegroundColor Yellow
pytest -v --tb=short

# Claude Code check
Write-Host "`n[6/8] Checking Claude Code..." -ForegroundColor Yellow
$claudeInstalled = $false
try {
    claude --version 2>&1 | Out-Null
    $claudeInstalled = $true
    Write-Host "  Claude Code already installed"
} catch {
    Write-Host "  Claude Code not found. Installing..."
    npm install -g @anthropic-ai/claude-code
}

# Global plugins
Write-Host "`n[7/8] Installing global Claude plugins..." -ForegroundColor Yellow
$globalPlugins = @(
    "github@claude-plugins-official",
    "ralph-loop@claude-plugins-official",
    "pyright-lsp@claude-plugins-official",
    "code-simplifier@claude-plugins-official",
    "serena@claude-plugins-official"
)
foreach ($plugin in $globalPlugins) {
    Write-Host "  Installing $plugin..."
    claude plugins install $plugin 2>&1 | Out-Null
}

# Project plugins
Write-Host "`n[8/8] Installing project Claude plugins..." -ForegroundColor Yellow
$projectPlugins = @(
    "superpowers@claude-plugins-official",
    "playwright@claude-plugins-official",
    "context7@claude-plugins-official",
    "feature-dev@claude-plugins-official",
    "code-review@claude-plugins-official",
    "pr-review-toolkit@claude-plugins-official",
    "frontend-design@claude-plugins-official",
    "python-development@claude-code-workflows",
    "security-scanning@claude-code-workflows",
    "developer-essentials@claude-code-workflows",
    "think-through@ilia-izmailov-plugins"
)
foreach ($plugin in $projectPlugins) {
    Write-Host "  Installing $plugin..."
    claude plugins install $plugin 2>&1 | Out-Null
}

# Summary
Write-Host "`n========================================" -ForegroundColor Green
Write-Host "  SETUP COMPLETE!" -ForegroundColor Green
Write-Host "========================================`n" -ForegroundColor Green

Write-Host "Remaining manual steps:" -ForegroundColor Yellow
Write-Host "  1. Copy accounts/ folder from main PC (ENCRYPTED!)"
Write-Host "  2. Make sure VPN is OFF"
Write-Host "  3. Test proxy: python -m src.cli check --proxy 'socks5:host:port:user:pass'"
Write-Host "  4. Run: claude  (start Claude Code)`n"
