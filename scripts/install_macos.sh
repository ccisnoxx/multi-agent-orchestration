#!/bin/bash
set -euo pipefail

SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
TARGET="/Users/sc/.codex/skills/multi-agent-orchestration"
SKIP_TESTS=0
SKIP_DOCTOR=0
NO_BACKUP=0

usage() {
  cat <<'EOF'
Usage: ./scripts/install_macos.sh [options]

Options:
  --target PATH    Installation target.
  --skip-tests     Skip source compile/unit/example checks.
  --skip-doctor    Skip active Codex config and Agent TOML doctor check.
  --no-backup      Do not create a timestamped backup of an existing target.
  -h, --help       Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target)
      TARGET=${2:?missing path after --target}
      shift 2
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    --skip-doctor)
      SKIP_DOCTOR=1
      shift
      ;;
    --no-backup)
      NO_BACKUP=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ "$SOURCE" = "$TARGET" ]; then
  echo "ERROR: source and target are the same directory" >&2
  exit 2
fi

if [ "$SKIP_TESTS" -eq 0 ]; then
  echo "[1/4] 验证源目录"
  "$SOURCE/bin/verify-skill"
else
  echo "[1/4] 跳过源目录测试"
fi

mkdir -p "$(dirname "$TARGET")"
if [ -d "$TARGET" ] && [ "$NO_BACKUP" -eq 0 ]; then
  timestamp=$(date +%Y%m%d-%H%M%S)
  backup="${TARGET}.backup-${timestamp}"
  echo "[2/4] 备份现有目录到 $backup"
  cp -a "$TARGET" "$backup"
else
  echo "[2/4] 无需备份"
fi

mkdir -p "$TARGET"
echo "[3/4] 同步到 $TARGET"
rsync -a --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  "$SOURCE/" "$TARGET/"
chmod +x \
  "$TARGET/bin/skill-python" \
  "$TARGET/bin/work-plan" \
  "$TARGET/bin/regenerate-examples" \
  "$TARGET/bin/verify-skill" \
  "$TARGET/scripts/work_plan.py" \
  "$TARGET/scripts/regenerate_examples.py" \
  "$TARGET/scripts/verify_skill.py" \
  "$TARGET/scripts/install_macos.sh"

if [ "$SKIP_DOCTOR" -eq 0 ]; then
  echo "[4/4] 检查当前 Codex 配置"
  "$TARGET/bin/work-plan" \
    --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
    --codex-config "${CODEX_CONFIG:-$HOME/.codex/config.toml}" \
    doctor
else
  echo "[4/4] 跳过 Doctor"
fi

cat <<EOF
安装完成：$TARGET

下一步：
  cd "$TARGET"
  ./bin/verify-skill
  git status --short
  git diff --stat
  git add .
  git commit -m "fix: use the dedicated Python runtime for local verification"
  git push origin main
EOF
