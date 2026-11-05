#!/usr/bin/env bash
# 跑 src-tauri 的 Rust 单元测试（Windows / MSVC）
#
# 为什么不能直接 `cargo test --lib`：
# tauri / wry 静态导入了只有 comctl32 v6 才有的函数（TaskDialogIndirect、
# SetWindowSubclass、DefSubclassProc）。正式 exe 由 tauri_build 嵌了
# Common-Controls 清单依赖，测试 exe 没嵌 —— Windows 装载器把 comctl32 绑到
# System32 里的 v5，进程在跑出第一条断言之前就 STATUS_ENTRYPOINT_NOT_FOUND
# （0xc0000139）退出，整条 Rust 单测一条都跑不了（实测：连改动前就存在的
# collab::tests 也一样起不来）。
#
# build.rs 里的 cargo:rustc-link-arg-tests 到不了 lib 单元测试这条链接命令
# （已用 `cargo build --lib --tests -v` 实测：那条 rustc 调用里没有 -C link-arg），
# 所以在这里给「测试构建」单独补同一条清单依赖。
# 用独立 target 目录，正式打包（target/debug、target/release）不受这些 RUSTFLAGS 影响。
#
# 用法：
#   scripts/run-rust-tests.sh                     # 全量单元测试
#   scripts/run-rust-tests.sh collab::            # 只跑协作相关
set -euo pipefail

cd "$(dirname "$0")/../src-tauri"

if [[ "$(uname -s)" =~ MINGW|MSYS ]]; then
  MANIFEST=$(cygpath -w "$PWD/test-common-controls.manifest")
  export RUSTFLAGS="${RUSTFLAGS:-} -C link-arg=/MANIFESTINPUT:$MANIFEST -C link-arg=/MANIFEST:EMBED"
  export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-target/rust-tests}"
fi

exec cargo test --lib "$@"
