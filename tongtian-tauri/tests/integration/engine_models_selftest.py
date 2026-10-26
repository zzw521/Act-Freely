#!/usr/bin/env python3
"""
engine/server.py 模型配置数据源自测（任务 #39）

浏览器直连模式下，引擎的 models.json 是模型清单的权威数据源。本自测锁定：
1. 清单读取视图永不回传明文 apiKey（只给 hasApiKey）
2. 新增/更新/删除/设为默认真实落盘；更新时 apiKey 缺省或占位符=保留，空串=清除
3. 默认模型最多一个；删除默认条目后不再谎报默认
4. 校验失败返回可读错误码（缺 baseUrl / modelName、非法协议）
5. Auto 解析取 isDefault；缺密钥的远端模型给出 MODEL_KEY_MISSING
6. 旧版整表接口按 id 合并，不会清空未出现在请求里的条目

所有读写都在临时 TONGTIANWORK_HOME 内，不触碰真实用户配置，也不发起网络请求。

用法: python tests/integration/engine_models_selftest.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

# Windows 控制台可能是 GBK/cp1250：中文断言输出不能因此崩掉
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

try:
    from fastapi.testclient import TestClient
    import server as engine_server
except Exception as exc:  # fastapi/httpx 缺失时优雅跳过
    print(f"SKIP: engine models selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class _FakeOllama(BaseHTTPRequestHandler):
    tags_body = b""

    def do_GET(self):  # noqa: N802
        if self.path != "/api/tags":
            self.send_response(404)
            self.end_headers()
            return
        body = type(self).tags_body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _serve(handler) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tt-models-"))
    home = tmp / "home"
    home.mkdir()
    prev_home = os.environ.get("TONGTIANWORK_HOME")
    os.environ["TONGTIANWORK_HOME"] = str(home)
    ollama_server = None
    not_ollama_server = None
    try:
        client = TestClient(engine_server.app)

        # 1 ── 空清单与真实路径
        empty = client.get("/api/models")
        body = empty.json()
        check("空清单：返回 ok 与空数组", empty.status_code == 200 and body.get("ok") is True
              and body.get("models") == [], empty.text[:200])
        check("空清单：路径指向隔离的 models.json",
              Path(body.get("path", "")) == home / "models.json", str(body.get("path")))
        check("空清单：没有默认模型", body.get("defaultId") is None, str(body.get("defaultId")))

        # 2 ── 新增：远端模型（含密钥）与本地 Ollama（免密钥）
        remote = client.post("/api/models", json={
            "name": "远端模型",
            "source": "openai_compat",
            "baseUrl": "https://models.example/v1",
            "apiKey": "sk-models-secret",
            "modelName": "remote-model",
            "contextWindow": 8192,
        })
        remote_body = remote.json()
        remote_id = (remote_body.get("model") or {}).get("id", "")
        check("新增：远端模型返回 200 和 id", remote.status_code == 200 and bool(remote_id),
              remote.text[:200])
        check("新增：读取视图不含明文密钥", "sk-models-secret" not in remote.text,
              remote.text[:200])
        check("新增：hasApiKey=true", (remote_body.get("model") or {}).get("hasApiKey") is True,
              str(remote_body)[:200])

        local = client.post("/api/models", json={
            "name": "本地 Ollama",
            "source": "ollama",
            "baseUrl": "",
            "modelName": "qwen2.5:14b",
        })
        local_body = local.json()
        local_id = (local_body.get("model") or {}).get("id", "")
        check("新增：Ollama 可按来源默认地址保存", local.status_code == 200 and bool(local_id),
              local.text[:200])
        check("新增：无密钥模型 hasApiKey=false", (local_body.get("model") or {}).get("hasApiKey") is False,
              str(local_body)[:200])

        listed = client.get("/api/models").json()
        check("清单：两个模型都在", len(listed.get("models") or []) == 2, str(listed)[:300])
        check("清单：所有条目都不含明文密钥",
              "sk-models-secret" not in str(listed), str(listed)[:300])

        # 3 ── 校验错误
        bad_base = client.post("/api/models", json={
            "name": "无端点", "source": "custom", "modelName": "x",
        })
        check("校验：自定义模型缺 baseUrl → 400 MODEL_INVALID",
              bad_base.status_code == 400 and bad_base.json().get("code") == "MODEL_INVALID",
              bad_base.text[:200])
        bad_model = client.post("/api/models", json={
            "name": "无模型名", "source": "openai_compat", "baseUrl": "https://x.example/v1",
        })
        check("校验：缺 modelName → 400 MODEL_INVALID",
              bad_model.status_code == 400 and bad_model.json().get("code") == "MODEL_INVALID",
              bad_model.text[:200])
        bad_scheme = client.post("/api/models", json={
            "name": "非法协议", "source": "openai_compat", "baseUrl": "ftp://x.example/v1",
            "modelName": "x",
        })
        check("校验：非 http(s) 协议 → 400 MODEL_INVALID",
              bad_scheme.status_code == 400 and bad_scheme.json().get("code") == "MODEL_INVALID",
              bad_scheme.text[:200])

        # 4 ── 更新：缺省/占位符保留密钥，空串清除
        kept = client.post("/api/models/update", json={
            "id": remote_id, "name": "远端模型改名", "contextWindow": 16384,
        })
        check("更新：缺省 apiKey 保留原密钥",
              kept.status_code == 200 and (kept.json().get("model") or {}).get("hasApiKey") is True,
              kept.text[:200])
        raw_after_keep = {m["id"]: m for m in engine_server._models_load()}
        check("更新：磁盘上密钥仍是原值",
              raw_after_keep[remote_id].get("apiKey") == "sk-models-secret",
              str(raw_after_keep.get(remote_id))[:200])

        placeholder = client.post("/api/models/update", json={
            "id": remote_id, "apiKey": engine_server._MODEL_KEY_PLACEHOLDER,
        })
        raw_after_placeholder = {m["id"]: m for m in engine_server._models_load()}
        check("更新：占位符不覆盖密钥",
              placeholder.status_code == 200
              and raw_after_placeholder[remote_id].get("apiKey") == "sk-models-secret",
              placeholder.text[:200])

        cleared = client.post("/api/models/update", json={"id": remote_id, "apiKey": ""})
        raw_after_clear = {m["id"]: m for m in engine_server._models_load()}
        check("更新：空串清除密钥", cleared.status_code == 200
              and raw_after_clear[remote_id].get("apiKey") == ""
              and (cleared.json().get("model") or {}).get("hasApiKey") is False,
              cleared.text[:200])
        check("更新：不存在的 id → 404 MODEL_NOT_FOUND",
              client.post("/api/models/update", json={"id": "m-nope", "name": "x"}).status_code == 404)

        # 5 ── 默认模型：最多一个；Auto 解析到它
        first_default = client.post("/api/models/default", json={"id": remote_id})
        check("默认：设为默认返回 200", first_default.status_code == 200,
              first_default.text[:200])
        second_default = client.post("/api/models/default", json={"id": local_id})
        listed = client.get("/api/models").json()
        defaults = [m for m in (listed.get("models") or []) if m.get("isDefault")]
        check("默认：切换后只有一个默认模型",
              second_default.status_code == 200 and len(defaults) == 1
              and defaults[0].get("id") == local_id, str(listed)[:300])
        check("默认：defaultId 与清单一致", listed.get("defaultId") == local_id,
              str(listed.get("defaultId")))
        check("默认：找不到的 id → 404",
              client.post("/api/models/default", json={"id": "m-nope"}).status_code == 404)

        auto_entry, auto_err = engine_server._models_find(None)
        check("Auto：未选档位/id 时解析到默认条目",
              auto_err == "" and auto_entry is not None and auto_entry.get("id") == local_id,
              str(auto_entry)[:200])
        auto_cfg, auto_cfg_err = engine_server._model_runtime_config(auto_entry)
        check("Auto：Ollama 免密钥可生成调用配置",
              auto_cfg_err == "" and auto_cfg is not None
              and auto_cfg.get("baseUrl") == "http://127.0.0.1:11434/v1",
              str(auto_cfg))

        # 远端无密钥时明确报错，不静默用空密钥调用
        no_key_entry = {**raw_after_clear[remote_id], "apiKey": ""}
        _, key_err = engine_server._model_runtime_config(no_key_entry)
        check("运行配置：远端模型缺密钥 → MODEL_KEY_MISSING",
              key_err.startswith("MODEL_KEY_MISSING"), key_err)

        # 6 ── 旧版整表接口按 id 合并
        legacy = client.post("/api/models/config", json={"models": [
            {"id": remote_id, "name": "旧接口改名"},
        ]})
        legacy_models = {m["id"]: m for m in engine_server._models_load()}
        check("旧接口：按 id 更新且保留其他条目",
              legacy.status_code == 200 and legacy_models[remote_id].get("name") == "旧接口改名"
              and local_id in legacy_models, legacy.text[:300])
        check("旧接口：读取视图同样脱敏",
              "sk-models-secret" not in legacy.text, legacy.text[:300])

        # 7 ── 删除：未知 id 报错；删默认后不再谎报默认
        missing = client.post("/api/models/delete", json={"id": "m-nope"})
        check("删除：不存在的 id → 404 MODEL_NOT_FOUND",
              missing.status_code == 404 and missing.json().get("code") == "MODEL_NOT_FOUND",
              missing.text[:200])
        deleted = client.post("/api/models/delete", json={"id": local_id})
        after_delete = client.get("/api/models").json()
        check("删除：默认条目移除后 defaultId 为空",
              deleted.status_code == 200 and after_delete.get("defaultId") is None,
              str(after_delete)[:300])
        check("删除：只移除指定条目", len(after_delete.get("models") or []) == 1
              and (after_delete.get("models") or [])[0].get("id") == remote_id,
              str(after_delete)[:300])

        # 8 ── 零配置本地模型：真实探测假 Ollama（只允许回环，不伪报成功）
        _FakeOllama.tags_body = json.dumps({
            "models": [
                {"name": "qwen2.5:14b", "size": 9_000_000_000,
                 "modified_at": "2026-10-01T00:00:00Z"},
                {"model": "llama3.2:latest", "size": 2_000_000_000},
                {"name": "qwen2.5:14b", "size": 1},
            ]
        }).encode("utf-8")
        ollama_server = _serve(_FakeOllama)
        ollama_port = ollama_server.server_address[1]

        found = client.post("/api/models/detect-local", json={
            "baseUrl": f"http://127.0.0.1:{ollama_port}/v1",
        })
        found_body = found.json()
        found_models = found_body.get("models") or []
        check("本地检测：真实解析 /api/tags 并返回可用",
              found.status_code == 200 and found_body.get("supported") is True
              and found_body.get("available") is True, found.text[:300])
        check("本地检测：端点去掉 /v1，OpenAI baseUrl 保留 /v1",
              found_body.get("endpoint") == f"http://127.0.0.1:{ollama_port}"
              and found_body.get("openaiBaseUrl") == f"http://127.0.0.1:{ollama_port}/v1",
              str(found_body)[:300])
        check("本地检测：name/model 字段都识别且去重",
              [m.get("name") for m in found_models] == ["qwen2.5:14b", "llama3.2:latest"],
              str(found_models)[:300])

        remote = client.post("/api/models/detect-local", json={
            "baseUrl": "https://api.example.com/v1",
        })
        check("本地检测：非回环地址 → 400 LOCAL_MODEL_INVALID",
              remote.status_code == 400 and remote.json().get("code") == "LOCAL_MODEL_INVALID",
              remote.text[:200])
        bad_path = client.post("/api/models/detect-local", json={
            "baseUrl": f"http://127.0.0.1:{ollama_port}/api/tags",
        })
        check("本地检测：无关路径 → 400 LOCAL_MODEL_INVALID",
              bad_path.status_code == 400
              and bad_path.json().get("code") == "LOCAL_MODEL_INVALID",
              bad_path.text[:200])

        down = client.post("/api/models/detect-local", json={
            "baseUrl": "http://127.0.0.1:1",
        })
        down_body = down.json()
        check("本地检测：端口不通 → 200 且 available=false，不伪报成功",
              down.status_code == 200 and down_body.get("supported") is True
              and down_body.get("available") is False
              and str(down_body.get("error") or "").startswith("LOCAL_MODEL_NOT_RUNNING"),
              down.text[:300])

        class _NotOllama(_FakeOllama):
            tags_body = b'{"ok": true}'

        not_ollama_server = _serve(_NotOllama)
        wrong = client.post("/api/models/detect-local", json={
            "baseUrl": f"http://127.0.0.1:{not_ollama_server.server_address[1]}",
        })
        wrong_body = wrong.json()
        check("本地检测：对面不是 Ollama → available=false 并说明缺 models 数组",
              wrong_body.get("available") is False
              and "没有 models 数组" in str(wrong_body.get("error") or ""),
              wrong.text[:300])
    finally:
        for server in (ollama_server, not_ollama_server):
            if server is not None:
                server.shutdown()
                server.server_close()
        if prev_home is None:
            os.environ.pop("TONGTIANWORK_HOME", None)
        else:
            os.environ["TONGTIANWORK_HOME"] = prev_home
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 50)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine models selftest: ALL PASS")


if __name__ == "__main__":
    main()
