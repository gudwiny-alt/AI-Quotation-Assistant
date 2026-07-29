#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$project_root"

.venv/bin/python -m pytest -q
.venv/bin/python -m PyInstaller --noconfirm --clean packaging/quotation_app.spec
codesign --force --deep --sign - "dist/福建移动铺货报价助手.app"
