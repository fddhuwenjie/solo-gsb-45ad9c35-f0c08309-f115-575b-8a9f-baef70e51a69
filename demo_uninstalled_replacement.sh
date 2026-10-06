#!/usr/bin/env bash
# 故障与修复演示：未安装替换件 R 不得被当作传力路径。
#
# 场景：登记固定支承点 S、在位承重梁 C、未安装替换件 R，
#       以及 S—R、R—C 两条默认在位榫卯（端点组合预登记，供日后安装）。
#       搭设 S—C 临时支撑并完成 C 的合格试载后，C 唯一"永久路径"仍穿过
#       未安装的 R —— 卸撑与快照都必须拒绝；R 按原工序安装接入后才放行。
#
# 用法：BASE=http://localhost:8080 ./demo_uninstalled_replacement.sh
# 注意：请在空库上运行（如 docker compose 首次启动，或另设 APP_DB 的实例），
#       脚本按工序号 1..6 连续提交。
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

api() {  # api METHOD PATH [JSON]  -> 原样打印响应
  local method="$1" path="$2" data="${3:-}"
  echo "==> $method $path ${data}"
  if [ -n "$data" ]; then
    curl -sS -w '\nHTTP %{http_code}\n' -X "$method" "$BASE$path" \
      -H 'Content-Type: application/json' -d "$data"
  else
    curl -sS -w '\nHTTP %{http_code}\n' -X "$method" "$BASE$path"
  fi
  echo
}

expect409() {  # expect409 FILE 描述；要求响应中状态码行为 HTTP 409
  local file="$1"; shift
  local code
  code="$(grep -E '^HTTP [0-9]+$' "$file" | tail -n 1)"
  if [ "$code" != "HTTP 409" ]; then
    echo "断言失败（$*）：期望 409，实际 '$code'，完整响应见 $file" >&2
    cat "$file" >&2
    exit 1
  fi
  echo "✔ $* → 409"
}

echo "---- 1. 登记：S、在位 C、未安装 R，以及默认在位的 S—R / R—C ----"
api POST /supports      '{"id":"S","name":"柱础"}'
api POST /components    '{"id":"C","name":"承重梁","min_load":10}'
api POST /replacements  '{"id":"R","name":"替换件","min_load":10}'
api POST /connections   '{"id":"JSR","a":"S","b":"R"}'
api POST /connections   '{"id":"JRC","a":"R","b":"C"}'

echo "---- 2. /state：R 未安装，C 可达性必须为 false（榫卯沿未安装件断裂）----"
api GET /state

echo "---- 3. 搭设 S—C 临时支撑（seq 1），C 完成合格试载（seq 2）----"
api POST /temp-supports '{"id":"T","a":"S","b":"C"}'
api POST /steps '{"seq":1,"action":"install","target":"temp_support","id":"T"}'
api POST /steps '{"seq":2,"action":"test_load","component":"C","load":12}'

echo "---- 4. 缺陷复现：卸撑被拒（首个失去永久支承构件为 C），状态不变 ----"
api POST /steps '{"seq":3,"action":"remove_support","id":"T"}' | tee /tmp/urs_remove.txt
expect409 /tmp/urs_remove.txt "卸撑拒绝 support_unreachable，first_affected_component=C"
echo "    响应前一行：$(head -n 1 /tmp/urs_remove.txt)"

api GET /state   # last_seq 仍为 2，临时支撑 T 仍 in_place=true

echo "---- 5. 支撑仍在位时，快照同样被拒（shoring_incomplete）----"
api POST /snapshots '{}' | tee /tmp/urs_snap1.txt
expect409 /tmp/urs_snap1.txt "卸撑未完成，快照拒绝"

echo "---- 6. 修复：按原工序安装 R（seq 3），默认在位榫卯随之生效 ----"
api POST /steps '{"seq":3,"action":"install","target":"replacement","id":"R"}'
api GET /state

echo "---- 7. 连接状态版本已更新，对 C、R 重新合格试载（seq 4、5）----"
api POST /steps '{"seq":4,"action":"test_load","component":"C","load":11}'
api POST /steps '{"seq":5,"action":"test_load","component":"R","load":10}'

echo "---- 8. 此时卸撑成功（seq 6），随后固化验收快照 1 ----"
api POST /steps '{"seq":6,"action":"remove_support","id":"T"}'
api POST /snapshots '{}'
api GET /snapshots/1
echo "✔ 修复流程完成：R 接入后合格试载支持卸撑与验收，快照已固化。"
