#!/usr/bin/env bash
# 支承点检修演示：停用受阻 → 借助其他支承点绕行 → 停用期间端点限制 →
#                 启用后复验（停启使旧试载失效）→ 预案中的停启与凭据失效。
#
# 场景：北柱础 S1、南台基 S2 两个固定支承点，承重梁 C1 经榫卯 J1 落在 S1 上。
#       直接停用 S1 会使 C1 失去施工期支承（409 指出首个受影响构件）；
#       先搭设 S2—C1 临时支撑绕行后即可停用 S1 检修；停用期间不得以 S1 为
#       端点新装连接/支撑；每次停/启都使连接状态版本 +1、旧试载失效，启用后
#       须重新合格试载才能卸撑与固化快照。停启同样可列入预案清单走预演/确认，
#       预演后发生的停启会使已签发凭据失效。
#
# 用法：BASE=http://localhost:8080 ./demo_maintenance.sh
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

echo "---- 1. 登记：S1/S2 支承点、承重梁 C1（经 J1 落在 S1）、绕行与端点限制用料 ----"
api POST /supports      '{"id":"S1","name":"北柱础"}'
api POST /supports      '{"id":"S2","name":"南台基"}'
api POST /components    '{"id":"C1","name":"承重梁","min_load":10}'
api POST /connections   '{"id":"J1","a":"S1","b":"C1"}'
api POST /connections   '{"id":"J2","a":"S1","b":"C1","in_place":false}'   # 预登记改接组合
api POST /temp-supports '{"id":"T1","a":"S2","b":"C1"}'                    # 绕行支撑
api POST /temp-supports '{"id":"T2","a":"S1","b":"S2"}'                    # 端点限制演示用

echo "---- 2. C1 合格试载（v0），随后直接停用 S1 → 409 指出首个受影响构件 ----"
api POST /steps '{"seq":1,"action":"test_load","component":"C1","load":12}' | tee /tmp/mt_test0.txt
expect /tmp/mt_test0.txt 201 "试载合格（当前版本 v0）"
api POST /steps '{"seq":2,"action":"deactivate_support","id":"S1"}' | tee /tmp/mt_deact_block.txt
expect /tmp/mt_deact_block.txt 409 "停用受阻：C1 无法经其他在用支承点获得施工期支承"
head -n 1 /tmp/mt_deact_block.txt | grep -q '"error": "unreachable"' \
  && head -n 1 /tmp/mt_deact_block.txt | grep -q '"first_affected_component": "C1"' \
  && echo "✔ 原有 409 语义：unreachable，first_affected_component=C1"
api GET /state | tee /tmp/mt_state1.txt
[ "$(field /tmp/mt_state1.txt "d['last_seq']")" = "1" ] \
  && [ "$(field /tmp/mt_state1.txt "d['supports_detail'][0]['active']")" = "True" ] \
  && echo "✔ 整步不留改动：last_seq 仍为 1，S1 仍在用（supports_detail.active=true）"

echo "---- 3. 借助其他支承点绕行：先搭 S2—C1 临时支撑，再停用 S1 ----"
api POST /steps '{"seq":2,"action":"install","target":"temp_support","id":"T1"}' | tee /tmp/mt_shore.txt
expect /tmp/mt_shore.txt 201 "搭设绕行临时支撑 T1（seq 2）"
api POST /steps '{"seq":3,"action":"deactivate_support","id":"S1"}' | tee /tmp/mt_deact.txt
expect /tmp/mt_deact.txt 201 "绕行后停用 S1 成功（seq 3）"
[ "$(field /tmp/mt_deact.txt "d['result']['version']")" = "1" ] \
  && echo "✔ 停用使连接状态版本 +1（v1），此前 v0 试载失效"
api GET /state | tee /tmp/mt_state2.txt
[ "$(field /tmp/mt_state2.txt "d['supports_detail'][0]['active']")" = "False" ] \
  && [ "$(field /tmp/mt_state2.txt "d['components'][0]['reachable']")" = "True" ] \
  && echo "✔ /state 显示 S1 停用检修中；C1 经 T1→S2 仍可达"

echo "---- 4. 停用期间：不得以 S1 为端点新装连接或临时支撑 ----"
api POST /steps '{"seq":4,"action":"install","target":"connection","id":"J2"}' | tee /tmp/mt_j2.txt
expect /tmp/mt_j2.txt 409 "以停用支承点为端点装连接被拒（support_inactive）"
api POST /steps '{"seq":4,"action":"install","target":"temp_support","id":"T2"}' | tee /tmp/mt_t2.txt
expect /tmp/mt_t2.txt 409 "以停用支承点为端点装临时支撑被拒（support_inactive）"
api POST /steps '{"seq":4,"action":"deactivate_support","id":"S1"}' | tee /tmp/mt_deact2.txt
expect /tmp/mt_deact2.txt 409 "重复停用被拒（already_inactive）"
api GET /steps | tee /tmp/mt_steps4.txt
[ "$(field /tmp/mt_steps4.txt 'len(d)')" = "3" ] && echo "✔ 三次拒绝均未落库：回执仍为 seq 1..3"

