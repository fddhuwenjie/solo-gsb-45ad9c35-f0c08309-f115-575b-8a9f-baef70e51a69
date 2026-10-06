#!/usr/bin/env bash
# 固定支承点施工期检修演示：停用受阻 → 借助其他支承点绕行停用 → 停用期限制 →
#                          每次停启旧试载失效 → 启用后复验 → 历史快照不改 →
#                          停启纳入预案预演/确认（不落库、凭据失效、单次确认）。
#
# 用法：BASE=http://localhost:8080 ./demo_support_maintenance.sh
# 注意：请在空库上运行（如 docker compose 首次启动，或另设 APP_DB 的实例）。
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

api() {  # api METHOD PATH [JSON]：请求行打印到 stderr，stdout 只留响应便于解析
  local method="$1" path="$2" data="${3:-}"
  echo "==> $method $path ${data}" >&2
  if [ -n "$data" ]; then
    curl -sS -w '\nHTTP %{http_code}\n' -X "$method" "$BASE$path" \
      -H 'Content-Type: application/json' -d "$data"
  else
    curl -sS -w '\nHTTP %{http_code}\n' -X "$method" "$BASE$path"
  fi
  echo
}

expect() {  # expect FILE 期望状态码 描述
  local file="$1" want="$2"; shift 2
  local code
  code="$(grep -E '^HTTP [0-9]+$' "$file" | tail -n 1)"
  if [ "$code" != "HTTP $want" ]; then
    echo "断言失败（$*）：期望 $want，实际 '$code'，完整响应见 $file" >&2
    cat "$file" >&2
    exit 1
  fi
  echo "✔ $* → $want"
}

field() {  # field FILE PYTHON_EXPR：对响应首行 JSON 求值
  head -n 1 "$1" | python3 -c "import json,sys; d=json.load(sys.stdin); print($2)"
}

echo "---- 1. 登记：两支承点、两根梁；C1 同时连 S1/S2，C2 只连 S1 ----"
api POST /supports      '{"id":"MS1","name":"北柱础"}'
api POST /supports      '{"id":"MS2","name":"南台基"}'
api POST /components    '{"id":"MC1","name":"主梁","min_load":10}'
api POST /components    '{"id":"MC2","name":"次梁","min_load":8}'
api POST /connections   '{"id":"MJ1","a":"MS1","b":"MC1"}'
api POST /connections   '{"id":"MJ2","a":"MS2","b":"MC1"}'
api POST /connections   '{"id":"MJ3","a":"MS1","b":"MC2"}'
# 绕行用改接组合：C2 可改接到 MS2（事先登记、暂不在位）
api POST /connections   '{"id":"MJ4","a":"MC2","b":"MS2","in_place":false}'
api POST /steps '{"seq":1,"action":"test_load","component":"MC1","load":11}' \
  | tee /tmp/mt_t1.txt
expect /tmp/mt_t1.txt 201 "初始合格试载（v0）"

echo "---- 2. 停用受阻：MC2 只能经 MS1 获得支承，停用 MS1 整步拒绝、不留改动 ----"
api POST /steps '{"seq":2,"action":"deactivate_support","id":"MS1"}' \
  | tee /tmp/mt_block.txt
expect /tmp/mt_block.txt 409 "停用受阻：409 unreachable"
head -n 1 /tmp/mt_block.txt | grep -q '"first_affected_component": "MC2"' \
  && echo "✔ first_affected_component=MC2（沿用原有 409 语义指出首个受影响构件）"
api GET /state | tee /tmp/mt_state0.txt
[ "$(field /tmp/mt_state0.txt "d['last_seq']")" = "1" ] \
  && [ "$(field /tmp/mt_state0.txt "d['version']")" = "0" ] \
  && [ "$(field /tmp/mt_state0.txt "d['supports_in_maintenance']")" = "[]" ] \
  && echo "✔ 整步不留改动：last_seq=1、version=0、无检修支承点"

echo "---- 3. 借助其他支承点绕行：安装 MJ4（MC2—MS2）后停用 MS1 ----"
api POST /steps '{"seq":2,"action":"install","target":"connection","id":"MJ4"}' \
  | tee /tmp/mt_j4.txt
expect /tmp/mt_j4.txt 201 "安装绕行连接 MJ4（版本 → v1）"
api POST /steps '{"seq":3,"action":"deactivate_support","id":"MS1"}' \
  | tee /tmp/mt_off.txt
