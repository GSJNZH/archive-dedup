#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py -- 用临时目录验证 archive_dedup_cli.py 的核心行为。

覆盖:
  * 快速/极速/精确三种比对模式
  * 试运行不落盘
  * 复制（保留目录结构和修改时间）
  * 同盘移动
  * 输出目录位于原素材内部时自动排除
  * 输出目录非空时的保护

运行: python selftest.py
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading

import archive_dedup

ROOT = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(ROOT, "archive_dedup_cli.py")
PY = sys.executable
TMP_ROOT = os.path.join(ROOT, "_selftest_tmp")
MTIME_NS = 1_600_000_000 * 10**9

FAILURES = []
PASSES = 0
CASE_COUNTER = 0


def check(name, condition, detail=""):
    global PASSES
    if condition:
        PASSES += 1
        print("  PASS  " + name)
    else:
        FAILURES.append(name + ((" | " + detail) if detail else ""))
        print("  FAIL  " + name + ((" | " + detail) if detail else ""))


def write_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def new_case_dir():
    """创建用例目录。

    不用 tempfile.mkdtemp：它用 mode=0o700 建目录，在受限令牌/沙箱下
    后续写入会被拒绝。这里用默认 mode（0o777）。
    """
    global CASE_COUNTER
    CASE_COUNTER += 1
    path = os.path.join(TMP_ROOT, "case_%02d" % CASE_COUNTER)
    os.makedirs(path, exist_ok=True)
    return path


def run_cli(*args):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [PY, CLI] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        payload = None
    return proc.returncode, payload, proc.stdout, proc.stderr


def build_tree(base):
    archive = os.path.join(base, "归档")
    source = os.path.join(base, "原素材")
    out = os.path.join(base, "未归档补充")

    write_file(os.path.join(archive, "a.bin"), b"A" * 4096)
    write_file(os.path.join(archive, "nested", "keep.bin"), b"K" * 1000)

    os.makedirs(os.path.join(source, "sub"), exist_ok=True)
    shutil.copy2(os.path.join(archive, "a.bin"), os.path.join(source, "a.bin"))
    shutil.copy2(os.path.join(archive, "a.bin"), os.path.join(source, "sub", "dup_a.bin"))
    write_file(os.path.join(source, "uniq.bin"), b"U" * 300000)
    os.utime(os.path.join(source, "uniq.bin"), ns=(MTIME_NS, MTIME_NS))
    write_file(os.path.join(source, "sub", "uniq2.bin"), b"V" * 5000)
    write_file(os.path.join(source, "empty.bin"), b"")
    return archive, source, out


def test_dry_run(archive, source, out):
    rc, data, stdout, stderr = run_cli(archive, source, out, "--dry-run", "--json")
    check("dry-run 退出码为 0", rc == 0, stderr[-400:])
    check("dry-run 返回 JSON", data is not None, stdout[:200])
    if not data:
        return
    check("dry-run 已归档=2", data.get("archived") == 2, repr(data))
    check("dry-run 未归档=3", data.get("unique") == 3, repr(data))
    check("dry-run 未处理文件", data.get("copied") == 0, repr(data))
    check("dry-run 标记正确", data.get("dry_run") is True, repr(data))
    check("dry-run 不创建输出目录", not os.path.exists(out))
    check("同盘提示出现", "同盘" in stderr, stderr[-300:])


def test_modes(archive, source, out):
    for mode in ("turbo", "exact"):
        rc, data, stdout, stderr = run_cli(
            archive, source, out, "--mode", mode, "--dry-run", "--json")
        check("%s 退出码为 0" % mode, rc == 0, stderr[-400:])
        if data:
            check("%s 已归档=2" % mode, data.get("archived") == 2, repr(data))
            check("%s 未归档=3" % mode, data.get("unique") == 3, repr(data))


