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

resolve_python() {
  if [ -n "${MULTI_AGENT_ORCHESTRATION_PYTHON:-}" ]; then
    printf '%s\n' "$MULTI_AGENT_ORCHESTRATION_PYTHON"
    return
  fi
  local dedicated="$HOME/.local/share/multi-agent-orchestration/.venv/bin/python"
  if [ -x "$dedicated" ]; then
    printf '%s\n' "$dedicated"
    return
  fi
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    command -v python3
    return
  fi
  echo "ERROR: Python >= 3.10 not found" >&2
  exit 2
}

PY=$(resolve_python)

if [ "$SKIP_TESTS" -eq 0 ]; then
  echo "[1/4] 验证源目录"
  "$PY" -m compileall -q "$SOURCE/scripts" "$SOURCE/tests"
  "$PY" -m unittest discover -s "$SOURCE/tests" -v
  before=$(mktemp)
  after=$(mktemp)
  trap 'rm -f "$before" "$after"' EXIT
  (
    cd "$SOURCE"
    find examples -type f -print0 | sort -z | xargs -0 shasum -a 256
  ) > "$before"
  "$PY" "$SOURCE/scripts/regenerate_examples.py"
  (
    cd "$SOURCE"
    find examples -type f -print0 | sort -z | xargs -0 shasum -a 256
  ) > "$after"
  if ! cmp -s "$before" "$after"; then
    echo "ERROR: regenerating examples changed committed outputs" >&2
    diff -u "$before" "$after" || true
    exit 2
  fi
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
chmod +x "$TARGET/bin/work-plan" "$TARGET/scripts/work_plan.py" \
  "$TARGET/scripts/regenerate_examples.py" "$TARGET/scripts/install_macos.sh"

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
  git status --short
  git diff --stat
  git add .
  git commit -m "feat: optimize fixed-profile multi-agent orchestration"
  git push origin main
EOF
