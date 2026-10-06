#!/usr/bin/env python3
"""木构件修复验收 API（仅依赖 Python 标准库：http.server + sqlite3）。

领域模型
--------
- 固定支承点 supports：基础/柱础等锚固点，默认在用（旧库迁移时一律视为在用）；
  施工期间可经工序暂时停用检修、完成后按工序启用。停用期间该点及搭在它身上的
  连接、临时支撑均不提供传力路径，也不得以它为端点新装连接或支撑。
- 承重构件 components：登记时带最低试载值 min_load；origin 为 original（初始在位）
  或 replacement（替换件，登记时不在位，安装工序后在位）。
- 榫卯连接 connections：事先登记的"允许端点组合"，in_place 表示是否在位；
  改接（安装连接）只允许使用已登记的端点组合。连接只有自身在位且两端构件
  均实际在位时才参与路径判断（在用支承点恒在位）；为未安装替换件预留的默认在位
  榫卯在替换件实际安装前沿路不生效。该判定在每次重建连接图时实时进行，
  数据库中已登记的同类记录同样遵循。
- 临时支撑 temp_supports：登记后可在位/卸除；不计入"永久连接状态版本"；
  同样要求两端节点实际在位才参与施工期路径判断。
- 工序回执 steps：按递增工序号提交 install / disassemble / test_load /
  remove_support / deactivate_support / activate_support。

校验规则
--------
- 工序号必须连续递增（last+1），跳号整步拒绝。
- 每个改变连接图的工序提交后重建连接图：施工中每个在位承重构件必须能经
  在位连接或临时支撑抵达某个在用固定支承点，否则整步回滚并指出首个受影响构件；
  试载同样校验被试构件具备该施工期路径。参与判断的连接必须自身在位且两端
  构件实际在位（见上）。
- 支承点停用/启用（deactivate_support / activate_support）是普通工序，纳入
  连续工序号，也可列入预案清单。停用后该点退出连接图：搭在它身上的连接、
  临时支撑一并失效，且不得以它为端点新装连接或临时支撑；若停用使某在位构件
  无法经其他在用支承点获得施工期支承，按 409 语义指出首个受影响构件，整步
  不留改动；启用后重新按当前连接检查路径。每次停/启均使连接状态版本 +1，
  此前的试载全部失效。
- 卸撑（remove_support）：卸除后每个在位承重构件必须仅凭永久连接抵达支承点，
  且每个在位承重构件都有在当前连接状态版本下通过、载荷不低于其最低值的试载。
- 连接状态版本 version：永久连接图每次变化（装/拆连接、拆构件、装构件、
  支承点停用/启用）递增；试载记录当时版本，版本过期即失效。临时支撑的
  装/卸不改变版本。
- 卸撑全部完成后可 POST /snapshots 固化验收快照；快照只增不改，
  后续修复工序不会改写旧快照。

施工预案
--------
- POST /plan/preview：按现有单步字段提交从末条回执后连续编号的工序清单
  （install / disassemble / test_load / remove_support / deactivate_support /
  activate_support），在同一事务内逐步
  套用现有传力路径与试载规则——后一步能看到前一步带来的支撑、连接版本与
  试载变化。仅当整单预演成功后，才签发由服务端密钥（持久保存在 SQLite
  meta 表，旧库启动时自动补建）保护的 HMAC 凭据，绑定原清单及预演当时的
  登记、工序与试载状态；失败指出出错工序及原有错误。无论成败，预演不留下
  任何改动，凭据也不可能由客户端自行伪造。
- POST /plan/confirm：在一次写事务内携带原清单与凭据。先以恒定时间比较
  校验 HMAC 凭据：伪造凭据、旧式无密钥摘要、清单改动或预演后登记/工序/
  试载状态变化一律按原有 409 plan_stale 语义拒绝；凭据整单仅可使用一次
  （plan_consumptions 登记，随确认事务一并提交/回滚）。校验通过后复核
  整单，全部成功才保存并返回按序回执；任一步失败指出出错工序并回滚整单
  新增的工序、连接版本、临时支撑状态及试载（连同凭据消耗记录），旧回执、
  试载与验收快照不变。密钥随库持久化，重启后状态未变的预案仍可确认。
"""

