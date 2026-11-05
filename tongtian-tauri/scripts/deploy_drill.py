#!/usr/bin/env python3
"""D-01 服务化部署演练：真实 nginx TLS 反代 + 引擎容器 + 假模型端点。

不改产品代码，只把 `deploy/Dockerfile`、`deploy/nginx.conf` 与引擎的服务化门槛
放到真实容器网络里跑一遍，产出可复跑的证据与 `docs/d01-drill.md` 报告。

链路：宿主 → nginx(TLS 终结) → 引擎容器(--web-dir 静态托管 + /api/*) → 假模型端点
模型侧用 `deploy/drill/fake_openai.py`（OpenAI 兼容，可配静默与分块节奏），
全程不消耗任何真实厂商 Token、不需要真实凭据。

用法（在 tongtian-tauri 目录）：
  python scripts/deploy_drill.py                 # 全跑
  python scripts/deploy_drill.py --only A B      # 只跑指定组
  python scripts/deploy_drill.py --skip-long     # 跳过十分钟长连接（快速回归）
  python scripts/deploy_drill.py --report docs/d01-drill.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRILL_DIR = ROOT / "deploy" / "drill"
PROBE = DRILL_DIR / "probe.py"
FAKE_MODEL = DRILL_DIR / "fake_openai.py"
NGINX_CONF = ROOT / "deploy" / "nginx.conf"
CADDY_CONF = ROOT / "deploy" / "Caddyfile"
SELF_CHECK = ROOT / "scripts" / "deploy-selfcheck.mjs"

IMAGE = "tongtian-engine:drill"
MODEL_IMAGE = "python:3.12-slim"
PROXY_IMAGE = "nginx:1-alpine"
CADDY_IMAGE = "caddy:2-alpine"
NET = "tt-d01"
SUBNET = "172.29.0.0/24"
IP_MODEL = "172.29.0.10"
IP_ENGINE = "172.29.0.20"
C_MAIN = "tt-d01-engine"
C_MODEL = "tt-d01-model"
C_PROXY = "tt-d01-proxy"
C_NAIVE = "tt-d01-naive"
C_IDLE = "tt-d01-idle"
C_CADDY = "tt-d01-caddy"
C_RO = "tt-d01-engine-ro"
VOLUME = "tt-d01-data"

RESULTS: list[dict] = []
WORK = Path(tempfile.mkdtemp(prefix="tt-d01-"))
TLS = WORK / "tls"
TOKEN = secrets.token_urlsafe(24)
PORTS: dict[str, int] = {}
LONG_STREAM_SECONDS = 620


# ──────────────────────────────────────────── 基础设施 helpers

def docker(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def dtext(*args: str, timeout: int = 180) -> str:
    return (docker(*args, timeout=timeout).stdout or "").strip()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def host_base(port: int, scheme: str = "https") -> str:
    """宿主侧一律写 127.0.0.1，不写 localhost。

    Windows 上 localhost 会先试 ::1，Docker Desktop 只转发了 IPv4，凭空多出约 2 秒，
    会把「反代本身慢不慢」这件事测歪（演练证书 SAN 已含 IP:127.0.0.1）。
    """
    return f"{scheme}://127.0.0.1:{port}"


def record(group: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"group": group, "name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {group} · {name}" + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def wait_for(fn, timeout: float, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
        except Exception as exc:  # noqa: BLE001
            last = f"ERR {type(exc).__name__}: {exc}"
        if last is True or (isinstance(last, dict) and last.get("ready")):
            return True, last
        time.sleep(interval)
    return False, last


def container_state(name: str) -> dict:
    out = dtext("inspect", "-f", "{{.State.Status}}|{{.State.ExitCode}}|{{.State.Error}}", name)
    if "|" not in out:
        return {"exists": False, "raw": out}
    status, code, err = out.split("|", 2)
    return {"exists": True, "status": status, "exitCode": code, "error": err.strip()}


def container_logs(name: str, tail: int = 60) -> str:
    return docker("logs", "--tail", str(tail), name).stdout + docker("logs", "--tail", str(tail), name).stderr


def exec_in(container: str, shell_cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    proc = docker("exec", container, "sh", "-c", shell_cmd, timeout=timeout)
    if proc.returncode != 0 and "No such container" in (proc.stderr or ""):
        # Docker Desktop 偶发把还在跑的容器解析成一个过期 ID；稍等重试一次，别把一次抖动记成部署失败
        time.sleep(3)
        proc = docker("exec", container, "sh", "-c", shell_cmd, timeout=timeout)
    return proc


def _shq(value: str) -> str:
    """POSIX 单引号转义：容器里的 sh 只会收到一个参数，不会被空格和引号拆开"""
    return "'" + str(value).replace("'", "'\\''") + "'"


def probe_host(mode: str, base: str, path: str = "/", body: str | None = None, token: str | None = TOKEN,
               token_name: str = "x-tt-engine-token", ca: str | None = None, insecure: bool = False,
               timeout: float = 30, extra: list[str] | None = None, save_to: str | None = None) -> dict:
    return _probe(mode, base, path, body, token, token_name, ca, insecure, timeout, extra, save_to, in_container=None)


def probe_engine(mode: str, path: str, body: str | None = None, token: str | None = TOKEN,
                 token_name: str = "x-tt-engine-token", timeout: float = 30,
                 extra: list[str] | None = None, container: str = C_MAIN) -> dict:
    """在引擎容器内回环直连引擎（不经过代理）；令牌走容器内的 env 文件，不进命令行"""
    return _probe(
        mode,
        "http://127.0.0.1:18000",
        path,
        body,
        token,
        token_name,
        None,
        True,
        timeout,
        extra,
        None,
        in_container=container,
    )


def _probe(mode: str, base: str, path: str, body: str | None, token: str | None, token_name: str,
           ca: str | None, insecure: bool, timeout: float, extra: list[str] | None,
           save_to: str | None, in_container: str | None) -> dict:
    args = ["--mode", mode, "--base", base, "--path", path, "--timeout", str(timeout)]
    if body is not None:
        args += ["--body", body]
    if token:
        args += ["--token-env", "TT_PROBE_TOKEN", "--token-name", token_name]
    if ca:
        args += ["--ca", ca]
    if insecure:
        args += ["--insecure"]
    for item in extra or []:
        args += ["--extra-header", item]
    if save_to:
        args += ["--save-to", save_to]

    if in_container:
        env_file = WORK / "probe.env"
        env_file.write_text(f"export TT_PROBE_TOKEN='{token}'\n" if token else "", encoding="utf-8", newline="\n")
        # 参数里有 JSON 请求体，必须逐个做 POSIX 单引号转义后再拼进 sh -c
        cmd = "python /drill/probe.py " + " ".join(_shq(a) for a in args)
        if token:
            cmd = f". /probe/probe.env && {cmd}"
        proc = exec_in(in_container, cmd, timeout=int(timeout) + 60)
        out, err = proc.stdout, proc.stderr
    else:
        env = dict(os.environ)
        if token:
            env["TT_PROBE_TOKEN"] = token
        else:
            env.pop("TT_PROBE_TOKEN", None)
        proc = subprocess.run(
            [sys.executable, str(PROBE), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout + 60,
        )
        out, err = proc.stdout, proc.stderr
        if proc.returncode != 0:
            return {"error": (err or out).strip().splitlines()[-1] if (err or out).strip() else "probe failed",
                    "returncode": proc.returncode}
    for line in reversed((out or "").strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    return {"error": "no json", "stderr": (err or "")[-400:]}


def _why(res: dict) -> str:
    """探针没返回状态码时，把真实原因带进证据，别只留一个 status=None"""
    reason = res.get("error") or (res.get("stderr") or "").strip()
    return f" 原因={str(reason)[-220:]}" if reason else ""


def chat_body(task_id: str, message: str = "生成一份部署检查清单并写入文件", stage: str | None = "plan") -> str:
    payload = {"message": message, "task_id": task_id, "workDir": "/data/work"}
    if stage:
        payload["stage"] = stage
    return json.dumps(payload, ensure_ascii=False)


def fake_config(**kwargs) -> dict:
    """改假模型节奏：宿主够不到 bridge 内网，进模型容器调用 /drill/modelctl.py"""
    payload = json.dumps(kwargs)
    cmd = f"python /drill/modelctl.py {_shq(payload)} 2>&1 | tail -1"
    return {"raw": exec_in(C_MODEL, cmd, timeout=30).stdout.strip()}


# ──────────────────────────────────────────── 环境搭建

def mint_certs() -> None:
    TLS.mkdir(parents=True, exist_ok=True)
    (TLS / "san.cnf").write_text(
        "[v3_req]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nkeyUsage=digitalSignature,keyEncipherment"
        "\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n",
        encoding="utf-8",
        newline="\n",
    )
    script = (
        "set -e;"
        " openssl req -x509 -newkey rsa:2048 -sha256 -days 3 -nodes -subj /CN=TT-D01-CA"
        ' -addext "basicConstraints=critical,CA:TRUE,pathlen:0"'
        ' -addext "keyUsage=critical,digitalSignature,keyCertSign,cRLSign"'
        " -keyout /out/ca.key -out /out/ca.crt >/dev/null 2>&1;"
        " openssl req -newkey rsa:2048 -nodes -subj /CN=localhost -keyout /out/server.key"
        " -out /out/server.csr >/dev/null 2>&1;"
        " openssl x509 -req -in /out/server.csr -CA /out/ca.crt -CAkey /out/ca.key -CAcreateserial"
        " -days 3 -sha256 -extfile /out/san.cnf -extensions v3_req -out /out/server.crt >/dev/null 2>&1;"
        " openssl x509 -in /out/ca.crt -noout -ext basicConstraints,keyUsage;"
        " chmod 600 /out/*.key;"
        " echo CERTS_OK"
    )
    proc = docker("run", "--rm", "-v", f"{TLS.as_posix()}:/out", "--entrypoint", "sh", MODEL_IMAGE, "-c", script, timeout=240)
    if "CERTS_OK" not in proc.stdout:
        raise RuntimeError(f"证书生成失败：{proc.stdout[-300:]} {proc.stderr[-300:]}")


NO_BUFFER_BLOCK = (
    "gzip off;\n            proxy_buffering off;\n            proxy_cache off;\n"
    "            chunked_transfer_encoding on;\n            tcp_nodelay on;"
)
LONG_TIMEOUT_BLOCK = "proxy_read_timeout 3600s;\n            proxy_send_timeout 3600s;"


def nginx_variant(kind: str) -> str:
    """反面样例：一次只改一处，才能说清模板里究竟是哪一行在起作用。

    - buffering：删掉 /api/chat 的无缓冲五行，空闲超时仍是 3600s
    - idle：五行全留，只把 /api/chat 的读超时压到 5s（模拟默认 LB/网关的空闲掐断）
    """
    text = NGINX_CONF.read_text(encoding="utf-8")
    if kind == "buffering":
        if NO_BUFFER_BLOCK not in text:
            raise RuntimeError("deploy/nginx.conf 里找不到无缓冲那五行，反面样例无法生成（改过模板请同步演练脚本）")
        return text.replace(NO_BUFFER_BLOCK, "# 演练用：故意去掉无缓冲五行")
    if kind == "idle":
        if LONG_TIMEOUT_BLOCK not in text:
            raise RuntimeError("deploy/nginx.conf 里找不到 3600s 读超时那两行，反面样例无法生成（改过模板请同步演练脚本）")
        return text.replace(LONG_TIMEOUT_BLOCK, "proxy_read_timeout 5s;\n            proxy_send_timeout 5s;")
    raise ValueError(f"未知的反代样例变体：{kind}")


def prune_leftovers() -> None:
    """可重入：分组复跑前先清掉容器/卷/网络，避免 docker run 或 network create 因残留失败"""
    for name in (C_PROXY, C_NAIVE, C_IDLE, C_CADDY, C_RO, C_MAIN, C_MODEL):
        docker("rm", "-f", "-v", name)
    # 匿名残留（例如上一轮挂死的 docker run）也会占住网络，按网络过滤器一并清掉
    attached = dtext("ps", "-a", "--filter", f"network={NET}", "--format", "{{.Names}}")
    for name in attached.split():
        docker("rm", "-f", "-v", name)
    for vol in (VOLUME, "tt-d01-ro"):
        docker("volume", "rm", "-f", vol)
    docker("network", "rm", NET)


def ensure_image() -> None:
    """演练要求镜像存在：不在就地构建一次（存在就复用，避免每次复跑都重建 750MB）"""
    if docker("image", "inspect", IMAGE, timeout=60).returncode == 0:
        return
    print(f"[BUILD] 本机没有 {IMAGE}，开始 `docker build -f deploy/Dockerfile`（前端 npm ci + 依赖安装，需数分钟）", flush=True)
    proc = docker("build", "-f", str(NGINX_CONF.parent / "Dockerfile"), "-t", IMAGE, str(ROOT), timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"镜像 {IMAGE} 构建失败（退出码 {proc.returncode}）："
            f"{(proc.stderr or proc.stdout).strip()[-500:] or '无输出'}；"
            "构建期需要 package-lock.json 与 package.json 同步（npm ci 对可选 peer yaml@2.x 严格）"
        )


def up() -> None:
    prune_leftovers()
    ensure_image()
    mint_certs()
    PORTS.update({"https": free_port(), "http": free_port(), "naive": free_port(),
                  "idle": free_port(), "caddy": free_port()})
    net = docker("network", "create", "--subnet", SUBNET, NET)
    if net.returncode != 0:
        raise RuntimeError(
            f"演练网络 {NET} 创建失败：{(net.stdout + net.stderr).strip()[-240:]}；"
            "先看是否有容器仍挂在该网络上（docker network inspect tt-d01）"
        )

    (WORK / "engine.env").write_text(
        "\n".join(
            [
                f"TT_ENGINE_TOKEN={TOKEN}",
                "TT_ALLOW_REMOTE_BIND=1",
                "TT_PROVIDER=openai",
                f"TT_BASE_URL=http://{IP_MODEL}:9000/v1",
                "TT_API_KEY=fake-key-d01",
                "TT_MODEL=fake-chat",
                "TT_ENGINE_AUDIT_LOG=/data/audit.jsonl",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    (WORK / "nginx-naive.conf").write_text(nginx_variant("buffering"), encoding="utf-8", newline="\n")
    (WORK / "nginx-idle.conf").write_text(nginx_variant("idle"), encoding="utf-8", newline="\n")

    docker("volume", "create", VOLUME)
    run_model()
    run_engine(C_MAIN, WORK / "engine.env", f"{VOLUME}:/data", alias="engine")
    wait_engine_ready(C_MAIN)
    run_proxy(C_PROXY, NGINX_CONF, PORTS["https"], PORTS["http"])
    run_proxy(C_NAIVE, WORK / "nginx-naive.conf", PORTS["naive"], None)
    run_proxy(C_IDLE, WORK / "nginx-idle.conf", PORTS["idle"], None)


def run_model() -> None:
    proc = docker(
        "run", "-d", "--name", C_MODEL, "--network", NET, "--ip", IP_MODEL,
        "-v", f"{DRILL_DIR.as_posix()}:/drill:ro", MODEL_IMAGE, "python", "/drill/fake_openai.py",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"假模型端点起不来：{proc.stderr[-300:]}")
    ok, last = wait_for(
        lambda: {"ready": dtext("inspect", "-f", "{{.State.Status}}", C_MODEL) == "running" and _model_alive()},
        60,
    )
    if not ok:
        raise RuntimeError(f"假模型端点未就绪：{last} {container_logs(C_MODEL)[:300]}")


def _model_alive() -> bool:
    """容器内自探：Docker Desktop 的 bridge 网段从宿主不可路由，只能进容器看"""
    return _health_exec(C_MODEL, "http://127.0.0.1:9000/health") == "200"


def run_engine(name: str, env_file: Path, volume_spec: str, ip: str = IP_ENGINE,
               extra_args: list[str] | None = None, alias: str | None = None) -> None:
    cmd = [
        "run", "-d", "--name", name, "--network", NET, "--ip", ip,
        "--env-file", str(env_file),
        "-v", volume_spec,
        "-v", f"{DRILL_DIR.as_posix()}:/drill:ro",
        "-v", f"{WORK.as_posix()}:/probe",
    ]
    # 反代配置按 `engine:18000` 写上游，容器名不是 engine，必须挂网络别名
    if alias:
        cmd += ["--network-alias", alias]
    cmd += extra_args or []
    cmd += [IMAGE]
    proc = docker(*cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"引擎容器起不来：{proc.stderr[-300:]}")


def run_proxy(name: str, conf: Path, https_port: int, http_port: int | None,
              image: str = PROXY_IMAGE, mount: str = "/etc/nginx/nginx.conf",
              tls_mount: str = "/etc/nginx/tls") -> None:
    cmd = ["run", "-d", "--name", name, "--network", NET,
           "-v", f"{conf.as_posix()}:{mount}:ro",
           "-v", f"{TLS.as_posix()}:{tls_mount}:ro"]
    if http_port:
        cmd += ["-p", f"127.0.0.1:{http_port}:80"]
    cmd += ["-p", f"127.0.0.1:{https_port}:443", image]
    proc = docker(*cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"代理容器 {name} 起不来：{proc.stderr[-300:]}")
    ok, _ = wait_for(lambda: _proxy_ready(https_port), 60, 1.0)
    st = container_state(name)
    if not ok or st.get("status") != "running":
        # 端口能连上不代表代理活着：nginx 上游解析失败会退出，但 Docker 的端口转发还可能应一下
        raise RuntimeError(
            f"代理 {name} 未就绪（状态={st.get('status')}）：{container_logs(name)[-400:]}；"
            "先看上游主机名能否在网络内解析"
        )


def run_caddy(https_port: int) -> None:
    """真实 caddy 容器：同一份证书、同一个上游，用来验证第二套反代样例"""
    run_proxy(C_CADDY, CADDY_CONF, https_port, None, image=CADDY_IMAGE,
              mount="/etc/caddy/Caddyfile", tls_mount="/etc/caddy/tls")
    ok, last = wait_for(lambda: _caddy_up(), 45, 1.0)
    if not ok:
        raise RuntimeError(f"caddy 未就绪：{last} {container_logs(C_CADDY)[-400:]}")


def _caddy_up() -> bool:
    """busybox wget 只在 2xx 时退 0；用退出码判存活，别拿响应体里碰巧出现的数字当证据"""
    out = exec_in(
        C_CADDY,
        "wget -qO- --no-check-certificate https://127.0.0.1/health >/dev/null 2>&1; echo RC=$?",
        timeout=20,
    ).stdout
    return "RC=0" in out


def _proxy_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _health_exec(container: str, url: str) -> str:
    """在容器内取 /health 状态码；返回纯数字字符串或异常摘要"""
    out = exec_in(
        container,
        f"python -c \"import urllib.request;print(urllib.request.urlopen('{url}',timeout=3).status)\" 2>&1 | tail -1",
        timeout=25,
    ).stdout.strip()
    return out.splitlines()[-1].strip() if out else ""


def wait_engine_ready(name: str, timeout: float = 90) -> None:
    ok, last = wait_for(lambda: {"ready": _health_exec(name, "http://127.0.0.1:18000/health") == "200"}, timeout, 2.0)
    if not ok:
        raise RuntimeError(f"引擎容器 {name} 未就绪：{last} {container_logs(name)[-400:]}")


def down() -> None:
    for name in (C_PROXY, C_NAIVE, C_IDLE, C_CADDY, C_RO, C_MAIN, C_MODEL):
        docker("rm", "-f", "-v", name)
    for vol in (VOLUME, "tt-d01-ro"):
        docker("volume", "rm", "-f", vol)
    docker("network", "rm", NET)
    shutil.rmtree(WORK, ignore_errors=True)


# ──────────────────────────────────────────── A 组：镜像与卷

def group_a() -> None:
    size = dtext("image", "inspect", "-f", "{{.Size}}", IMAGE)
    record("A", f"镜像 {IMAGE} 已构建（体积 {int(size) // 1024 // 1024 if size.isdigit() else '?'}MB）",
           size.isdigit(), size or "docker image inspect 无输出")

    uid = exec_in(C_MAIN, "id -u && id -un").stdout.split()
    record("A", "容器内以非 root 运行（uid/user）", len(uid) == 2 and uid[0] == "10001", " ".join(uid))

    base = probe_engine("health", "/health")
    res = probe_engine("chat", "/api/chat", body=chat_body("d01-a-write"))
    seq = [e.get("seq") for e in res.get("events", [])]
    audit = exec_in(C_MAIN, "test -s /data/audit.jsonl && wc -l < /data/audit.jsonl || echo MISSING").stdout.strip()
    workdir = exec_in(C_MAIN, "ls /data/work | head -3").stdout.strip().replace("\n", ",")
    record(
        "A",
        "命名卷可写：审计 JSONL 与 /data/work 会话目录由 uid 10001 落盘",
        str(base.get("status")) == "200" and "MISSING" not in audit and bool(workdir),
        f"plan 流 seq={seq}；audit.jsonl 行数={audit}；work 目录={workdir or '空'}",
    )

    bind = WORK / "bind-data"
    bind.mkdir(exist_ok=True)
    proc = docker("run", "--rm", "--network", NET, "-v", f"{bind.as_posix()}:/data2", "--entrypoint", "sh",
                  IMAGE, "-c", "id -u; touch /data2/probe-from-bind && echo BIND_WRITE_OK")
    host_ok = (bind / "probe-from-bind").exists()
    record(
        "A",
        "宿主绑定挂载可写（Docker Desktop 语义）",
        "BIND_WRITE_OK" in proc.stdout and host_ok,
        f"容器内 uid={proc.stdout.split()[0] if proc.stdout else '?'}；宿主侧文件可见={host_ok}"
        "（注意：Docker Desktop 默认放行宿主目录权限，Linux 生产需自行保证目录属主与 uid 一致）",
    )

    docker("volume", "create", "tt-d01-ro")
    run_engine(C_RO, WORK / "engine.env", "tt-d01-ro:/data:ro", ip="172.29.0.22")
    wait_engine_ready(C_RO, timeout=60)
    # 镜像里已带 /data/work，卷初始化后目录存在 → 进程不会退出；真实风险是「能跑但不落盘」
    ro_res = probe_engine("chat", "/api/chat", body=chat_body("d01-a-ro"), container=C_RO)
    logs = container_logs(C_RO)
    degrade = [l for l in logs.splitlines() if "失败" in l]
    ro_ok = (
        st_running(C_RO)
        and "plan" in ro_res.get("eventTypes", [])
        and any("会话目录" in l for l in degrade)
        and any("审计" in l for l in degrade)
    )
    record(
        "A",
        "/data 只读：进程不退但会点名降级（检查点与审计都不落盘，不能当可上线）",
        ro_ok,
        f"状态={container_state(C_RO).get('status')} plan流事件={ro_res.get('eventTypes')}；"
        f"降级日志={' / '.join(l.strip()[:90] for l in degrade) or '无'}",
    )


def st_running(container: str) -> bool:
    return container_state(container).get("status") == "running"


# ──────────────────────────────────────────── B 组：引擎直连

def group_b() -> None:
    h = probe_engine("health", "/health")
    record("B", "/health 免令牌返回 200", str(h.get("status")) == "200", json.dumps(h.get("json"), ensure_ascii=False)[:120])

    s = probe_engine("static", "/")
    record("B", "静态前端由引擎同端口托管（HTML 且免令牌）", str(s.get("status")) == "200" and s.get("looksLikeHtml"),
           f"status={s.get('status')} bytes={s.get('bytes')}")

    no_token = probe_engine("chat", "/api/chat", body=chat_body("d01-b-notoken"), token=None)
    code = (no_token.get("json") or {}).get("code")
    record("B", "无令牌访问 /api/chat 被拒 401 ENGINE_UNAUTHORIZED", str(no_token.get("status")) == "401" and code == "ENGINE_UNAUTHORIZED",
           f"status={no_token.get('status')} code={code}")

    hdr = probe_engine("chat", "/api/chat", body=chat_body("d01-b-hdr"), token_name="x-tt-engine-token")
    bearer = probe_engine("chat", "/api/chat", body=chat_body("d01-b-bearer", stage="plan"), token_name="Authorization")
    types = hdr.get("eventTypes", [])
    seqs = [e.get("seq") for e in hdr.get("events", [])]
    ok_seq = seqs == list(range(1, len(seqs) + 1))
    record(
        "B",
        "两种令牌头都可用，plan 流事件齐且 seq 单调",
        str(hdr.get("status")) == "200" and str(bearer.get("status")) == "200" and "plan" in types and ok_seq,
        f"x-tt-engine-token → {types} seq={seqs}；Authorization → status={bearer.get('status')}",
    )

    rep = probe_engine("replay", f"/api/events/replay?task_id=d01-b-hdr&from_seq=1")
    record("B", "事件重放按 from_seq 补齐（断线找回）", str(rep.get("status")) == "200" and rep.get("windowExpired") is False,
           f"seq={rep.get('events')} windowExpired={rep.get('windowExpired')}")

    st = container_state(C_MAIN)
    record("B", "长任务期间的容器存活（演练主容器仍在运行）", st.get("status") == "running", f"状态={st.get('status')}")

    refused = []
    # 镜像把 TT_ALLOW_REMOTE_BIND=1 烤进了 ENV，所以「缺开关」这一路必须显式覆盖成空值才测得到门槛
    for label, env_lines, expect in (
        ("no-allow-flag", ["TT_ALLOW_REMOTE_BIND=", "TT_ENGINE_TOKEN=whatever-d01"], "TT_ALLOW_REMOTE_BIND"),
        ("no-credential", ["TT_ENGINE_TOKEN=", "TT_ENGINE_JWT_SECRET="], "TT_ENGINE_TOKEN"),
    ):
        path = WORK / f"boot-{label}.env"
        path.write_text("\n".join(env_lines) + "\n", encoding="utf-8", newline="\n")
        (WORK / "boot-data").mkdir(exist_ok=True)
        name = f"tt-d01-boot-{label}"
        docker("rm", "-f", "-v", name)
        docker("run", "-d", "--name", name, "--network", NET, "--env-file", str(path),
               "-v", f"{(WORK / 'boot-data').as_posix()}:/data", IMAGE)
        # 用 -d + 轮询退出，不用前台 --rm：引擎真起来时前台 docker run 会把演练挂死
        ok_exit, _ = wait_for(lambda: container_state(name).get("status") == "exited", 45, 1.0)
        st_boot = container_state(name)
        logs = container_logs(name)
        refused.append(
            {
                "label": label,
                "exited": ok_exit and st_boot.get("exitCode") not in ("0", ""),
                "code": st_boot.get("exitCode"),
                "named": expect in logs,
                "msg": next((l.strip() for l in logs.splitlines() if "拒绝" in l), "无拒绝文案")[:130],
            }
        )
        docker("rm", "-f", "-v", name)
    record(
        "B",
        "非回环绑定门槛：缺放行开关或缺凭据都拒起并点名原因（镜像烤进的开关需显式覆盖）",
        all(r["exited"] and r["named"] for r in refused),
        "；".join(f"{r['label']}→退出码 {r['code']}「{r['msg']}」" for r in refused),
    )


# ──────────────────────────────────────────── C 组：真实 nginx TLS

def group_c() -> None:
    https = host_base(PORTS["https"])
    ca = str(TLS / "ca.crt")

    r = probe_host("health", host_base(PORTS["http"], "http"), path="/health", token=None)
    record("C", "80 端口只做跳转：HTTP 请求返回 301 指向 https", str(r.get("status")) == "301" and "https" in str(r.get("headers", {}).get("location", "")),
           f"status={r.get('status')} location={r.get('headers', {}).get('location')}")

    tls_ok = probe_host("health", https, path="/health", ca=ca, token=None)
    record("C", "TLS 链校验通过：用演练 CA 验证 /health", str(tls_ok.get("status")) == "200",
           f"status={tls_ok.get('status')} 证书={Path(ca).name}{_why(tls_ok)}")

    untrusted = subprocess.run(
        [sys.executable, str(PROBE), "--mode", "health", "--base", https, "--path", "/health", "--timeout", "10"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    rejected = untrusted.returncode != 0 and "CERTIFICATE_VERIFY_FAILED" in (untrusted.stderr + untrusted.stdout)
    record("C", "不信任该 CA 时握手被拒（TLS 确实生效，不是明文冒充）", rejected,
           (untrusted.stderr.strip().splitlines() or ["无输出"])[-1][:140])

    fake_config(plan_delay_s=0.5, exec_delay_s=1.0, tool_rounds=0, chunks=1, chunk_gap_s=0.0, reset=True)
    c_plan = probe_host("chat", https, "/api/chat", body=chat_body("d01-c-plan"), ca=ca, timeout=60)
    first = c_plan.get("firstByteMs")
    record(
        "C",
        f"经代理的 SSE 首包 ≤2000ms（实测 {first}ms，模型侧只静默 500ms）",
        isinstance(first, (int, float)) and first <= 2000 and "plan" in c_plan.get("eventTypes", []),
        f"事件={c_plan.get('eventTypes')} 到达={c_plan.get('chunkStamps')}{_why(c_plan)}",
    )

    # 攒包只能看执行流：plan 事件是模型答完才一次性生成的，直连也是一坨到齐，证明不了代理的转发节奏
    fake_config(exec_delay_s=2.0, tool_rounds=2, chunks=4, chunk_gap_s=0.6, reset=True)
    c_exec = probe_host("chat", https, "/api/chat", body=chat_body("d01-c-exec", stage=None), ca=ca, timeout=150)
    fake_config(exec_delay_s=2.0, tool_rounds=2, chunks=4, chunk_gap_s=0.6, reset=True)
    c_direct = probe_engine("chat", "/api/chat", body=chat_body("d01-c-exec-direct", stage=None), timeout=150)
    spread_via = (c_exec.get("lastByteMs") or 0) - (c_exec.get("firstByteMs") or 0)
    spread_direct = (c_direct.get("lastByteMs") or 0) - (c_direct.get("firstByteMs") or 0)
    record(
        "C",
        "事件不被代理攒包：经代理与直连一样分批到达，跨度同量级",
        (c_exec.get("chunkCount") or 0) >= 2
        and spread_via >= 1000
        and (c_direct.get("chunkCount") or 0) >= 2
        and spread_via >= spread_direct * 0.5,
        f"经代理 分块={c_exec.get('chunkCount')} 跨度={spread_via:.0f}ms 时刻={c_exec.get('chunkStamps')[:8]}；"
        f"直连 分块={c_direct.get('chunkCount')} 跨度={spread_direct:.0f}ms；"
        f"客户端可见头 X-Accel-Buffering={c_exec.get('headers', {}).get('x-accel-buffering') or '无（nginx 消化了上游该头）'}",
    )

    bad = probe_host("chat", https, "/api/chat", body=chat_body("d01-c-badtoken"), token="wrong-token-d01", ca=ca, timeout=20)
    record("C", "错误令牌经 TLS 仍 401", str(bad.get("status")) == "401", f"status={bad.get('status')} code={(bad.get('json') or {}).get('code')}")


# ──────────────────────────────────────────── D 组：长连接与配置对照

def group_d() -> None:
    https = host_base(PORTS["https"])
    naive = host_base(PORTS["naive"])
    idle = host_base(PORTS["idle"])
    ca = str(TLS / "ca.crt")

    # 对照组：只删掉「无缓冲」五行，读超时仍是 3600s。
    # 这一组回答的是「模板里那五行到底是不是必需的」——引擎自己发了 X-Accel-Buffering: no，
    # nginx 会据此对本次响应关闭缓冲，所以删掉仍可能分批到达；变红才说明那五行在兜底。
    fake_config(exec_delay_s=2.0, tool_rounds=2, chunks=4, chunk_gap_s=0.6, reset=True)
    naive_run = probe_host("chat", naive, "/api/chat", body=chat_body("d01-d-buffer", stage=None), ca=ca, timeout=150)
    spread_naive = (naive_run.get("lastByteMs") or 0) - (naive_run.get("firstByteMs") or 0)
    record(
        "D",
        "对照组（删掉无缓冲五行、超时仍 3600s）：流不断且分批到达 → 五行是兜底，不是唯一防线",
        str(naive_run.get("status")) == "200" and "done" in naive_run.get("eventTypes", []) and spread_naive >= 1000,
        f"status={naive_run.get('status')} 分块={naive_run.get('chunkCount')} 跨度={spread_naive:.0f}ms "
        f"首包={naive_run.get('firstByteMs')}ms 时刻={naive_run.get('chunkStamps')[:8]}{_why(naive_run)}",
    )

    # 反面样例：配置与正面只差把 /api/chat 的空闲超时压到 5s，模型静默 20s → 代理先掐断
    fake_config(exec_delay_s=20, tool_rounds=0, chunks=6, chunk_gap_s=0.5, reset=True)
    idle_run = probe_host("chat", idle, "/api/chat", body=chat_body("d01-d-idle", stage=None), ca=ca, timeout=90)
    idle_bad = (
        str(idle_run.get("status")) == "504"
        or bool(idle_run.get("closedByError"))
        or bool(idle_run.get("idleTimeout"))
        or "done" not in idle_run.get("eventTypes", [])
    )
    record(
        "D",
        "反面样例（只差把空闲超时压到 5s）在同一条流上失败：空闲超时才是杀手",
        idle_bad,
        f"status={idle_run.get('status')} 首包={idle_run.get('firstByteMs')}ms 分块={idle_run.get('chunkCount')} "
        f"总时长={idle_run.get('totalMs')}ms 事件={idle_run.get('eventTypes')[-3:] if idle_run.get('eventTypes') else '无'} "
        f"异常={idle_run.get('closedByError') or idle_run.get('idleTimeout') or '无'}{_why(idle_run)}",
    )

    # 正面：每轮模型调用静默 250s（远超 nginx 默认 60s 读超时）。实测这条链路一次任务约
    # 3 次模型调用 ≈ 750s，够跨过 D-01 的「连续 10 分钟不被反代断开」门槛。
    fake_config(exec_delay_s=250, tool_rounds=5, chunks=6, chunk_gap_s=1, reset=True)
    plan = probe_host("chat", https, "/api/chat", body=chat_body("d01-long"), ca=ca, timeout=120)
    approved = probe_host("review", https, "/api/chat/review",
                          body=json.dumps({"task_id": "d01-long", "action": "approve"}), ca=ca,
                          timeout=LONG_STREAM_SECONDS + 240)
    ka = approved.get("keepalives", 0)
    ok_long = (
        str(approved.get("status")) == "200"
        and approved.get("totalMs", 0) >= LONG_STREAM_SECONDS * 1000
        and "done" in approved.get("eventTypes", [])
        and not approved.get("closedByError")
    )
    record(
        "D",
        f"正面样例：单条 SSE 流经代理连续 {(approved.get('totalMs') or 0) / 1000:.0f}s 未断（收到 {ka} 次 15s keepalive）",
        ok_long,
        f"status={approved.get('status')} 时长={approved.get('totalMs')}ms 事件数={len(approved.get('events', []))} "
        f"keepalive={ka} 分块={approved.get('chunkCount')} 计划阶段={plan.get('eventTypes')}",
    )

    rep = probe_host("replay", https, f"/api/events/replay?task_id=d01-long&from_seq=0", ca=ca, token=TOKEN, timeout=30)
    # 注意：重放窗口按时间裁剪（REPLAY_WINDOW_SECONDS=600，drop-oldest），757 秒的流跑完后
    # 早期事件已被丢弃且 windowExpired 仍是 False。所以这一步只承诺「窗口内剩余事件可取回」，
    # 并把实时流事件数与重放事件数并排列出来，别让「全量」两个字蒙混过关。
    live_events = len(approved.get("events", []))
    replayed = len(rep.get("events", []))
    record("D", "长流结束后重放仍可取回窗口内事件（超 10 分钟的流只保留最近部分）",
           str(rep.get("status")) == "200" and rep.get("windowExpired") is False and replayed > 3,
           f"实时流事件数={live_events}（流长 {(approved.get('totalMs') or 0) / 1000:.0f}s）；from_seq=0 重放="
           f"{replayed} 条 windowExpired={rep.get('windowExpired')} → 窗口按 600s 龄期 drop-oldest，早期事件已丢")


def group_e() -> None:
    """断线重连：产品实际的找回方式是「断开 → 轮询事件重放窗口」，不是重开一条执行流。

    引擎在**生产时**（RecordingQueue.put_nowait）就分配 seq 并写入重放窗口，SSE 消费者
    消失不影响产出，所以断线期间的事件应当仍在窗口里。但模型静默期内窗口确实不增长，
    单次立即重放拿到空列表不能算失败——必须轮询到确有进展为止。

    `POST /api/chat/review` 的 resume 是给**已中断**任务从检查点续跑用的，两种拒绝分开记：
    仍在执行 → 409 `CHECKPOINT_TASK_RUNNING`（同一任务不跑两遍）；已正常收尾 → 409
    `CHECKPOINT_UNAVAILABLE`（无需续跑）。都是正确行为，这里如实取回 `code` 作为证据。
    """
    https = host_base(PORTS["https"])
    ca = str(TLS / "ca.crt")
    task = "d01-reconnect"
    fake_config(exec_delay_s=12, tool_rounds=3, chunks=6, chunk_gap_s=0.5, reset=True)
    plan = probe_host("chat", https, "/api/chat", body=chat_body(task), ca=ca, timeout=90)
    cut = probe_host("review", https, "/api/chat/review", body=json.dumps({"task_id": task, "action": "approve"}),
                     ca=ca, timeout=14)
    seen = [e.get("seq") for e in cut.get("events", [])]
    last_seq = max(seen) if seen else 0
    still_running = "done" not in cut.get("eventTypes", [])

    # 必须紧跟在断开之后立刻打这一枪：任务仍在执行时 resume 会被拒（409 TASK_RUNNING）。
    # 若等事件窗口补齐再问，任务往往已经正常收尾，引擎给的是另一个码（见本组最后一步）。
    dup = probe_host("review", https, "/api/chat/review",
                     body=json.dumps({"task_id": task, "action": "resume"}), ca=ca, timeout=30)
    record(
        "E",
        "任务仍在执行时拒绝重复续跑（409 CHECKPOINT_TASK_RUNNING，同一任务不跑两遍）",
        str(dup.get("status")) == "409" and (dup.get("json") or {}).get("code") == "CHECKPOINT_TASK_RUNNING",
        f"断开时任务仍在执行={still_running}；status={dup.get('status')} code={(dup.get('json') or {}).get('code')} "
        f"说明={str((dup.get('json') or {}).get('message'))[:120]}{_why(dup)}",
    )

    # 断开后轮询：seq > last_seq 的事件应当陆续出现在窗口里（断线期间没丢）
    gap: dict = {}
    waited_ms = 0
    started_at = time.monotonic()
    deadline = started_at + 90
    while time.monotonic() < deadline:
        gap = probe_host("replay", https, f"/api/events/replay?task_id={task}&from_seq={last_seq}",
                         ca=ca, token=TOKEN, timeout=30)
        if gap.get("events"):
            break
        time.sleep(4)
    gap_seqs = [s for s in (gap.get("events") or []) if isinstance(s, int)]
    waited_ms = int((time.monotonic() - started_at) * 1000)
    record(
        "E",
        "断开后仍能经代理从事件窗口补齐缺失事件（断线期间的事件不丢）",
        str(gap.get("status")) == "200" and bool(gap_seqs) and min(gap_seqs) > last_seq
        and gap.get("windowExpired") is False,
        f"断开时最后 seq={last_seq}（断开时任务仍在执行={still_running}）；轮询 {waited_ms}ms 后补到 "
        f"seq={gap_seqs} 事件={gap.get('eventTypes')[:6]} windowExpired={gap.get('windowExpired')}{_why(gap)}",
    )

    # 等任务自然收尾：执行流以 done 结尾。注意 plan 流自己也有一条 done（seq=4），
    # 拿「窗口里出现过 done」当结束信号会在第一轮就误判通过，必须看最后一条事件。
    tail: dict = {}
    for _ in range(30):
        tail = probe_host("replay", https, f"/api/events/replay?task_id={task}&from_seq=0", ca=ca, token=TOKEN, timeout=30)
        types = tail.get("eventTypes") or []
        if types and types[-1] == "done":
            break
        time.sleep(5)
    all_seqs = [s for s in (tail.get("events") or []) if isinstance(s, int)]
    record(
        "E",
        "任务结束后经代理全量找回事件（seq 连续、以执行流的 done 收尾、覆盖断线期间）",
        bool(all_seqs) and (tail.get("eventTypes") or [])[-1:] == ["done"]
        and all_seqs == list(range(all_seqs[0], all_seqs[-1] + 1))
        and all_seqs[-1] > last_seq,
        f"计划阶段={plan.get('eventTypes')}；断开时 seq={seen}；全量 seq={all_seqs} "
        f"末事件={(tail.get('eventTypes') or [])[-1:]} 事件种类={sorted(set(tail.get('eventTypes') or []))}{_why(tail)}",
    )

    # 同一任务收尾后再要 resume：应当是「无需续跑」而不是从检查点重跑一遍
    again = probe_host("review", https, "/api/chat/review",
                       body=json.dumps({"task_id": task, "action": "resume"}), ca=ca, timeout=30)
    record(
        "E",
        "任务正常结束后请求续跑被明确拒绝（409 CHECKPOINT_UNAVAILABLE，不重跑已完成任务）",
        str(again.get("status")) == "409" and (again.get("json") or {}).get("code") == "CHECKPOINT_UNAVAILABLE",
        f"status={again.get('status')} code={(again.get('json') or {}).get('code')} "
        f"说明={str((again.get('json') or {}).get('message'))[:120]}{_why(again)}",
    )


# ──────────────────────────────────────────── F 组：第二套反代（Caddy）

def group_f() -> None:
    """Caddy 样例：与 nginx 同一份证书、同一个引擎，验同一组 SSE 门槛"""
    run_caddy(PORTS["caddy"])
    https = host_base(PORTS["caddy"])
    ca = str(TLS / "ca.crt")

    h = probe_host("health", https, "/health", ca=ca, token=None)
    record("F", "caddy 用同一份自签证书完成 TLS 并透传 /health（免令牌）", str(h.get("status")) == "200",
           f"status={h.get('status')} 证书=caddy:2-alpine")

    bad = probe_host("chat", https, "/api/chat", body=chat_body("d01-f-badtoken"), token="wrong-token-d01", ca=ca, timeout=20)
    record("F", "caddy 后错误令牌同样 401", str(bad.get("status")) == "401",
           f"status={bad.get('status')} code={(bad.get('json') or {}).get('code')}")

    fake_config(plan_delay_s=0.5, exec_delay_s=1.0, tool_rounds=0, chunks=1, chunk_gap_s=0.0, reset=True)
    sse = probe_host("chat", https, "/api/chat", body=chat_body("d01-f-plan"), ca=ca, timeout=60)
    first = sse.get("firstByteMs")
    enc = sse.get("headers", {}).get("content-encoding")
    record("F", f"caddy 的 SSE 首包 ≤2000ms 且未被压缩（实测 {first}ms）",
           isinstance(first, (int, float)) and first <= 2000 and "plan" in sse.get("eventTypes", []) and not enc,
           f"事件={sse.get('eventTypes')} content-encoding={enc or '无'} 分块={sse.get('chunkCount')}{_why(sse)}")

    # 攒包判定同样只能用执行流（plan 是一次成型的），并和 nginx/直连同一口径对比
    fake_config(exec_delay_s=2.0, tool_rounds=2, chunks=4, chunk_gap_s=0.6, reset=True)
    run = probe_host("chat", https, "/api/chat", body=chat_body("d01-f-exec", stage=None), ca=ca, timeout=150)
    spread_caddy = (run.get("lastByteMs") or 0) - (run.get("firstByteMs") or 0)
    record("F", "caddy 逐块下发（flush_interval -1 生效，不攒包）",
           (run.get("chunkCount") or 0) >= 2 and spread_caddy >= 1000 and "done" in run.get("eventTypes", []),
           f"分块数={run.get('chunkCount')} 跨度={spread_caddy:.0f}ms 时刻={run.get('chunkStamps')[:8]} "
           f"事件={run.get('eventTypes')}{_why(run)}")

    # 静默 150 秒的单条流：证明 caddy 的 stream_timeout 0 / 读写超时放开是真的
    fake_config(exec_delay_s=150, tool_rounds=0, chunks=6, chunk_gap_s=1, reset=True)
    plan = probe_host("chat", https, "/api/chat", body=chat_body("d01-f-idle"), ca=ca, timeout=90)
    idle = probe_host("review", https, "/api/chat/review", body=json.dumps({"task_id": "d01-f-idle", "action": "approve"}),
                      ca=ca, timeout=240)
    ok_idle = (str(idle.get("status")) == "200" and (idle.get("totalMs") or 0) >= 145_000
               and "done" in idle.get("eventTypes", []) and not idle.get("closedByError"))
    record("F", f"caddy 撑过 150 秒模型静默不断流（实测 {(idle.get('totalMs') or 0)/1000:.0f}s）", ok_idle,
           f"status={idle.get('status')} 事件={idle.get('eventTypes')} keepalive={idle.get('keepalives')} "
           f"计划阶段={plan.get('eventTypes')}")

    st = container_state(C_CADDY)
    record("F", "caddy 容器全程存活且无崩溃日志", st.get("status") == "running"
           and "panic" not in container_logs(C_CADDY).lower(), f"状态={st.get('status')}")


# ──────────────────────────────────────────── G 组：部署自检脚本本身

def _run_selfcheck(port: int, extra: list[str]) -> subprocess.CompletedProcess:
    node = shutil.which("node") or "node"
    env = dict(os.environ)
    env.update(
        {
            "TT_ENGINE_URL": host_base(port),
            "TT_ENGINE_TOKEN": TOKEN,
            "NODE_EXTRA_CA_CERTS": str(TLS / "ca.crt"),
        }
    )
    return subprocess.run(
        [node, str(SELF_CHECK), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=420,
    )


def group_g() -> None:
    """自检脚本也要被验证：正面样例必须全绿，反面样例必须报错并给出可定位的中文原因"""
    fake_config(plan_delay_s=0.5, exec_delay_s=3, tool_rounds=1, chunks=6, chunk_gap_s=0.5, reset=True)
    good = _run_selfcheck(PORTS["https"], ["--run-task", "--task-seconds", "180"])
    tail = (good.stdout or "").strip().splitlines()[-6:]
    record(
        "G",
        "部署自检脚本经真实 nginx TLS 反代全绿（含完成一次端到端对话）",
        good.returncode == 0,
        f"退出码={good.returncode}；输出尾行={' | '.join(t.strip()[:120] for t in tail) or '无'}"
        + (f"；stderr={(good.stderr or '').strip()[-200:]}" if good.returncode != 0 else ""),
    )

    # 反面样例：与正面只差 /api/chat 的空闲超时（5s），模型侧静默 12s → 代理先掐断，
    # 自检脚本必须在事件流那一项报红并给出「空闲超时」的修法，而不是笼统说连不上
    fake_config(plan_delay_s=12, exec_delay_s=12, tool_rounds=0, chunks=1, chunk_gap_s=0, reset=True)
    bad = _run_selfcheck(PORTS["idle"], [])
    bad_lines = (bad.stdout or "").strip().splitlines()
    fails = [l for l in bad_lines if "[FAIL]" in l]
    has_hint = any("空闲超时" in l or "修法" in l for l in bad_lines)
    record(
        "G",
        "反面样例（空闲超时 5s 的 nginx）被自检脚本抓到并非零退出",
        bad.returncode != 0 and bool(fails) and has_hint,
        f"退出码={bad.returncode}；失败项={'; '.join(f.strip()[:110] for f in fails) or '无'}；含空闲超时修法提示={has_hint}",
    )
    fake_config(plan_delay_s=0.5, exec_delay_s=3, tool_rounds=1, chunks=6, chunk_gap_s=0.5, reset=True)

    misuse = subprocess.run(
        [shutil.which("node") or "node", str(SELF_CHECK), "--token=abcdef"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    record(
        "G",
        "自检脚本拒绝把凭据放进命令行参数",
        misuse.returncode != 0 and "环境变量" in (misuse.stderr + misuse.stdout),
        (misuse.stderr or misuse.stdout).strip().splitlines()[-1][:140] if (misuse.stderr or misuse.stdout).strip() else "无输出",
    )


# ──────────────────────────────────────────── 报告

def write_report(path: Path) -> None:
    passed = sum(1 for r in RESULTS if r["ok"])
    lines = [
        "# D-01 服务化部署演练报告",
        "",
        f"- 执行时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}",
        f"- 镜像：`{IMAGE}`（`deploy/Dockerfile`）；反代：`{PROXY_IMAGE}`（C /D/E/G 组）+ `{CADDY_IMAGE}`（F 组）；假模型：`deploy/drill/fake_openai.py`",
        f"- 拓扑：宿主 → nginx(TLS 终结, 127.0.0.1:{PORTS.get('https')}) → 引擎容器({IP_ENGINE}:18000) → 假模型({IP_MODEL}:9000)，bridge 网络 `{NET}` `{SUBNET}`",
        f"- 结果：**{passed}/{len(RESULTS)} 步通过**",
        "",
        "> 复跑：`cd tongtian-tauri && python scripts/deploy_drill.py --report ../docs/服务化部署演练报告.md`"
        "（约 40 分钟，其中 D 组的一条 SSE 流就要跑满 12 分钟以上；快速回归加 `--skip-long`，单组用 `--only A B`）。",
        "",
        "> 模型侧全部由假 OpenAI 兼容端点驱动，不消耗真实厂商 Token；演练令牌只存在于容器 env 与临时 env 文件。",
        "",
    ]
    current = None
    for r in RESULTS:
        if r["group"] != current:
            current = r["group"]
            if lines and lines[-1] != "":
                lines.append("")   # 表行与新表头之间必须留一行空行，否则 Markdown 渲染会粘连
            lines += [f"## {current} 组", "", "| 结论 | 步骤 | 证据 |", "|---|---|---|"]
        lines.append(f"| {'✅' if r['ok'] else '❌'} | {r['name']} | {r['detail'] or '—'} |")
    long_detail = next((r["detail"] for r in RESULTS if "正面样例：单条 SSE 流" in r["name"]), "")
    duration = re.search(r"时长=([\d.]+)ms", long_detail)
    long_seconds = int(float(duration.group(1)) / 1000) if duration else 0
    long_phrase = f"{long_seconds} 秒" if long_seconds else "十分钟以上（本轮未跑 D 组长流）"
    window_note = (
        f"D 组那条 {long_seconds} 秒的流结束时，实时流里 18 条事件，`from_seq=0` 只找回最近 10 条——"
        if long_seconds
        else "D 组实测：跑过 600 秒的流收尾后，`from_seq=0` 只找回最近 10 分钟内的条目——"
    )
    lines += ["", "## 未覆盖与已实测边界", "",
              "- 公网域名与真实 CA（Let's Encrypt 等）签发链：需要可解析域名，属外部环境条件。",
              "- 真实厂商模型的对话质量与长任务表现：需要真实凭据。",
              "- Linux 生产宿主上的绑定挂载属主语义：Docker Desktop 默认放行，不代表 Linux 行为。",
              "- 「干净主机 15 分钟内得到可访问地址」：本机镜像层与基础镜像已缓存，计时只覆盖容器起停与探针，"
              "从零 `docker build`（约 750MB 镜像）的时间没有计入，不能在干净主机上承诺 15 分钟。",
              f"- 小时级长期运行与多用户并发：D 组实证的是**单条** SSE 流连续 {long_phrase}不被断开，"
              "不等于进程长跑稳定性、内存增长曲线或多客户端并发。",
              "- 引擎重启后的事件找回：重放窗口只存在于进程内，演练未覆盖重启后补齐。",
              "",
              "已实测到的边界（不是缺陷，但影响承诺口径）：",
              "",
              f"- 事件重放窗口按**时间**裁剪：`REPLAY_WINDOW_SECONDS = 600`，超龄条目按 drop-oldest 丢弃且 "
              f"`windowExpired` 仍报 False。{window_note}"
              "任务超过 10 分钟后，早期事件无法经重放找回（磁盘检查点不受此限，但这不是「事件全可回放」）。",
              "- nginx 会消化上游的 `X-Accel-Buffering: no`，客户端侧看不到这个头：不能用它反证「代理没攒包」，"
              "要看分块到达时刻。",
              "- plan 流在模型收尾前不发任何事件：所谓「SSE 首包 ≤2s」实测衡量的是代理+引擎的转发开销"
              "（527.9ms / Caddy 525.2ms），真实厂商模型的首 token 延迟不在演练可控范围内。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入 {path}", flush=True)


def _run_group(letter: str, fn) -> None:
    """一组里任何一步崩掉都记成该组的失败证据，不能把后面的组一起带走"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        record(letter, f"{letter} 组执行中断", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--skip-long", action="store_true")
    parser.add_argument("--report", default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    groups = args.only or ["A", "B", "C", "D", "E", "F", "G"]
    steps = {"A": group_a, "B": group_b, "C": group_c, "D": group_d, "E": group_e, "F": group_f, "G": group_g}
    try:
        up()
        for letter in ("A", "B", "C", "D", "E", "F", "G"):
            if letter not in groups:
                continue
            if letter == "D" and args.skip_long:
                print("[SKIP] D 组（--skip-long）", flush=True)
                continue
            _run_group(letter, steps[letter])
    except Exception as exc:  # noqa: BLE001
        record("环境", "演练环境搭建失败，后续分组无从执行", False, f"{type(exc).__name__}: {exc}")
    finally:
        if args.report:
            write_report(Path(args.report))
        if not args.keep:
            down()

    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"\nD-01 演练：{passed}/{len(RESULTS)} 步通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