def test_copy(archive, source, out):
    rc, data, stdout, stderr = run_cli(
        archive, source, out, "--mode", "fast", "--workers", "2", "--json")
    check("复制退出码为 0", rc == 0, stderr[-400:])
    if data:
        check("复制 archived=2", data.get("archived") == 2, repr(data))
        check("复制 unique=3", data.get("unique") == 3, repr(data))
        check("复制 copied=3", data.get("copied") == 3, repr(data))
    check("输出 uniq.bin 存在", os.path.isfile(os.path.join(out, "uniq.bin")))
    check("输出 sub/uniq2.bin 存在", os.path.isfile(os.path.join(out, "sub", "uniq2.bin")))
    check("输出 empty.bin 存在", os.path.isfile(os.path.join(out, "empty.bin")))
    check("空文件大小正确", os.path.getsize(os.path.join(out, "empty.bin")) == 0)
    check("输出 a.bin 不存在（已归档）", not os.path.exists(os.path.join(out, "a.bin")))
    check("原素材 uniq.bin 仍在", os.path.isfile(os.path.join(source, "uniq.bin")))
    check("复制保留修改时间",
          os.stat(os.path.join(out, "uniq.bin")).st_mtime_ns == MTIME_NS,
          str(os.stat(os.path.join(out, "uniq.bin")).st_mtime_ns))

    rc, data, stdout, stderr = run_cli(archive, source, out, "--json")
    check("输出目录非空时拒绝", rc == 2, "rc=%d" % rc)
    check("非空拒绝返回 ok=false", bool(data) and data.get("ok") is False, repr(data))

    parts = []
    for dirpath, dirnames, filenames in os.walk(out):
        parts += [os.path.join(dirpath, name) for name in filenames
                  if name.endswith(".part")]
    check("复制后没有 .part 残留", not parts, repr(parts[:3]))


def test_move(base):
    archive, source, out = build_tree(base)
    rc, data, stdout, stderr = run_cli(
        archive, source, out, "--copy-mode", "move", "--yes", "--json")
    check("移动退出码为 0", rc == 0, stderr[-400:])
    if data:
        check("移动 copied=3", data.get("copied") == 3, repr(data))
        check("移动 copy_mode=move", data.get("copy_mode") == "move", repr(data))
    check("移动后原素材 uniq.bin 消失", not os.path.exists(os.path.join(source, "uniq.bin")))
    check("移动后原素材 a.bin 保留", os.path.isfile(os.path.join(source, "a.bin")))
    check("移动后输出 uniq.bin 存在", os.path.isfile(os.path.join(out, "uniq.bin")))


def test_output_inside_source(base):
    archive, source, _ = build_tree(base)
    out = os.path.join(source, "_out")
    write_file(os.path.join(out, "stale.bin"), b"S" * 100)
    rc, data, stdout, stderr = run_cli(
        archive, source, out, "--dry-run", "--json")
    check("输出在素材内 dry-run 退出码为 0", rc == 0, stderr[-400:])
    if data:
        check("输出目录被排除", data.get("unique") == 3, repr(data))


def test_cancel_payload(base):
    """worker 收到取消时必须回传 cancelled=True，GUI 才不会卡在运行中。"""
    archive, source, out = build_tree(base)
    msg_queue = queue.Queue()
    stop_event = threading.Event()
    stop_event.set()
    archive_dedup.worker(archive, source, out, True, False,
                         archive_dedup.MODE_FAST, archive_dedup.COPY_MODE_COPY,
                         1, msg_queue, stop_event)
    done = None
    while True:
        try:
            kind, payload = msg_queue.get_nowait()
        except queue.Empty:
            break
        if kind == "done":
            done = payload
    check("取消回传 cancelled=True", bool(done) and done.get("cancelled") is True, repr(done))
    check("取消不复制文件", not os.path.exists(out))


def drain_done(msg_queue):
    done = None
    error = None
    while True:
        try:
            kind, payload = msg_queue.get_nowait()
        except queue.Empty:
            break
        if kind == "done":
            done = payload
        elif kind == "error":
            error = payload
    return done, error