echo "---- 5. 停用期间试载记录当时版本（v1）；启用后旧试载再失效，须复验 ----"
api POST /steps '{"seq":4,"action":"test_load","component":"C1","load":11}' | tee /tmp/mt_test1.txt
expect /tmp/mt_test1.txt 201 "停用期间试载合格（记录 v1）"
api POST /steps '{"seq":5,"action":"activate_support","id":"S1"}' | tee /tmp/mt_act.txt
expect /tmp/mt_act.txt 201 "检修完成，启用 S1（seq 5）"
[ "$(field /tmp/mt_act.txt "d['result']['version']")" = "2" ] \
  && echo "✔ 启用同样使版本 +1（v2）：停用期间的 v1 试载失效"
api POST /steps '{"seq":6,"action":"remove_support","id":"T1"}' | tee /tmp/mt_rm_block.txt
expect /tmp/mt_rm_block.txt 409 "启用后未复验：卸撑被拒（test_load_missing，当前版本 v2 无合格试载）"
api POST /steps '{"seq":6,"action":"test_load","component":"C1","load":12.5}' | tee /tmp/mt_test2.txt
expect /tmp/mt_test2.txt 201 "启用后复验：C1 在 v2 下重新合格试载"
api POST /steps '{"seq":7,"action":"remove_support","id":"T1"}' | tee /tmp/mt_rm.txt
expect /tmp/mt_rm.txt 201 "复验合格，卸撑成功（seq 7）"
api POST /snapshots '{}' | tee /tmp/mt_snap.txt
expect /tmp/mt_snap.txt 201 "固化验收快照（只认当前版本 v2 的合格试载）"

echo "---- 6. 预案中的停启：逐步反映影响、确认可用、凭据随停启失效 ----"
api POST /plan/preview '{"steps":[{"action":"deactivate_support","id":"S1"}]}' | tee /tmp/mt_pv_block.txt
expect /tmp/mt_pv_block.txt 409 "失败预演：T1 已卸除，停用 S1 使 C1 失支承（failed_step 指出出错工序）"
api POST /plan/preview '{"steps":[
  {"action":"deactivate_support","id":"S2"},
  {"action":"install","target":"temp_support","id":"T1"}
]}' | tee /tmp/mt_pv_chain.txt
expect /tmp/mt_pv_chain.txt 409 "逐步反映：前步停用 S2 后，以 S2 为端点装支撑被拒"
head -n 1 /tmp/mt_pv_chain.txt | grep -q '"error": "support_inactive"' \
  && head -n 1 /tmp/mt_pv_chain.txt | grep -q '"failed_step": {"index": 1, "seq": 9, "action": "install"}' \
  && echo "✔ failed_step={index:1, seq:9, action:install}，error=support_inactive"
api POST /plan/preview '{"steps":[
  {"action":"deactivate_support","id":"S2"},
  {"action":"activate_support","id":"S2"}
]}' | tee /tmp/mt_pv_ok.txt
expect /tmp/mt_pv_ok.txt 200 "成功预演：停用 S2 检修后即启用，签发凭据"
TOKEN="$(field /tmp/mt_pv_ok.txt "d['token']")"
api GET /state | tee /tmp/mt_state3.txt
[ "$(field /tmp/mt_state3.txt "d['last_seq']")" = "7" ] \
  && [ "$(field /tmp/mt_state3.txt "d['version']")" = "2" ] \
  && echo "✔ 预演不落库：last_seq=7、version=2 不变"
api POST /plan/confirm "{\"steps\":[
  {\"action\":\"deactivate_support\",\"id\":\"S2\"},
  {\"action\":\"activate_support\",\"id\":\"S2\"}
],\"token\":\"$TOKEN\"}" | tee /tmp/mt_cf_ok.txt
expect /tmp/mt_cf_ok.txt 201 "确认成功：停启工序按序落库（seq 8、9）"

echo "---- 7. 凭据随停启后的状态变化失效 ----"
api POST /plan/preview '{"steps":[{"action":"deactivate_support","id":"S2"}]}' | tee /tmp/mt_pv2.txt
expect /tmp/mt_pv2.txt 200 "再次预演取得新凭据"
TOKEN2="$(field /tmp/mt_pv2.txt "d['token']")"
api POST /steps '{"seq":10,"action":"deactivate_support","id":"S2"}' | tee /tmp/mt_deact3.txt
expect /tmp/mt_deact3.txt 201 "预演后经 /steps 停用 S2（seq 10）"
api POST /plan/confirm "{\"steps\":[{\"action\":\"deactivate_support\",\"id\":\"S2\"}],\"token\":\"$TOKEN2\"}" | tee /tmp/mt_cf_stale.txt
expect /tmp/mt_cf_stale.txt 409 "停启使状态变化，原凭据失效（plan_stale），不写入任何工序"
api POST /steps '{"seq":11,"action":"activate_support","id":"S2"}' | tee /tmp/mt_act3.txt
expect /tmp/mt_act3.txt 201 "重新启用 S2（seq 11）"
api GET /state | tee /tmp/mt_state4.txt
[ "$(field /tmp/mt_state4.txt "all(s['active'] for s in d['supports_detail'])")" = "True" ] \
  && echo "✔ 全部支承点恢复在用；/state 原有字段保留并附 supports_detail 检修状态"

echo "✔ 支承点检修流程完成：停用受阻 → 绕行 → 端点限制 → 启用后复验 → 预案停启与凭据失效。"
