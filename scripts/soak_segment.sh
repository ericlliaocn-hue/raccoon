#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

SESSION_ID="${SOAK_SESSION_ID:-mobile-default}"
STABLE_ROUNDS=1
PERTURB_ROUNDS=1
OPEN_WORLD_ROUNDS=1
OUT_ROOT="${SOAK_OUT_DIR:-output/soak_runs}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

usage() {
  cat <<'EOF'
用法:
  bash scripts/soak_segment.sh [选项]

选项:
  --session-id <id>         同一批次压测会话 ID（推荐按周，例如 2026w17）
  --stable-rounds <n>       live 稳定包轮数（默认 1）
  --perturb-rounds <n>      live 扰动包轮数（默认 1）
  --open-world-rounds <n>   open_world 轮数（默认 1）
  --out-root <dir>          输出根目录（默认 output/soak_runs）
  --python <bin>            Python 命令（默认 python3.11）
  -h, --help                显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session-id)
      SESSION_ID="${2:?missing value for --session-id}"
      shift 2
      ;;
    --stable-rounds)
      STABLE_ROUNDS="${2:?missing value for --stable-rounds}"
      shift 2
      ;;
    --perturb-rounds)
      PERTURB_ROUNDS="${2:?missing value for --perturb-rounds}"
      shift 2
      ;;
    --open-world-rounds)
      OPEN_WORLD_ROUNDS="${2:?missing value for --open-world-rounds}"
      shift 2
      ;;
    --out-root)
      OUT_ROOT="${2:?missing value for --out-root}"
      shift 2
      ;;
    --python)
      PYTHON_BIN="${2:?missing value for --python}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "未找到 Python 命令: ${PYTHON_BIN}" >&2
  exit 1
fi

SESSION_DIR="${OUT_ROOT}/${SESSION_ID}"
mkdir -p "${SESSION_DIR}"

timestamp="$(date '+%Y%m%d-%H%M%S')"
raw_json="${SESSION_DIR}/segment-${timestamp}.json"
segments_jsonl="${SESSION_DIR}/segments.jsonl"
summary_json="${SESSION_DIR}/summary.json"

echo "▶️  开始分段压测: session=${SESSION_ID} timestamp=${timestamp}"
echo "    配置: stable=${STABLE_ROUNDS} perturb=${PERTURB_ROUNDS} open_world=${OPEN_WORLD_ROUNDS}"

set +e
"${PYTHON_BIN}" raccoon.py benchmark all \
  --stable-rounds "${STABLE_ROUNDS}" \
  --perturb-rounds "${PERTURB_ROUNDS}" \
  --open-world-rounds "${OPEN_WORLD_ROUNDS}" \
  --json >"${raw_json}"
bench_exit_code=$?
set -e

python3 - "${raw_json}" "${segments_jsonl}" "${timestamp}" "${bench_exit_code}" <<'PY'
import json
import sys
from pathlib import Path

raw_json_path = Path(sys.argv[1])
jsonl_path = Path(sys.argv[2])
timestamp = sys.argv[3]
exit_code = int(sys.argv[4])

row = {
    "timestamp": timestamp,
    "segment_file": str(raw_json_path),
    "exit_code": exit_code,
}

try:
    payload = json.loads(raw_json_path.read_text(encoding="utf-8"))
    row["overall_pass"] = bool(payload.get("overall_pass"))
    row["core_pass"] = bool(payload.get("pipelines", {}).get("core", {}).get("benchmark_report", {}).get("overall", {}).get("pass"))
    row["live_pass"] = bool(payload.get("pipelines", {}).get("live", {}).get("overall", {}).get("pass"))
    row["open_world_pass"] = bool(payload.get("pipelines", {}).get("open_world", {}).get("overall", {}).get("pass"))
    row["live_execution_success_rate"] = payload.get("pipelines", {}).get("live", {}).get("overall", {}).get("execution_success_rate")
    row["live_browser_chain_execution_success_rate"] = payload.get("pipelines", {}).get("live", {}).get("overall", {}).get("browser_chain_execution_success_rate")
    row["open_world_execution_success_rate"] = payload.get("pipelines", {}).get("open_world", {}).get("overall", {}).get("open_world_execution_success_rate")
    row["open_world_browser_long_chain_success_rate"] = payload.get("pipelines", {}).get("open_world", {}).get("overall", {}).get("browser_long_chain_success_rate")
    row["open_world_false_clarification_rate"] = payload.get("pipelines", {}).get("open_world", {}).get("overall", {}).get("false_clarification_rate")
except Exception as exc:  # pragma: no cover
    row["parse_error"] = str(exc)
    row["overall_pass"] = False

with jsonl_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
PY

python3 - "${segments_jsonl}" "${summary_json}" "${SESSION_ID}" <<'PY'
import json
import sys
from pathlib import Path

jsonl_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
session_id = sys.argv[3]

rows = []
if jsonl_path.exists():
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))

def avg(key: str):
    vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else None

total = len(rows)
passed = sum(1 for r in rows if r.get("overall_pass"))
valid = sum(1 for r in rows if "parse_error" not in r)

summary = {
    "session_id": session_id,
    "total_segments": total,
    "valid_segments": valid,
    "pass_segments": passed,
    "segment_pass_rate": (passed / total) if total else 0.0,
    "avg_live_execution_success_rate": avg("live_execution_success_rate"),
    "avg_live_browser_chain_execution_success_rate": avg("live_browser_chain_execution_success_rate"),
    "avg_open_world_execution_success_rate": avg("open_world_execution_success_rate"),
    "avg_open_world_browser_long_chain_success_rate": avg("open_world_browser_long_chain_success_rate"),
    "avg_open_world_false_clarification_rate": avg("open_world_false_clarification_rate"),
    "last_segment": rows[-1] if rows else None,
}

summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY

echo
echo "✅ 分段压测完成:"
echo "   - 原始结果: ${raw_json}"
echo "   - 累计明细: ${segments_jsonl}"
echo "   - 累计汇总: ${summary_json}"

exit "${bench_exit_code}"