def test_path_guards(base):
    archive, source, out = build_tree(base)
    cases = [
        ("输出在归档内部", os.path.join(archive, "sub")),
        ("输出等于原素材", source),
        ("输出包含归档", base),
    ]
    for name, target in cases:
        rc, data, stdout, stderr = run_cli(
            archive, source, target, "--dry-run", "--json")
        check(name + " 被拒绝", rc == 2, "rc=%d %s" % (rc, stderr[-200:]))
        check(name + " 返回 ok=false", bool(data) and data.get("ok") is False, repr(data))


def test_atomic_copy(base):
    src = os.path.join(base, "src.bin")
    dst = os.path.join(base, "out", "dst.bin")
    write_file(src, b"Z" * 1000)
    os.utime(src, ns=(MTIME_NS, MTIME_NS))

    archive_dedup.copy_one(src, dst)
    check("原子复制内容正确",
          open(dst, "rb").read() == b"Z" * 1000)
    check("原子复制无 .part 残留", not os.path.exists(dst + ".part"))
    check("原子复制保留 mtime", os.stat(dst).st_mtime_ns == MTIME_NS)

    write_file(dst, b"old")
    archive_dedup.copy_one(src, dst)
    check("原子复制覆盖旧文件", open(dst, "rb").read() == b"Z" * 1000)
    check("覆盖后仍无 .part", not os.path.exists(dst + ".part"))


def test_partial_failure(base):
    archive, source, out = build_tree(base)
    original = archive_dedup.copy_one

    def flaky(src, dst):
        if os.path.basename(src) == "uniq.bin":
            raise OSError("simulated failure")
        return original(src, dst)

    archive_dedup.copy_one = flaky
    try:
        msg_queue = queue.Queue()
        stop_event = threading.Event()
        archive_dedup.worker(archive, source, out, False, False,
                             archive_dedup.MODE_FAST, archive_dedup.COPY_MODE_COPY,
                             1, msg_queue, stop_event)
    finally:
        archive_dedup.copy_one = original

    done, error = drain_done(msg_queue)
    check("部分失败不返回 error", error is None, repr(error))
    check("部分失败 failed=1", bool(done) and done.get("failed") == 1, repr(done))
    check("部分失败 copied=2", bool(done) and done.get("copied") == 2, repr(done))
    check("部分失败 unique=3", bool(done) and done.get("unique") == 3, repr(done))


def test_disk_preflight(base):
    archive, source, out = build_tree(base)

    original_free = archive_dedup.disk_free
    archive_dedup.disk_free = lambda path: 100 * 1024 * 1024
    try:
        ok, _ = archive_dedup.check_disk_space("x", 10)
        check("空间充足判定", ok)
        ok2, reason2 = archive_dedup.check_disk_space("x", 200 * 1024 * 1024)
        check("空间不足判定", (not ok2) and "空间不足" in reason2, repr(reason2))
    finally:
        archive_dedup.disk_free = original_free

    original = archive_dedup.check_disk_space
    archive_dedup.check_disk_space = lambda path, need: (False, "输出磁盘空间不足: 测试")
    try:
        msg_queue = queue.Queue()
        stop_event = threading.Event()
        archive_dedup.worker(archive, source, out, False, False,
                             archive_dedup.MODE_FAST, archive_dedup.COPY_MODE_COPY,
                             1, msg_queue, stop_event)
    finally:
        archive_dedup.check_disk_space = original

    done, error = drain_done(msg_queue)
    check("空间不足返回 error", bool(error) and "空间不足" in error, repr(error))
    check("空间不足不创建输出", not os.path.exists(out))
    check("空间不足不返回 done", done is None, repr(done))


def test_path_helpers():
    check("is_inside 相等", archive_dedup.is_inside(r"C:\a", r"C:\a"))
    check("is_inside 子目录", archive_dedup.is_inside(r"C:\a\b", r"C:\a"))
    check("is_inside 前缀不误判", not archive_dedup.is_inside(r"C:\ab", r"C:\a"))
    check("is_inside 盘根", archive_dedup.is_inside(r"C:\x", "C:\\"))
    check("is_inside 反向不成立", not archive_dedup.is_inside(r"C:\a", r"C:\a\b"))
    check("same_path 忽略大小写", archive_dedup.same_path(r"C:\a", r"c:\A"))


