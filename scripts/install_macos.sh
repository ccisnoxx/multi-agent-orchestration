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
  echo "[1/5] 验证源目录"
  "$SOURCE/bin/verify-skill"
else
  echo "[1/5] 跳过源目录测试"
fi

mkdir -p "$(dirname "$TARGET")"
if [ -d "$TARGET" ] && [ "$NO_BACKUP" -eq 0 ]; then
  timestamp=$(date +%Y%m%d-%H%M%S)
  backup="${TARGET}.backup-${timestamp}"
  echo "[2/5] 备份现有目录到 $backup"
  cp -a "$TARGET" "$backup"
else
  echo "[2/5] 无需备份"
fi

mkdir -p "$TARGET"
echo "[3/5] 同步到 $TARGET"
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

AUDIT_ROOT=${MULTI_AGENT_AUDIT_ROOT:-${CODEX_HOME:-$HOME/.codex}/audits/multi-agent}
echo "[4/5] 初始化持久化审计目录 $AUDIT_ROOT"
mkdir -p "$AUDIT_ROOT"
chmod 700 "$AUDIT_ROOT" 2>/dev/null || true

if [ "$SKIP_DOCTOR" -eq 0 ]; then
  echo "[5/5] 检查当前 Codex 配置"
  "$TARGET/bin/work-plan" \
    --agents-dir "${CODEX_AGENTS_DIR:-$HOME/.codex/agents}" \
    --codex-config "${CODEX_CONFIG:-$HOME/.codex/config.toml}" \
    doctor
else
  echo "[5/5] 跳过 Doctor"
fi

cat <<EOF
安装完成：$TARGET

下一步：
  cd "$TARGET"
  ./bin/verify-skill
  git status --short
  git diff --stat
  git add .
  git commit -m "feat: add persistent multi-agent audit bundles"
  git push origin main
EOF
