#!/usr/bin/env bash
# 施工预案演示：失败预演 → 伪造/旧式凭据拒绝 → 成功预演 → 过期拒绝 →
#               写事务内验签确认 → 凭据重用拒绝。
#
# 场景：登记东西两支承点、两根在位金柱与一根替换件；以一份从末条回执后
#       连续编号的清单（搭撑 → 拆柱 → 装替换件 → 试载 → 卸撑）先预演、
#       后确认。预演不留下改动；凭据由服务端密钥保护，不经过成功预演无法
#       得到有效凭据；预演后登记或工序变化使凭据过期；确认在一次写事务中
#       验签并复核整单，全部成功才保存按序回执，凭据整单仅可使用一次。
#
# 用法：BASE=http://localhost:8080 ./demo_plan.sh
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

echo "---- 1. 登记 ----"
api POST /supports      '{"id":"PS1","name":"西柱础"}'
api POST /supports      '{"id":"PS2","name":"东台基"}'
api POST /components    '{"id":"PC1","name":"西金柱","min_load":10}'
api POST /components    '{"id":"PC2","name":"东金柱","min_load":8}'
api POST /replacements  '{"id":"PR2","name":"替换东金柱","min_load":8}'
api POST /connections   '{"id":"PJ1","a":"PS1","b":"PC1"}'
api POST /connections   '{"id":"PJ2","a":"PC1","b":"PC2"}'
api POST /connections   '{"id":"PJ3","a":"PC1","b":"PR2","in_place":false}'
api POST /connections   '{"id":"PJ4","a":"PS2","b":"PR2","in_place":false}'
api POST /temp-supports '{"id":"PT1","a":"PS2","b":"PC1"}'

echo "---- 2. 失败预演：卸撑前未试载 → 409 指出出错工序，且不留下改动 ----"
api POST /plan/preview '{"steps":[
  {"action":"install","target":"temp_support","id":"PT1"},
  {"action":"remove_support","id":"PT1"}
]}' | tee /tmp/plan_bad.txt
expect /tmp/plan_bad.txt 409 "失败预演 test_load_missing，failed_step 指向清单第 2 步（seq 2）"
head -n 1 /tmp/plan_bad.txt | grep -q '"failed_step": {"index": 1, "seq": 2, "action": "remove_support"}' \
  && echo "✔ failed_step={index:1, seq:2, action:remove_support}"
api GET /steps | tee /tmp/plan_empty.txt
expect /tmp/plan_empty.txt 200 "预演后查询回执"
[ "$(field /tmp/plan_empty.txt 'len(d)')" = "0" ] && echo "✔ 预演不留下改动：仍无任何回执"

echo "---- 3. 成功预演：每步预计结果 + 服务端密钥签发的确认凭据 ----"
# 清单各项与 POST /steps 单步同字段；seq 可省略（服务端从末条回执后连续补齐），
# 这里显式给出 1..6。后一步套用规则时可见前一步的支撑、连接版本与试载变化。
STEPS='[
  {"seq":1,"action":"install","target":"temp_support","id":"PT1"},
  {"seq":2,"action":"disassemble","target":"component","id":"PC2"},
  {"seq":3,"action":"install","target":"replacement","id":"PR2","connections":["PJ3","PJ4"]},
  {"seq":4,"action":"test_load","component":"PC1","load":12},
  {"seq":5,"action":"test_load","component":"PR2","load":9.5},
  {"seq":6,"action":"remove_support","id":"PT1"}
]'
api POST /plan/preview "{\"steps\":$STEPS}" | tee /tmp/plan_prev.txt
expect /tmp/plan_prev.txt 200 "预演成功，返回每步预计结果与 token"
TOKEN="$(field /tmp/plan_prev.txt "d['token']")"
echo "✔ token=$TOKEN（planv2- 前缀的服务端 HMAC 凭据）"
case "$TOKEN" in planv2-*) echo "✔ 凭据带 planv2- 前缀（旧式无密钥 plan- 摘要不再签发）";; *) echo "凭据前缀异常：$TOKEN" >&2; exit 1;; esac
echo "✔ 试载预计落在版本 v$(field /tmp/plan_prev.txt "d['steps'][3]['result']['version']")（前两步装/拆已递进版本）"
api GET /state | tee /tmp/plan_state0.txt
[ "$(field /tmp/plan_state0.txt "d['last_seq']")" = "0" ] \
  && [ "$(field /tmp/plan_state0.txt "d['version']")" = "0" ] \
  && echo "✔ 预演不留下改动：last_seq=0、version=0"