def test_verify_ok(base):
    archive, source, out = build_tree(base)
    rc, data, stdout, stderr = run_cli(archive, source, out, "--json")
    check("复查准备复制成功", rc == 0, stderr[-300:])

    rc, data, stdout, stderr = run_cli(archive, source, out, "--verify", "--json")
    check("复查通过退出码 0", rc == 0, stderr[-300:])
    if data:
        check("复查 ok=true", data.get("ok") is True, repr(data))
        check("复查 unused=3", data.get("unused") == 3, repr(data))
        check("复查 verified=3", data.get("verified") == 3, repr(data))
        check("复查无缺失/不一致/多余/part",
              data.get("missing") == 0 and data.get("mismatch") == 0
              and data.get("extra") == 0 and data.get("part") == 0, repr(data))
        report = data.get("report") or ""
        check("复查报告已生成", bool(report) and os.path.isfile(report), repr(report))
        if report and os.path.isfile(report):
            with open(report, "r", encoding="utf-8-sig") as handle:
                text = handle.read()
            check("报告含项目名称", "项目名称:" in text)
            check("报告含未归档统计", "未归档(应有): 3" in text)
            check("报告含文件指纹", "fp=" in text)
            check("报告含集合指纹", "输出集合指纹" in text)

    rc, data, stdout, stderr = run_cli(
        archive, source, out, "--verify", "--mode", "exact", "--json")
    check("精确复查通过", rc == 0 and bool(data) and data.get("ok") is True,
          stderr[-300:])

    os.remove(os.path.join(out, "uniq.bin"))
    rc, data, stdout, stderr = run_cli(archive, source, out, "--verify", "--json")
    check("缺失时退出码 5", rc == 5, "rc=%d" % rc)
    check("缺失时 missing=1", bool(data) and data.get("missing") == 1, repr(data))
    check("缺失时 ok=false", bool(data) and data.get("ok") is False, repr(data))


def test_verify_mismatch_extra(base):
    archive, source, out = build_tree(base)
    rc, data, stdout, stderr = run_cli(archive, source, out, "--json")
    check("复查不一致准备复制成功", rc == 0, stderr[-300:])

    target = os.path.join(out, "sub", "uniq2.bin")
    with open(target, "rb") as handle:
        content = handle.read()
    write_file(target, b"X" + content[1:])

    rc, data, stdout, stderr = run_cli(archive, source, out, "--verify", "--json")
    check("内容不一致退出码 5", rc == 5, "rc=%d" % rc)
    check("内容不一致 mismatch=1", bool(data) and data.get("mismatch") == 1, repr(data))

    write_file(os.path.join(out, "extra.bin"), b"E")
    rc, data, stdout, stderr = run_cli(archive, source, out, "--verify", "--json")
    check("多余文件退出码 5", rc == 5, "rc=%d" % rc)
    check("多余文件 extra=1", bool(data) and data.get("extra") == 1, repr(data))

    write_file(os.path.join(out, "left.part"), b"P")
    rc, data, stdout, stderr = run_cli(archive, source, out, "--verify", "--json")
    check(".part 退出码 5", rc == 5, "rc=%d" % rc)
    check(".part part=1", bool(data) and data.get("part") == 1, repr(data))


def test_verify_guards(base):
    archive, source, out = build_tree(base)
    out_file = os.path.join(base, "not_a_dir")
    write_file(out_file, b"x")
    rc, data, stdout, stderr = run_cli(
        archive, source, out_file, "--verify", "--json")
    check("复查输出不能是文件", rc == 2, "rc=%d" % rc)

    rc, data, stdout, stderr = run_cli(
        archive, source, out, "--verify", "--dry-run", "--json")
    check("复查不能配试运行", rc == 2, "rc=%d" % rc)