import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get(
    "APP_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db")
)
PORT = int(os.environ.get("APP_PORT", "8080"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supports (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,   -- 1 在用；0 停用检修中（旧库迁移补建，默认在用）
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS components (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    min_load REAL NOT NULL,
    origin TEXT NOT NULL DEFAULT 'original',   -- original | replacement
    in_place INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    in_place INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS temp_supports (
    id TEXT PRIMARY KEY,
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    in_place INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    seq INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,   -- JSON：工序参数
    result TEXT NOT NULL,    -- JSON：本步校验/执行结果
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_loads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL,
    load REAL NOT NULL,
    passed INTEGER NOT NULL,
    version INTEGER NOT NULL,  -- 试载时的永久连接状态版本
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL         -- JSON：固化后的验收快照，只增不改
);
CREATE TABLE IF NOT EXISTS plan_consumptions (
    token TEXT PRIMARY KEY,    -- 已用于确认的预案凭据（整单仅可落库一次）
    created_at TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- 基础设施

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # 旧库迁移：supports 无 active 列时补建，既有支承点默认在用
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(supports)")}
        if "active" not in cols:
            conn.execute(
                "ALTER TABLE supports ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('version', '0')")
        _ensure_server_secret(conn)   # 旧库首次启动同样补建服务端密钥
        conn.commit()
    finally:
        conn.close()


# 服务端密钥：只存在本进程内存与 SQLite meta 表，永不下发；用于 HMAC 签发/
# 校验预案凭据。旧库（无此行）启动时生成并持久化，重启后仍可校验此前凭据。
_SERVER_SECRET = None


def _ensure_server_secret(conn):
    """meta 中无密钥时生成 32 字节随机密钥并持久化，返回当前密钥。"""
    global _SERVER_SECRET
    row = conn.execute("SELECT v FROM meta WHERE k='server_secret'").fetchone()
    if row is None:
        secret = os.urandom(32).hex()
        conn.execute(
            "INSERT INTO meta(k, v) VALUES('server_secret', ?)", (secret,))
    else:
        secret = row["v"]
    _SERVER_SECRET = secret
    return secret


def _server_secret(conn):
    global _SERVER_SECRET
    if _SERVER_SECRET is None:
        _ensure_server_secret(conn)
    return _SERVER_SECRET


class ApiError(Exception):
    """HTTP 错误响应；整步拒绝时 status=409。"""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self):
        d = {"error": self.code, "message": self.message}
        d.update(self.extra)
        return d


def reject(code, message, component=None, **extra):
    """构造 409 工序拒绝；component 为首个受影响构件。"""
    if component is not None:
        extra["first_affected_component"] = component
    return ApiError(409, code, message, **extra)


def bad_request(message):
    return ApiError(400, "bad_request", message)


# ---------------------------------------------------------------- 图与版本

def _version(conn):
    return int(conn.execute("SELECT v FROM meta WHERE k='version'").fetchone()["v"])


def _bump(conn):
    v = _version(conn) + 1
    conn.execute("UPDATE meta SET v=? WHERE k='version'", (str(v),))
    return v


def _live_nodes(conn, comps, sups):
    """当前实际在位的节点集合：在用固定支承点恒在位，外加在位构件。

    sups 由 _graph 给出（仅含在用支承点）；停用检修中的支承点不在其列，
    搭在它身上的连接与临时支撑因端点不在位而沿路失效。
    """
    live = set(sups)
    for c in comps:
        if c["in_place"]:
            live.add(c["id"])
    return live


def _graph(conn):
    """重建当前连接图：在位构件、在用支承点、在位永久连接、在位临时支撑。

    连接/支撑只有自身在位且两端节点均实际在位（在用支承点恒在位）才作为
    有效边返回；未安装替换件身上的"默认在位"榫卯不参与任何路径判断；
    停用检修中的支承点不作为传力起点，其上的连接与临时支撑同样失效。
    """
    comps = conn.execute(
        "SELECT id, name, min_load, origin, in_place FROM components ORDER BY rowid"
    ).fetchall()
    sups = [r["id"] for r in conn.execute(
        "SELECT id FROM supports WHERE active=1 ORDER BY rowid")]
    live = _live_nodes(conn, comps, sups)

    def _live_edges(rows):
        return [(r["a"], r["b"]) for r in rows
                if r["a"] in live and r["b"] in live]

    perm = _live_edges(conn.execute("SELECT a, b FROM connections WHERE in_place=1"))
    temp = _live_edges(conn.execute("SELECT a, b FROM temp_supports WHERE in_place=1"))
    return comps, sups, perm, temp


def _reachable(supports, edges):
    """从所有固定支承点出发沿边扩展，返回可达节点集合。"""
    adj = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen = set(supports)
    stack = list(supports)
    while stack:
        n = stack.pop()
        for m in adj.get(n, ()):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return seen


def _first_unreachable(comps, seen):
    """按登记顺序返回首个无法抵达支承点的在位构件 id。"""
    for c in comps:
        if c["in_place"] and c["id"] not in seen:
            return c["id"]
    return None


def _first_untested(conn, comps, version):
    """按登记顺序返回首个在当前版本下无合格试载的在位构件 id。"""
    for c in comps:
        if not c["in_place"]:
            continue
        row = conn.execute(
            "SELECT 1 FROM test_loads WHERE component_id=? AND version=? AND passed=1 LIMIT 1",
            (c["id"], version),
        ).fetchone()
        if row is None:
            return c["id"]
    return None


def _check_construction_reachability(conn):
    """施工中校验：每个在位构件经在位连接或临时支撑抵达固定支承点。"""
    comps, sups, perm, temp = _graph(conn)
    seen = _reachable(sups, perm + temp)
    bad = _first_unreachable(comps, seen)
    if bad:
        raise reject(
            "unreachable",
            f"本工序后构件 {bad} 无法经在位连接或临时支撑抵达固定支承点，整步拒绝",
            component=bad,
        )


def _get(conn, table, oid, what):
    if not isinstance(oid, str) or not oid:
        raise bad_request(f"缺少合法字段 id（{what}）")
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
    if row is None:
        raise reject("not_registered", f"{what} {oid} 未事先登记")
    return row


def _require_endpoints(conn, *eps):
    """端点须已登记且在位；支承点须在用（停用检修中不得作为新装端点）。"""
    for ep in eps:
        row = conn.execute("SELECT active FROM supports WHERE id=?", (ep,)).fetchone()
        if row is not None:
            if not row["active"]:
                raise reject(
                    "support_inactive",
                    f"支承点 {ep} 停用检修中，不得作为新装连接或临时支撑的端点，整步拒绝",
                    support=ep,
                )
            continue
        row = conn.execute("SELECT in_place FROM components WHERE id=?", (ep,)).fetchone()
        if row is None:
            raise reject("not_registered", f"端点 {ep} 未登记", component=ep)
        if not row["in_place"]:
            raise reject("endpoint_not_in_place", f"端点 {ep} 不在位，整步拒绝", component=ep)


# ---------------------------------------------------------------- 登记接口

def _insert_unique(conn, sql, params, dup_msg):
    try:
        conn.execute(sql, params)
    except sqlite3.IntegrityError:
        raise ApiError(409, "duplicate", dup_msg)


def _check_endpoint_registered(conn, ep):
    if conn.execute("SELECT 1 FROM supports WHERE id=?", (ep,)).fetchone():
        return
    if conn.execute("SELECT 1 FROM components WHERE id=?", (ep,)).fetchone():
        return
    raise ApiError(409, "not_registered", f"端点 {ep} 未登记（须为支承点或构件）")


def create_support(body):
    sid, name = body.get("id"), body.get("name")
    if not isinstance(sid, str) or not sid:
        raise bad_request("缺少合法字段 id")
    if not isinstance(name, str) or not name:
        raise bad_request("缺少合法字段 name")
    conn = connect()
    try:
        with conn:
            _insert_unique(
                conn,
                "INSERT INTO supports(id, name, created_at) VALUES(?,?,?)",
                (sid, name, now()),
                f"支承点 {sid} 已登记",
            )
        return 201, {"id": sid, "name": name, "active": True}
    finally:
        conn.close()


def _create_component(body, origin, in_place):
    cid, name, min_load = body.get("id"), body.get("name"), body.get("min_load")
    if not isinstance(cid, str) or not cid:
        raise bad_request("缺少合法字段 id")
    if not isinstance(name, str) or not name:
        raise bad_request("缺少合法字段 name")
    if isinstance(min_load, bool) or not isinstance(min_load, (int, float)) or min_load < 0:
        raise bad_request("min_load 须为非负数值")
    conn = connect()
    try:
        with conn:
            _insert_unique(
                conn,
                "INSERT INTO components(id, name, min_load, origin, in_place, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (cid, name, float(min_load), origin, in_place, now()),
                f"构件 {cid} 已登记",
            )
        return 201, {
            "id": cid, "name": name, "min_load": min_load,
            "origin": origin, "in_place": bool(in_place),
        }
    finally:
        conn.close()


def create_component(body):
    return _create_component(body, "original", 1)


def create_replacement(body):
    return _create_component(body, "replacement", 0)


def _create_link(body, table, what, default_in_place):
    lid, a, b = body.get("id"), body.get("a"), body.get("b")
    in_place = body.get("in_place", default_in_place)
    if not isinstance(lid, str) or not lid:
        raise bad_request("缺少合法字段 id")
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        raise bad_request("缺少合法端点字段 a、b")
    if a == b:
        raise bad_request("端点 a、b 不能相同")
    if not isinstance(in_place, bool):
        raise bad_request("in_place 须为布尔值")
    conn = connect()
    try:
        with conn:
            _check_endpoint_registered(conn, a)
            _check_endpoint_registered(conn, b)
            _insert_unique(
                conn,
                f"INSERT INTO {table}(id, a, b, in_place, created_at) VALUES(?,?,?,?,?)",
                (lid, a, b, 1 if in_place else 0, now()),
                f"{what} {lid} 已登记",
            )
        return 201, {"id": lid, "a": a, "b": b, "in_place": in_place}
    finally:
        conn.close()


def create_connection(body):
    # 榫卯连接：默认视为现状在位；为改接预留的组合可登记 in_place=false
    return _create_link(body, "connections", "连接", True)


def create_temp_support(body):
    # 临时支撑：默认登记时未搭设
    return _create_link(body, "temp_supports", "临时支撑", False)


# ---------------------------------------------------------------- 工序回执

def _apply_install(conn, body):
    target, oid = body.get("target"), body.get("id")
    if target == "connection":
        row = _get(conn, "connections", oid, "连接")
        if row["in_place"]:
            raise reject("already_in_place", f"连接 {oid} 已在位")
        _require_endpoints(conn, row["a"], row["b"])
        conn.execute("UPDATE connections SET in_place=1 WHERE id=?", (oid,))
        _bump(conn)
        return {"connection": oid, "endpoints": [row["a"], row["b"]]}
    if target == "temp_support":
        row = _get(conn, "temp_supports", oid, "临时支撑")
        if row["in_place"]:
            raise reject("already_in_place", f"临时支撑 {oid} 已在位")
        _require_endpoints(conn, row["a"], row["b"])
        conn.execute("UPDATE temp_supports SET in_place=1 WHERE id=?", (oid,))
        return {"temp_support": oid, "endpoints": [row["a"], row["b"]]}
    if target in ("component", "replacement"):
        row = _get(conn, "components", oid, "构件")
        if target == "replacement" and row["origin"] != "replacement":
            raise bad_request(f"{oid} 不是已登记的替换件")
        if row["in_place"]:
            raise reject("already_in_place", f"构件 {oid} 已在位", component=oid)
        # 允许同一步内附带安装该构件的已登记连接（端点组合仍须事先登记），
        # 否则新装构件必然悬空、无法通过施工可达性校验。
        jids = body.get("connections") or []
        if not isinstance(jids, list) or not all(isinstance(j, str) for j in jids):
            raise bad_request("connections 须为连接 id 数组")
        conn.execute("UPDATE components SET in_place=1 WHERE id=?", (oid,))
        installed = []
        for jid in jids:
            j = _get(conn, "connections", jid, "连接")
            if j["in_place"]:
                raise reject("already_in_place", f"连接 {jid} 已在位")
            _require_endpoints(conn, j["a"], j["b"])
            conn.execute("UPDATE connections SET in_place=1 WHERE id=?", (jid,))
            installed.append(jid)
        _bump(conn)
        return {"component": oid, "connections": installed}
    raise bad_request("install 的 target 须为 connection/temp_support/component/replacement")


def _apply_disassemble(conn, body):
    target, oid = body.get("target"), body.get("id")
    if target == "connection":
        row = _get(conn, "connections", oid, "连接")
        if not row["in_place"]:
            raise reject("not_in_place", f"连接 {oid} 不在位，无法拆卸")
        conn.execute("UPDATE connections SET in_place=0 WHERE id=?", (oid,))
        _bump(conn)
        return {"connection": oid}
    if target == "component":
        row = _get(conn, "components", oid, "构件")
        if not row["in_place"]:
            raise reject("not_in_place", f"构件 {oid} 不在位，无法拆卸", component=oid)
        conn.execute("UPDATE components SET in_place=0 WHERE id=?", (oid,))
        # 联动卸下依附于该构件的在位连接与临时支撑
        jc = [r["id"] for r in conn.execute(
            "SELECT id FROM connections WHERE in_place=1 AND (a=? OR b=?)", (oid, oid))]
        tc = [r["id"] for r in conn.execute(
            "SELECT id FROM temp_supports WHERE in_place=1 AND (a=? OR b=?)", (oid, oid))]
        conn.execute(
            "UPDATE connections SET in_place=0 WHERE in_place=1 AND (a=? OR b=?)", (oid, oid))
        conn.execute(
            "UPDATE temp_supports SET in_place=0 WHERE in_place=1 AND (a=? OR b=?)", (oid, oid))
        _bump(conn)
        return {"component": oid, "removed_connections": jc, "removed_temp_supports": tc}
    raise bad_request("disassemble 的 target 须为 connection/component")


def _apply_test_load(conn, seq, body):
    cid, load = body.get("component"), body.get("load")
    row = _get(conn, "components", cid, "构件")
    if not row["in_place"]:
        raise reject("endpoint_not_in_place", f"构件 {cid} 不在位，无法试载", component=cid)
    if isinstance(load, bool) or not isinstance(load, (int, float)) or load < 0:
        raise bad_request("load 须为非负数值")
    v = _version(conn)
    passed = load >= row["min_load"]
    conn.execute(
        "INSERT INTO test_loads(component_id, load, passed, version, seq, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (cid, float(load), 1 if passed else 0, v, seq, now()),
    )
    return {
        "component": cid, "load": load, "min_load": row["min_load"],
        "passed": passed, "version": v,
    }


def _apply_remove_support(conn, body):
    oid = body.get("id")
    row = _get(conn, "temp_supports", oid, "临时支撑")
    if not row["in_place"]:
        raise reject("not_in_place", f"临时支撑 {oid} 不在位，无法卸撑")
    conn.execute("UPDATE temp_supports SET in_place=0 WHERE id=?", (oid,))
    # 卸撑条件一：仅凭永久连接抵达固定支承点
    comps, sups, perm, _temp = _graph(conn)
    seen = _reachable(sups, perm)
    bad = _first_unreachable(comps, seen)
    if bad:
        raise reject(
            "support_unreachable",
            f"卸撑后构件 {bad} 无法仅凭永久连接抵达固定支承点，整步拒绝",
            component=bad,
        )
    # 卸撑条件二：每个在位构件都有当前连接状态下通过的合格试载
    v = _version(conn)
    bad = _first_untested(conn, comps, v)
    if bad:
        raise reject(
            "test_load_missing",
            f"构件 {bad} 缺少在当前连接状态(v{v})下通过且载荷不低于最低值的试载，整步拒绝",
            component=bad,
        )
    return {
        "temp_support": oid,
        "permanent_only": True,
        "version": v,
        "components_checked": [c["id"] for c in comps if c["in_place"]],
    }


def _apply_deactivate_support(conn, body):
    """停用支承点检修：该点退出连接图，其上的连接/临时支撑不再提供传力路径。

    停用使连接状态版本 +1（旧试载失效）；若此后某在位构件无法经其他在用
    支承点获得施工期支承，由 _apply_action 的施工中校验按 409 语义拒绝，
    整步回滚、不留改动。
    """
    oid = body.get("id")
    row = _get(conn, "supports", oid, "支承点")
    if not row["active"]:
        raise reject("already_inactive", f"支承点 {oid} 已处于停用检修状态", support=oid)
    conn.execute("UPDATE supports SET active=0 WHERE id=?", (oid,))
    v = _bump(conn)   # 停用使旧试载失效
    return {"support": oid, "active": False, "version": v}


def _apply_activate_support(conn, body):
    """启用支承点：重新按当前连接检查路径（由 _apply_action 的施工校验执行）。

    启用同样使连接状态版本 +1（旧试载失效），恢复后该点及其上仍在位的
    连接、临时支撑重新参与传力路径判断。
    """
    oid = body.get("id")
    row = _get(conn, "supports", oid, "支承点")
    if row["active"]:
        raise reject("not_inactive", f"支承点 {oid} 本就在用，无需启用", support=oid)
    conn.execute("UPDATE supports SET active=1 WHERE id=?", (oid,))
    v = _bump(conn)   # 启用同样使旧试载失效
    return {"support": oid, "active": True, "version": v}


# 工序动作全集：/steps 单步与预案清单共用同一套校验
_ACTIONS = ("install", "disassemble", "test_load", "remove_support",
            "deactivate_support", "activate_support")


def _apply_action(conn, seq, action, body):
    """按动作套用单步工序并返回执行结果。

    装/拆改变连接图，试载也必须作用于当前经在位连接或临时支承可达的构件，
    支承点停/启同样改变传力路径，因此这些工序套用后重建连接图做施工中校验
    （卸撑在自身函数内做更严校验）。
    """
    if action == "install":
        result = _apply_install(conn, body)
    elif action == "disassemble":
        result = _apply_disassemble(conn, body)
    elif action == "test_load":
        result = _apply_test_load(conn, seq, body)
    elif action == "deactivate_support":
        result = _apply_deactivate_support(conn, body)
    elif action == "activate_support":
        result = _apply_activate_support(conn, body)
    else:
        result = _apply_remove_support(conn, body)
    if action in ("install", "disassemble", "test_load",
                  "deactivate_support", "activate_support"):
        _check_construction_reachability(conn)
    return result


def _record_receipt(conn, seq, action, body, result):
    """写入工序回执并返回回执对象。"""
    receipt = {
        "seq": seq,
        "action": action,
        "payload": {k: v for k, v in body.items() if k not in ("seq", "action")},
        "result": result,
        "created_at": now(),
    }
    conn.execute(
        "INSERT INTO steps(seq, action, payload, result, created_at) VALUES(?,?,?,?,?)",
        (seq, action,
         json.dumps(receipt["payload"], ensure_ascii=False),
         json.dumps(result, ensure_ascii=False),
         receipt["created_at"]),
    )
    return receipt


def create_step(body):
    seq, action = body.get("seq"), body.get("action")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise bad_request("seq 须为正整数")
    if action not in _ACTIONS:
        raise bad_request("action 须为 " + "/".join(_ACTIONS))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM steps").fetchone()["s"]
        if seq != last + 1:
            raise reject(
                "sequence_gap",
                f"工序跳号：期望工序号 {last + 1}，实收 {seq}，整步拒绝",
                expected=last + 1, got=seq,
            )
        result = _apply_action(conn, seq, action, body)
        receipt = _record_receipt(conn, seq, action, body, result)
        conn.commit()
        return 201, receipt
    except Exception:
        conn.rollback()   # 整步拒绝：已有回执与状态不变
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 施工预案（预演 / 确认）

def _normalize_plan_items(conn, body, enforce_continuity):
    """校验清单形状并补齐工序号：清单从末条回执后连续编号。

    各项字段与 POST /steps 单步相同；seq 可省略（由服务端连续补齐），显式给出时
    预演阶段必须吻合编号，否则整单拒绝。确认阶段编号一致性由凭据保证（凭据绑定
    了补齐后的清单与当前状态），不再重复校验。
    """
    steps = body.get("steps")
    if not isinstance(steps, list) or not steps:
        raise bad_request("steps 须为非空工序数组")
    last = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM steps").fetchone()["s"]
    items = []
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise bad_request(f"清单第 {i + 1} 项须为 JSON 对象")
        item = dict(raw)
        action = item.get("action")
        if action not in _ACTIONS:
            raise bad_request(
                f"清单第 {i + 1} 项 action 须为 {'/'.join(_ACTIONS)}")
        seq = item.get("seq")
        expected = last + 1 + i
        if seq is None:
            item["seq"] = expected
        else:
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                raise bad_request(f"清单第 {i + 1} 项 seq 须为正整数")
            if enforce_continuity and seq != expected:
                raise reject(
                    "sequence_gap",
                    f"清单第 {i + 1} 项工序跳号：期望工序号 {expected}，实收 {seq}，整单拒绝",
                    expected=expected, got=seq,
                    failed_step={"index": i, "seq": seq, "action": action},
                )
        items.append(item)
    return items


def _table_fingerprint(conn, sql):
    h = hashlib.sha256()
    for r in conn.execute(sql):
        h.update(json.dumps(dict(r), ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _plan_token(conn, items):
    """确认凭据：以服务端持久密钥对"清单 + 当前登记/工序状态"做 HMAC-SHA256。

    绑定清单、当前登记（支承点/构件/连接/临时支撑）与工序状态（连接状态
    版本、工序回执、试载记录）。密钥永不下发，客户端无法自行伪造；清单或
    任一状态变化都会使重算结果不符。仅 /plan/preview 整单预演成功后签发。
    """
    list_fp = hashlib.sha256(json.dumps(
        items, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    state_fp = hashlib.sha256("|".join([
        _table_fingerprint(conn, "SELECT * FROM supports ORDER BY rowid"),
        _table_fingerprint(conn, "SELECT * FROM components ORDER BY rowid"),
        _table_fingerprint(conn, "SELECT * FROM connections ORDER BY rowid"),
        _table_fingerprint(conn, "SELECT * FROM temp_supports ORDER BY rowid"),
        _table_fingerprint(conn, "SELECT * FROM steps ORDER BY seq"),
        _table_fingerprint(conn, "SELECT * FROM test_loads ORDER BY id"),
        str(_version(conn)),
    ]).encode("utf-8")).hexdigest()
    mac = hmac.new(
        _server_secret(conn).encode("ascii"),
        f"{list_fp}:{state_fp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return "planv2-" + mac


def _execute_plan(conn, items):
    """在当前事务内依次套用整单工序并写入回执：后一步可见前一步带来的支撑、
    连接版本与试载变化。任一步失败抛出原错误并附 failed_step，由调用方回滚整单
    （工序、连接版本、临时支撑状态及试载）。"""
    receipts = []
    for i, item in enumerate(items):
        seq, action = item["seq"], item["action"]
        try:
            result = _apply_action(conn, seq, action, item)
        except ApiError as e:
            e.extra.setdefault(
                "failed_step", {"index": i, "seq": seq, "action": action})
            raise
        receipts.append(_record_receipt(conn, seq, action, item, result))
    return receipts


def preview_plan(body):
    """预演：在同一事务内逐步套用清单。仅当整单全部成功才返回每步预计结果
    与服务端密钥签发的确认凭据（凭据绑定执行前的清单与登记、工序状态）；
    无论成败都回滚，预演不留下任何改动。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        items = _normalize_plan_items(conn, body, enforce_continuity=True)
        token = _plan_token(conn, items)  # 凭据绑定执行前的清单与当前状态
        receipts = _execute_plan(conn, items)  # 整单成功才走到返回；失败抛出且不签发
        return 200, {
            "ok": True,
            "token": token,
            "base_seq": items[0]["seq"] - 1,
            "steps": [{k: r[k] for k in ("seq", "action", "payload", "result")}
                      for r in receipts],
            "resulting": {"last_seq": receipts[-1]["seq"], "version": _version(conn)},
        }
    finally:
        conn.rollback()   # 预演不留下改动
        conn.close()


def _plan_stale():
    return ApiError(
        409, "plan_stale",
        "凭据无效或与清单/当前登记、工序状态不符：未经过成功预演、凭据系伪造"
        "或旧式无密钥摘要、预演后已有登记/工序/试载变化、清单被改动，或凭据"
        "已使用过，请重新预演")


def confirm_plan(body):
    """确认：在一次写事务内先校验服务端 HMAC 凭据与凭据一次性，再复核整单；
    全部成功才保存并返回按序回执。伪造/旧式摘要、清单或状态变化、凭据重用
    一律 409 plan_stale；任一步失败指出出错工序并回滚整单（含凭据消耗记录）。"""
    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise bad_request("缺少合法字段 token（预演返回的确认凭据）")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        items = _normalize_plan_items(conn, body, enforce_continuity=False)
        expected = _plan_token(conn, items)
        # 旧式 plan- 无密钥摘要或任何伪造值都不可能通过恒定时间 HMAC 比较
        _HEX64 = frozenset("0123456789abcdef")
        well_formed = (token.startswith("planv2-")
                       and len(token) == 7 + 64
                       and all(c in _HEX64 for c in token[7:]))
        if not well_formed or not hmac.compare_digest(expected, token):
            raise _plan_stale()
        # 整单仅可落库一次：消费登记随确认事务提交；确认失败回滚后凭据仍可重试
        try:
            conn.execute(
                "INSERT INTO plan_consumptions(token, created_at) VALUES(?,?)",
                (token, now()))
        except sqlite3.IntegrityError:
            raise _plan_stale()
        receipts = _execute_plan(conn, items)
        conn.commit()
        return 201, {"ok": True, "token": token, "receipts": receipts}
    except Exception:
        conn.rollback()   # 任一步失败：整单新增的工序、连接版本、临时支撑状态、试载及凭据消费全部回滚
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 快照

def create_snapshot(_body):
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        left = [r["id"] for r in conn.execute(
            "SELECT id FROM temp_supports WHERE in_place=1 ORDER BY rowid")]
        if left:
            raise reject(
                "shoring_incomplete",
                f"卸撑未完成：临时支撑 {left[0]} 仍在位，不能固化验收快照",
                temp_support=left[0],
            )
        comps, sups, perm, _temp = _graph(conn)
        seen = _reachable(sups, perm)
        bad = _first_unreachable(comps, seen)
        if bad:
            raise reject(
                "support_unreachable",
                f"构件 {bad} 无法仅凭永久连接抵达固定支承点，不能固化快照",
                component=bad,
            )
        v = _version(conn)
        bad = _first_untested(conn, comps, v)
        if bad:
            raise reject(
                "test_load_missing",
                f"构件 {bad} 缺少在当前连接状态(v{v})下通过的试载，不能固化快照",
                component=bad,
            )
        last = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM steps").fetchone()["s"]
        comp_list = []
        for c in comps:
            if not c["in_place"]:
                continue
            t = conn.execute(
                "SELECT load, seq, version FROM test_loads"
                " WHERE component_id=? AND version=? AND passed=1"
                " ORDER BY id DESC LIMIT 1",
                (c["id"], v),
            ).fetchone()
            comp_list.append({
                "id": c["id"], "name": c["name"], "min_load": c["min_load"],
                "origin": c["origin"],
                "accepted_test": dict(t) if t else None,
            })
        live = _live_nodes(conn, comps, sups)
        joints = [dict(r) for r in conn.execute(
            "SELECT id, a, b FROM connections WHERE in_place=1 ORDER BY rowid")
            if r["a"] in live and r["b"] in live]
        receipts = [{
            "seq": r["seq"], "action": r["action"],
            "payload": json.loads(r["payload"]), "result": json.loads(r["result"]),
            "created_at": r["created_at"],
        } for r in conn.execute("SELECT * FROM steps ORDER BY seq")]
        data = {
            "version": v,
            "last_seq": last,
            "supports": sups,
            "components": comp_list,
            "connections_in_place": joints,
            "steps": receipts,
        }
        created = now()
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, data) VALUES(?,?)",
            (created, json.dumps(data, ensure_ascii=False)),
        )
        conn.commit()
        return 201, {"id": cur.lastrowid, "created_at": created,
                     "version": v, "last_seq": last,
                     "components": [c["id"] for c in comp_list]}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 查询接口

def list_table(table):
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for r in rows:
            if "in_place" in r:
                r["in_place"] = bool(r["in_place"])
            if "active" in r:
                r["active"] = bool(r["active"])
        return 200, rows
    finally:
        conn.close()


def list_steps():
    conn = connect()
    try:
        out = []
        for r in conn.execute("SELECT * FROM steps ORDER BY seq"):
            out.append({
                "seq": r["seq"], "action": r["action"],
                "payload": json.loads(r["payload"]), "result": json.loads(r["result"]),
                "created_at": r["created_at"],
            })
        return 200, out
    finally:
        conn.close()


def get_step(seq):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM steps WHERE seq=?", (seq,)).fetchone()
        if r is None:
            raise ApiError(404, "not_found", f"工序 {seq} 不存在")
        return 200, {
            "seq": r["seq"], "action": r["action"],
            "payload": json.loads(r["payload"]), "result": json.loads(r["result"]),
            "created_at": r["created_at"],
        }
    finally:
        conn.close()


def get_state():
    conn = connect()
    try:
        comps, sups, perm, temp = _graph(conn)
        v = _version(conn)
        seen_all = _reachable(sups, perm + temp)
        seen_perm = _reachable(sups, perm)
        comp_list = []
        for c in comps:
            t = conn.execute(
                "SELECT load, passed, version, seq FROM test_loads"
                " WHERE component_id=? ORDER BY id DESC LIMIT 1",
                (c["id"],),
            ).fetchone()
            comp_list.append({
                "id": c["id"], "name": c["name"], "min_load": c["min_load"],
                "origin": c["origin"], "in_place": bool(c["in_place"]),
                "reachable": c["id"] in seen_all if c["in_place"] else None,
                "permanent_reachable": c["id"] in seen_perm if c["in_place"] else None,
                "last_test": dict(t) if t else None,
            })
        joints = [dict(r, in_place=bool(r["in_place"])) for r in conn.execute(
            "SELECT id, a, b, in_place FROM connections ORDER BY rowid")]
        shores = [dict(r, in_place=bool(r["in_place"])) for r in conn.execute(
            "SELECT id, a, b, in_place FROM temp_supports ORDER BY rowid")]
        # 各支承点检修状态：active=false 为停用检修中（不参与传力路径）
        sups_detail = [dict(r, active=bool(r["active"])) for r in conn.execute(
            "SELECT id, name, active FROM supports ORDER BY rowid")]
        last = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM steps").fetchone()["s"]
        return 200, {
            "version": v,
            "last_seq": last,
            "supports": [s["id"] for s in sups_detail],
            "supports_detail": sups_detail,
            "components": comp_list,
            "connections": joints,
            "temp_supports": shores,
        }
    finally:
        conn.close()


def list_snapshots():
    conn = connect()
    try:
        return 200, [{"id": r["id"], "created_at": r["created_at"]}
                     for r in conn.execute("SELECT id, created_at FROM snapshots ORDER BY id")]
    finally:
        conn.close()


def get_snapshot(sid):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
        if r is None:
            raise ApiError(404, "not_found", f"快照 {sid} 不存在")
        data = json.loads(r["data"])
        data["id"] = r["id"]
        data["created_at"] = r["created_at"]
        return 200, data
    finally:
        conn.close()


# ---------------------------------------------------------------- 路由

def route(method, path, body):
    if path == "/health":
        return 200, {"ok": True}
    if path == "/" and method == "GET":
        return 200, {
            "service": "木构件修复验收 API",
            "endpoints": [
                "POST /supports            登记固定支承点",
                "POST /components          登记承重构件（含 min_load 最低试载值）",
                "POST /connections         登记榫卯连接（允许的端点组合）",
                "POST /temp-supports       登记临时支撑",
                "POST /replacements        登记替换件",
                "POST /steps               按递增工序号提交拆卸/安装/试载/卸撑/支承点停启回执",
                "POST /plan/preview         预演工序清单：整单成功后签发服务端密钥保护的凭据（不留改动）",
                "POST /plan/confirm         凭据确认：写事务内验签并复核整单，凭据整单仅可用一次",
                "GET  /steps | /steps/{n}  查询各工序回执与结果",
                "GET  /state               当前连接图与各构件状态",
                "POST /snapshots           卸撑完成后固化验收快照（只增不改）",
                "GET  /snapshots | /snapshots/{id}",
            ],
        }
    if method == "POST" and path == "/supports":
        return create_support(body)
    if method == "POST" and path == "/components":
        return create_component(body)
    if method == "POST" and path == "/connections":
        return create_connection(body)
    if method == "POST" and path == "/temp-supports":
        return create_temp_support(body)
    if method == "POST" and path == "/replacements":
        return create_replacement(body)
    if method == "POST" and path == "/steps":
        return create_step(body)
    if method == "POST" and path == "/plan/preview":
        return preview_plan(body)
    if method == "POST" and path == "/plan/confirm":
        return confirm_plan(body)
    if method == "POST" and path == "/snapshots":
        return create_snapshot(body)
    if method == "GET" and path in ("/supports", "/components", "/connections",
                                    "/temp-supports"):
        return list_table(path.strip("/").replace("-", "_"))
    if method == "GET" and path == "/replacements":
        conn = connect()
        try:
            rows = [dict(r, in_place=bool(r["in_place"])) for r in conn.execute(
                "SELECT * FROM components WHERE origin='replacement' ORDER BY rowid")]
            return 200, rows
        finally:
            conn.close()
    if method == "GET" and path == "/steps":
        return list_steps()
    m = re.fullmatch(r"/steps/(\d+)", path)
    if method == "GET" and m:
        return get_step(int(m.group(1)))
    if method == "GET" and path == "/state":
        return get_state()
    if method == "GET" and path == "/snapshots":
        return list_snapshots()
    m = re.fullmatch(r"/snapshots/(\d+)", path)
    if method == "GET" and m:
        return get_snapshot(int(m.group(1)))
    raise ApiError(404, "not_found", f"路由不存在：{method} {path}")


class Handler(BaseHTTPRequestHandler):
    server_version = "TimberRepair/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise bad_request("请求体不是合法 JSON")
        if not isinstance(obj, dict):
            raise bad_request("请求体须为 JSON 对象")
        return obj

    def _handle(self, method):
        try:
            body = self._read_body() if method == "POST" else {}
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            status, obj = route(method, path, body)
            self._send(status, obj)
        except ApiError as e:
            self._send(e.status, e.body())
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": "internal", "message": str(e)})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，保持容器输出干净


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"木构件修复验收 API  listening on :{PORT}  db={DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