echo "---- 3b. 绕过预演直接确认：伪造凭据 / 旧式无密钥摘要一律 409，不写入回执 ----"
FORGED="planv2-$(printf '%064d' 0)"
api POST /plan/confirm "{\"steps\":$STEPS,\"token\":\"$FORGED\"}" | tee /tmp/plan_forged.txt
expect /tmp/plan_forged.txt 409 "自行捏造的 planv2- 凭据被拒绝（无服务端密钥无法伪造）"
OLDSTYLE="plan-$(printf '%064d' 0)"
api POST /plan/confirm "{\"steps\":$STEPS,\"token\":\"$OLDSTYLE\"}" | tee /tmp/plan_oldstyle.txt
expect /tmp/plan_oldstyle.txt 409 "旧式无密钥 plan- 摘要被拒绝"
api GET /steps | tee /tmp/plan_empty3.txt
[ "$(field /tmp/plan_empty3.txt 'len(d)')" = "0" ] && echo "✔ 伪造/旧式凭据均未写入任何工序"

echo "---- 4. 过期拒绝：预演后发生登记变化，原凭据失效 ----"
api POST /temp-supports '{"id":"PT2","a":"PS1","b":"PC2"}'
api POST /plan/confirm "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}" | tee /tmp/plan_stale.txt
expect /tmp/plan_stale.txt 409 "plan_stale：预演后有登记或工序变化，凭据拒绝"
api GET /steps | tee /tmp/plan_empty2.txt
[ "$(field /tmp/plan_empty2.txt 'len(d)')" = "0" ] && echo "✔ 过期拒绝不写入任何工序"

echo "---- 5. 重新预演并确认：写事务内验签并复核整单，返回按序回执 ----"
api POST /plan/preview "{\"steps\":$STEPS}" | tee /tmp/plan_prev2.txt
expect /tmp/plan_prev2.txt 200 "重新预演（绑定最新登记状态）"
TOKEN="$(field /tmp/plan_prev2.txt "d['token']")"
api POST /plan/confirm "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}" | tee /tmp/plan_ok.txt
expect /tmp/plan_ok.txt 201 "确认成功，整单保存"
[ "$(field /tmp/plan_ok.txt "len(d['receipts'])")" = "6" ] \
  && [ "$(field /tmp/plan_ok.txt "[r['seq'] for r in d['receipts']]")" = "[1, 2, 3, 4, 5, 6]" ] \
  && echo "✔ 返回按序回执 seq 1..6"

echo "---- 5b. 凭据整单仅可用一次：重用已落库凭据 → 409，不重复写入 ----"
api POST /plan/confirm "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}" | tee /tmp/plan_replay.txt
expect /tmp/plan_replay.txt 409 "凭据重用被拒绝（状态已变且凭据已登记消耗）"
api GET /steps | tee /tmp/plan_steps_after.txt
[ "$(field /tmp/plan_steps_after.txt 'len(d)')" = "6" ] && echo "✔ 回执仍为 seq 1..6，未重复落库"

echo "---- 6. 确认后：卸撑完成可固化快照；原 /steps 单步调用仍可用 ----"
api POST /snapshots '{}' | tee /tmp/plan_snap.txt
expect /tmp/plan_snap.txt 201 "验收快照固化"
api POST /steps '{"seq":7,"action":"test_load","component":"PC1","load":11}' | tee /tmp/plan_step7.txt
expect /tmp/plan_step7.txt 201 "单步 /steps 继续可用（seq 7）"
api GET /snapshots/1 | tee /tmp/plan_snap1.txt
[ "$(field /tmp/plan_snap1.txt "d['last_seq']")" = "6" ] \
  && echo "✔ 快照 1 保持 last_seq=6，后续工序不改写旧快照"

echo "✔ 施工预案流程完成：失败预演 → 伪造/旧式凭据拒绝 → 成功预演 → 过期拒绝 → 写事务验签确认 → 重用拒绝。"