def manual_copy_unused(archive, source, out):
    keys = set()
    for p in archive_dedup.list_files(archive):
        try:
            keys.add(archive_dedup.make_key(p, archive_dedup.MODE_FAST))
        except OSError:
            pass
    copied = 0
    for p in archive_dedup.list_files(source):
        try:
            key = archive_dedup.make_key(p, archive_dedup.MODE_FAST)
        except OSError:
            continue
        if key in keys:
            continue
        rel = os.path.relpath(p, source)
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(p, dst)
        copied += 1
    return copied


def test_verify_without_run(base):
    """没有跑过复制/移动，也能对任意输出目录做复查。"""
    archive, source, out = build_tree(base)
    os.makedirs(out)
    copied = manual_copy_unused(archive, source, out)
    check("手动准备 3 个未归档文件", copied == 3, "copied=%d" % copied)
    check("手动复制不产生清单",
          not os.path.exists(os.path.join(out, archive_dedup.MANIFEST_NAME)))

    rc, data, stdout, stderr = run_cli(archive, source, out, "--verify", "--json")
    check("未运行复制也能复查通过", rc == 0 and bool(data) and data.get("ok") is True,
          stderr[-300:])
    check("未运行复查 unused=3", bool(data) and data.get("unused") == 3, repr(data))

    empty_out = os.path.join(base, "empty_out")
    os.makedirs(empty_out)
    rc, data, stdout, stderr = run_cli(
        archive, source, empty_out, "--verify", "--json")
    check("空目录复查退出码 5", rc == 5, "rc=%d" % rc)
    check("空目录 missing=3", bool(data) and data.get("missing") == 3, repr(data))

    missing_out = os.path.join(base, "missing_out")
    rc, data, stdout, stderr = run_cli(
        archive, source, missing_out, "--verify", "--json")
    check("不存在的输出目录退出码 5", rc == 5, "rc=%d" % rc)
    check("不存在的输出目录 missing=3",
          bool(data) and data.get("missing") == 3, repr(data))
    check("复查不会创建输出目录", not os.path.exists(missing_out))


def test_verify_read_error(base):
    archive, source, out = build_tree(base)
    rc, data, stdout, stderr = run_cli(archive, source, out, "--json")
    check("复查读取失败准备复制成功", rc == 0, stderr[-300:])

    original = archive_dedup.make_key
    source_abs = os.path.abspath(source)

    def flaky(path, mode):
        if (os.path.abspath(path).startswith(source_abs)
                and os.path.basename(path) == "empty.bin"):
            raise OSError("simulated read error")
        return original(path, mode)

    archive_dedup.make_key = flaky
    try:
        msg_queue = queue.Queue()
        stop_event = threading.Event()
        archive_dedup.verify_worker(archive, source, out, archive_dedup.MODE_FAST,
                                    False, msg_queue, stop_event)
    finally:
        archive_dedup.make_key = original

    done, error = drain_done(msg_queue)
    check("读取失败返回 done", done is not None, repr(error))
    check("读取失败 errors=1", bool(done) and done.get("errors") == 1, repr(done))
    check("读取失败 ok=false", bool(done) and done.get("ok") is False, repr(done))