expect /tmp/mt_off.txt 201 "MS1 停用检修（版本 → v2）"
[ "$(field /tmp/mt_off.txt "d['result']['in_service']")" = "False" ] \
  && [ "$(field /tmp/mt_off.txt "d['result']['version']")" = "2" ] \
  && echo "✔ 回执 result.in_service=false、version=2（停启即换版）"
api GET /state | tee /tmp/mt_state1.txt
[ "$(field /tmp/mt_state1.txt "d['supports']")" = "['MS1', 'MS2']" ] \
  && [ "$(field /tmp/mt_state1.txt "d['supports_in_service']")" = "['MS2']" ] \
  && [ "$(field /tmp/mt_state1.txt "d['supports_in_maintenance'][0]['id']")" = "MS1" ] \
  && echo "✔ /state 保留原 supports 字段，并显示检修状态 supports_in_maintenance"

echo "---- 4. 停用期限制：该点不提供传力路径，也不得以它为端点新装连接/支撑 ----"
api POST /temp-supports '{"id":"MT9","a":"MS1","b":"MC1"}' >/dev/null
api POST /steps '{"seq":4,"action":"install","target":"temp_support","id":"MT9"}' \
  | tee /tmp/mt_ep.txt
expect /tmp/mt_ep.txt 409 "以停用点为端点安装临时支撑被拒（support_out_of_service）"
api POST /steps '{"seq":4,"action":"deactivate_support","id":"MS1"}' \
  | tee /tmp/mt_twice.txt
expect /tmp/mt_twice.txt 409 "重复停用被拒"

echo "---- 5. 每次停启旧试载失效：v0 试载不被卸撑/快照承认 ----"
api POST /snapshots '{}' | tee /tmp/mt_snap_stale.txt
expect /tmp/mt_snap_stale.txt 409 "当前版本 v2 无合格试载，快照拒绝 test_load_missing"
head -n 1 /tmp/mt_snap_stale.txt | grep -q '"first_affected_component": "MC1"' \
  && echo "✔ 首个受影响构件为 MC1"

echo "---- 6. 启用后复验：按当前连接重新检查路径，并再换一版 ----"
api POST /steps '{"seq":4,"action":"activate_support","id":"MS1"}' \
  | tee /tmp/mt_on.txt
expect /tmp/mt_on.txt 201 "MS1 重新启用（版本 → v3，按当前连接复验通过）"
api POST /steps '{"seq":5,"action":"activate_support","id":"MS1"}' \
  | tee /tmp/mt_on2.txt
expect /tmp/mt_on2.txt 409 "重复启用被拒"
# 停用→启用已经两次换版：仍须在 v3 下重新合格试载
api POST /steps '{"seq":5,"action":"test_load","component":"MC1","load":12}' \
  | tee /tmp/mt_tc1.txt
expect /tmp/mt_tc1.txt 201 "MC1 重新试载 v3"
api POST /steps '{"seq":6,"action":"test_load","component":"MC2","load":9}' \
  | tee /tmp/mt_tc2.txt
expect /tmp/mt_tc2.txt 201 "MC2 重新试载 v3"
api POST /snapshots '{}' | tee /tmp/mt_snap1.txt
expect /tmp/mt_snap1.txt 201 "当前版本合格试载齐备，固化验收快照 1"

echo "---- 7. 历史快照不改：再次停用并产生新版本后，快照 1 原样 ----"
api POST /steps '{"seq":7,"action":"deactivate_support","id":"MS1"}' \
  | tee /tmp/mt_off2.txt
expect /tmp/mt_off2.txt 201 "再次停用 MS1（绕行仍在，版本 → v4）"
api GET /snapshots/1 | tee /tmp/mt_snap1b.txt
[ "$(field /tmp/mt_snap1b.txt "d['version']")" = "3" ] \
  && [ "$(field /tmp/mt_snap1b.txt "d['last_seq']")" = "6" ] \
  && [ "$(field /tmp/mt_snap1b.txt "d['supports']")" = "['MS1', 'MS2']" ] \
  && echo "✔ 历史快照不改：仍为 v3、last_seq=6、两支承点均在用"