def test_verify_move(base):
    archive, source, out = build_tree(base)
    rc, data, stdout, stderr = run_cli(
        archive, source, out, "--copy-mode", "move", "--yes", "--json")
    check("复查移动准备成功", rc == 0, stderr[-300:])

    rc, data, stdout, stderr = run_cli(archive, source, out, "--verify", "--json")
    check("移动后复查通过", rc == 0 and bool(data) and data.get("ok") is True,
          stderr[-300:])
    check("移动后 unused=3", bool(data) and data.get("unused") == 3, repr(data))
    check("移动后 verified=3", bool(data) and data.get("verified") == 3, repr(data))
    report = (data or {}).get("report") or ""
    if report and os.path.isfile(report):
        with open(report, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
        check("移动报告源文件数正确", "源文件: 5 个" in text)
        check("移动报告处理方式正确", "同盘移动" in text)

    manifest = os.path.join(out, archive_dedup.MANIFEST_NAME)
    check("复查清单已生成", os.path.isfile(manifest))
    if os.path.isfile(manifest):
        os.remove(manifest)
    rc, data, stdout, stderr = run_cli(archive, source, out, "--verify", "--json")
    check("无清单移动复查失败", rc == 5 and bool(data) and data.get("ok") is False,
          "rc=%d" % rc)
    check("无清单标记 no_manifest=1",
          bool(data) and data.get("no_manifest") == 1, repr(data))


def test_output_inside_source_copy(base):
    """默认输出目录就在原素材下面，复制后复查也必须正确排除输出目录。"""
    archive, source, _ = build_tree(base)
    out = os.path.join(source, archive_dedup.DEFAULT_OUTPUT_NAME)

    rc, data, stdout, stderr = run_cli(archive, source, out, "--json")
    check("输出在素材内复制成功", rc == 0, stderr[-300:])
    check("输出在素材内 copied=3", bool(data) and data.get("copied") == 3, repr(data))
    check("输出在素材内生成清单",
          os.path.isfile(os.path.join(out, archive_dedup.MANIFEST_NAME)))

    rc, data, stdout, stderr = run_cli(archive, source, out, "--verify", "--json")
    check("输出在素材内复查通过",
          rc == 0 and bool(data) and data.get("ok") is True, stderr[-300:])
    check("输出在素材内报告生成",
          bool(data) and os.path.isfile(data.get("report") or ""), repr(data))


def test_report_fingerprint():
    head = "a" * 32
    k1 = (100, head, "b" * 32)
    k2 = (100, head, "c" * 32)
    f1 = archive_dedup.report_fingerprint(k1, archive_dedup.MODE_FAST)
    f2 = archive_dedup.report_fingerprint(k2, archive_dedup.MODE_FAST)
    check("头相同尾不同时指纹不同", f1 != f2, f1 + " / " + f2)
    check("快速指纹带 fp= 前缀", f1.startswith("fp="), f1)


def main():
    if not os.path.isfile(CLI):
        print("找不到 " + CLI)
        return 1
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    os.makedirs(TMP_ROOT, exist_ok=True)

    try:
        print("[1] 试运行（快速模式）")
        test_dry_run(*build_tree(new_case_dir()))

        print("[2] 极速 / 精确模式")
        test_modes(*build_tree(new_case_dir()))

        print("[3] 复制")
        test_copy(*build_tree(new_case_dir()))

        print("[4] 同盘移动")
        test_move(new_case_dir())

        print("[5] 输出目录位于原素材内部")
        test_output_inside_source(new_case_dir())

        print("[6] 取消回传")
        test_cancel_payload(new_case_dir())

        print("[7] 路径守卫")
        test_path_guards(new_case_dir())

        print("[8] 原子复制")
        test_atomic_copy(new_case_dir())

        print("[9] 部分失败统计")
        test_partial_failure(new_case_dir())

        print("[10] 空间预检")
        test_disk_preflight(new_case_dir())

        print("[11] 路径判断")
        test_path_helpers()

        print("[12] 复查通过 / 缺失")
        test_verify_ok(new_case_dir())

        print("[13] 复查不一致 / 多余 / .part")
        test_verify_mismatch_extra(new_case_dir())

        print("[14] 复查守卫")
        test_verify_guards(new_case_dir())

        print("[15] 复查读取失败")
        test_verify_read_error(new_case_dir())

        print("[16] 移动后复查 / 清单")
        test_verify_move(new_case_dir())

        print("[17] 未运行复制也能复查")
        test_verify_without_run(new_case_dir())

        print("[18] 输出目录在原素材内部")
        test_output_inside_source_copy(new_case_dir())

        print("[19] 报告指纹组合")
        test_report_fingerprint()
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print("")
    print("-" * 56)
    print("通过 %d 项, 失败 %d 项" % (PASSES, len(FAILURES)))
    if FAILURES:
        for item in FAILURES:
            print("  ! " + item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