# 当前在 v4 停修状态下做合格试载后可固化新快照，新快照记录检修状态
api POST /steps '{"seq":8,"action":"test_load","component":"MC1","load":13}' >/dev/null
api POST /steps '{"seq":9,"action":"test_load","component":"MC2","load":10}' >/dev/null
api POST /snapshots '{}' | tee /tmp/mt_snap2.txt
expect /tmp/mt_snap2.txt 201 "固化快照 2（检修期间，只认 v4 合格试载）"
api GET /snapshots/2 | tee /tmp/mt_snap2b.txt
[ "$(field /tmp/mt_snap2b.txt "d['version']")" = "4" ] \
  && [ "$(field /tmp/mt_snap2b.txt "d['supports']")" = "['MS2']" ] \
  && [ "$(field /tmp/mt_snap2b.txt "d['supports_in_maintenance'][0]['id']")" = "MS1" ] \
  && echo "✔ 快照 2：在用支承点仅 MS2，supports_in_maintenance 记录 MS1"

echo "---- 8. 停启纳入预案：失败预演不落库 ----"
# 当前 MS1 停用、MS2 是唯一在用根；再停用 MS2 必使 MC1 悬空
api POST /plan/preview '{"steps":[{"action":"deactivate_support","id":"MS2"}]}' \
  | tee /tmp/mt_prev_bad.txt
expect /tmp/mt_prev_bad.txt 409 "预演逐步反映停启影响：末步 409 unreachable"
head -n 1 /tmp/mt_prev_bad.txt \
  | grep -q '"failed_step": {"index": 0, "seq": 10, "action": "deactivate_support"}' \
  && echo "✔ failed_step 指向停用 MS2 这一步"
api GET /state | tee /tmp/mt_state2.txt
[ "$(field /tmp/mt_state2.txt "d['last_seq']")" = "9" ] \
  && [ "$(field /tmp/mt_state2.txt "d['version']")" = "4" ] \
  && echo "✔ 预演不落库：last_seq=9、version=4"

echo "---- 9. 成功预演签发凭据；预演后停启状态变化使凭据失效 ----"
STEPS='[
  {"action":"activate_support","id":"MS1"},
  {"action":"test_load","component":"MC1","load":14},
  {"action":"test_load","component":"MC2","load":11}
]'
api POST /plan/preview "{\"steps\":$STEPS}" | tee /tmp/mt_prev_ok.txt
expect /tmp/mt_prev_ok.txt 200 "启用 + 复验试载清单预演成功"
TOKEN="$(field /tmp/mt_prev_ok.txt "d['token']")"
case "$TOKEN" in planv2-*) echo "✔ 取得 planv2- 凭据";; *) echo "凭据异常" >&2; exit 1;; esac
# 先直接执行启用（停启改变状态）→ 原凭据必须失效
api POST /steps '{"seq":10,"action":"activate_support","id":"MS1"}' \
  | tee /tmp/mt_direct_on.txt
expect /tmp/mt_direct_on.txt 201 "绕开预案直接启用 MS1（版本 → v5）"
api POST /plan/confirm "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}" \
  | tee /tmp/mt_stale.txt
expect /tmp/mt_stale.txt 409 "确认凭据随停启后的状态变化失效：plan_stale"

echo "---- 10. 重新预演并确认；凭据整单仅可使用一次 ----"
# 上一步已直接启用 MS1（v5）；改用新清单：再次停用（绕行仍在）+ 当前版本复验试载
STEPS='[
  {"action":"deactivate_support","id":"MS1"},
  {"action":"test_load","component":"MC1","load":14},
  {"action":"test_load","component":"MC2","load":11}
]'
api POST /plan/preview "{\"steps\":$STEPS}" > /tmp/mt_prev_ok2.txt
expect /tmp/mt_prev_ok2.txt 200 "重新预演（停用 + 复验试载清单）"
TOKEN="$(field /tmp/mt_prev_ok2.txt "d['token']")"
api POST /plan/confirm "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}" \
  | tee /tmp/mt_confirm.txt
expect /tmp/mt_confirm.txt 201 "确认成功，整单（停用 + 两次复验试载）保存"
[ "$(field /tmp/mt_confirm.txt "[r['seq'] for r in d['receipts']]")" = "[11, 12, 13]" ] \
  && echo "✔ 返回按序回执 seq 11..13"
api POST /plan/confirm "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}" \
  | tee /tmp/mt_replay.txt
expect /tmp/mt_replay.txt 409 "凭据重用被拒（成功确认仅可提交一次）"
api GET /snapshots/1 | tee /tmp/mt_snap1c.txt
[ "$(field /tmp/mt_snap1c.txt "d['version']")" = "3" ] \
  && echo "✔ 全程结束后历史快照 1 仍未改"

echo "✔ 支承点检修流程完成：停用受阻 → 绕行停用 → 停用期限制 → 旧试载失效 →"
echo "  启用后复验 → 历史快照不改 → 预案预演不落库/凭据失效/整单回滚/单次确认。"
