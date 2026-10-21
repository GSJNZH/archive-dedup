#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""archive_dedup_cli.py

无界面命令行封装，复用 archive_dedup.worker() 的扫描、比对、复制/移动逻辑。
供 DeepSeek harness、计划任务、批处理脚本使用。

示例:
    python archive_dedup_cli.py D:\\Archive D:\\Source D:\\未归档补充 --dry-run
    python archive_dedup_cli.py D:\\Archive D:\\Source D:\\未归档补充 --mode exact --workers 4
    python archive_dedup_cli.py D:\\Archive D:\\Source D:\\未归档补充 --copy-mode move --yes
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time

from archive_dedup import (
    APP_VERSION,
    COPY_MODE_COPY,
    COPY_MODE_MOVE,
    MODE_EXACT,
    MODE_FAST,
    MODE_TURBO,
    strip_quotes,
    verify_worker,
    worker,
)

MODE_BY_NAME = {"turbo": MODE_TURBO, "fast": MODE_FAST, "exact": MODE_EXACT}
COPY_BY_NAME = {"copy": COPY_MODE_COPY, "move": COPY_MODE_MOVE}
COPY_NAME = {v: k for k, v in COPY_BY_NAME.items()}

EXIT_OK = 0
EXIT_CANCELLED = 1
EXIT_USAGE = 2
EXIT_RUNTIME = 3
EXIT_PARTIAL = 4
EXIT_VERIFY_FAILED = 5
EXIT_INTERRUPTED = 130


def _norm(path):
    return os.path.normcase(os.path.abspath(os.path.expanduser(strip_quotes(path))))


def _is_inside(path, parent):
    """path 等于 parent 或位于 parent 之下（Windows 下忽略大小写）。"""
    p = _norm(path)
    r = _norm(parent)
    return p == r or p.startswith(r + os.sep)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="archive_dedup_cli.py",
        description="按内容指纹对比归档与原素材，只输出原素材中未被归档覆盖的文件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
退出码:
  0   成功（含试运行）
  1   用户在确认处取消
  2   参数或路径不合法
  3   运行期错误
  4   部分文件处理失败（其余成功）
  5   复查未通过（有缺失/不一致/多余/.part）
  130 被 Ctrl+C 中断

示例:
  python archive_dedup_cli.py D:\\Archive D:\\Source D:\\未归档补充 --dry-run
  python archive_dedup_cli.py D:\\Archive D:\\Source D:\\未归档补充 --mode fast --workers 4
""",
    )
    parser.add_argument("archive", metavar="归档目录", help="归档文件夹（含工程和已打包素材）")
    parser.add_argument("source", metavar="原素材目录", help="原始素材文件夹")
    parser.add_argument("output", metavar="输出目录", help="未归档补充输出目录，保留相对路径")
    parser.add_argument("--mode", choices=["turbo", "fast", "exact"], default="fast",
                        help="比对模式: turbo=大小+修改时间, fast=头尾采样哈希(默认), exact=全量MD5")
    parser.add_argument("--copy-mode", choices=["copy", "move"], default="copy",
                        help="copy=复制(默认，安全), move=同盘移动(会从原素材移除未归档文件)")
    parser.add_argument("--workers", type=int, default=8,
                        help="并发线程数 1-32，默认 8；机械盘建议 1，SSD 可试 8")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行：只扫描比对并列出未归档文件，不复制/移动，也不创建输出目录")
    parser.add_argument("--verify", action="store_true",
                        help="复查模式：检查未归档文件是否都在输出目录中且内容一致；输出目录可以不存在（按缺失处理），不复制")
    parser.add_argument("--verbose", action="store_true",
                        help="逐文件输出扫描/比对/复制日志（默认只输出阶段性摘要）")
    parser.add_argument("--json", action="store_true",
                        help="stdout 只输出 JSON 摘要，日志改走 stderr")
    parser.add_argument("--progress", action="store_true",
                        help="每秒向 stderr 输出一次进度")
    parser.add_argument("--yes", action="store_true",
                        help="同盘移动时跳过交互确认（非交互环境必须加）")
    parser.add_argument("--force", action="store_true",
                        help="输出目录非空时仍然继续（可能混入旧文件）")
    parser.add_argument("--version", action="version",
                        version="archive_dedup_cli %s" % APP_VERSION)
    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    archive = _norm(args.archive)
    source = _norm(args.source)
    output = _norm(args.output)

    def _display(path):
        return os.path.normpath(os.path.abspath(
            os.path.expanduser(strip_quotes(path))))

    archive_display = _display(args.archive)
    source_display = _display(args.source)
    output_display = _display(args.output)

    log_stream = sys.stderr if args.json else sys.stdout

    def say(message=""):
        log_stream.write(str(message) + "\n")
        log_stream.flush()

    def fail(code, message):
        say("错误: " + message)
        if args.json:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return code

    # ---- 参数与路径校验 -------------------------------------------------
    if not os.path.isdir(archive):
        return fail(EXIT_USAGE, "归档文件夹不存在: " + archive)
    if not os.path.isdir(source):
        return fail(EXIT_USAGE, "原素材文件夹不存在: " + source)
    if args.verify and args.dry_run:
        return fail(EXIT_USAGE, "--verify 不能和 --dry-run 同时使用。")
    if archive == source:
        return fail(EXIT_USAGE, "归档文件夹和原素材文件夹不能相同。")
    if _is_inside(output, archive):
        return fail(EXIT_USAGE, "输出目录不能是归档文件夹或位于其内部: " + output)
    if _is_inside(archive, output):
        return fail(EXIT_USAGE, "输出目录不能包含归档文件夹: " + output)
    if output == source:
        return fail(EXIT_USAGE, "输出目录不能等于原素材文件夹。")
    if not (1 <= args.workers <= 32):
        return fail(EXIT_USAGE, "--workers 需在 1-32 之间。")

    mode = MODE_BY_NAME[args.mode]
    copy_mode = COPY_BY_NAME[args.copy_mode]

    if args.verify:
        if os.path.exists(output) and not os.path.isdir(output):
            return fail(EXIT_USAGE, "输出路径不是文件夹: " + output)
    else:
        if not args.dry_run:
            try:
                os.makedirs(output, exist_ok=True)
            except OSError as exc:
                return fail(EXIT_USAGE, "无法创建输出目录: %s" % exc)
            if not args.force:
                try:
                    existing = [n for n in os.listdir(output) if not n.startswith(".")]
                except OSError as exc:
                    return fail(EXIT_USAGE, "无法读取输出目录: %s" % exc)
                if existing:
                    return fail(
                        EXIT_USAGE,
                        "输出目录非空（%d 项）。确认可以混入旧文件后加 --force，或换一个空目录。"
                        % len(existing))

        if copy_mode == COPY_MODE_MOVE and not args.dry_run and not args.yes:
            if not sys.stdin.isatty():
                return fail(EXIT_USAGE,
                            "同盘移动会从原素材移除未归档文件；非交互环境请显式加 --yes。")
            say("")
            say("警告: 同盘移动会从原素材文件夹移除未归档文件（已归档的重复文件保留）。")
            say("确认继续？输入 y 回车: ")
            try:
                answer = sys.stdin.readline()
            except (EOFError, OSError):
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                say("已取消，未做任何修改。")
                return EXIT_CANCELLED

    # ---- 运行 worker ----------------------------------------------------
    msg_queue = queue.Queue()
    stop_event = threading.Event()
    started = time.time()

    def run_worker():
        try:
            if args.verify:
                verify_worker(archive_display, source_display, output_display,
                              mode, args.verbose, msg_queue, stop_event)
            else:
                worker(archive_display, source_display, output_display,
                       args.dry_run, args.verbose,
                       mode, copy_mode, args.workers, msg_queue, stop_event)
        except BaseException as exc:  # worker 内部已捕获 Exception，这里兜底
            msg_queue.put(("error", "%s: %s" % (type(exc).__name__, exc)))

    thread = threading.Thread(target=run_worker, name="archive-dedup-worker", daemon=True)
    thread.start()

    summary = None
    error = None
    last_progress = 0.0
    progress_tty = sys.stderr.isatty()

    try:
        while True:
            try:
                kind, payload = msg_queue.get(timeout=0.2)
            except queue.Empty:
                if not thread.is_alive():
                    if summary is None and error is None:
                        error = "工作线程意外结束，未返回结果。"
                    break
                continue

            if kind == "logs":
                say(payload)
            elif kind == "progress":
                if not args.progress:
                    continue
                now = time.time()
                if now - last_progress < 1.0:
                    continue
                last_progress = now
                pct, text = payload
                line = "[%5.1f%%] %s" % (pct, text)
                if progress_tty:
                    sys.stderr.write("\r" + line + "\x1b[K")
                else:
                    sys.stderr.write(line + "\n")
                sys.stderr.flush()
            elif kind == "done":
                summary = payload
                break
            elif kind == "error":
                error = payload
                break
    except KeyboardInterrupt:
        stop_event.set()
        say("")
        say("收到中断，正在停止（已复制/移动的文件保留在输出目录）...")
        thread.join(timeout=5.0)
        if progress_tty and args.progress:
            sys.stderr.write("\n")
            sys.stderr.flush()
        if args.json:
            print(json.dumps({"ok": False, "interrupted": True}, ensure_ascii=False))
        return EXIT_INTERRUPTED

    if progress_tty and args.progress:
        sys.stderr.write("\n")
        sys.stderr.flush()

    if error is not None:
        say("错误: " + str(error))
        if args.json:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return EXIT_RUNTIME

    if summary is None:
        say("错误: 没有收到完成结果。")
        if args.json:
            print(json.dumps({"ok": False, "error": "no result"}, ensure_ascii=False))
        return EXIT_RUNTIME

    if args.verify:
        cancelled = bool(summary.get("cancelled"))
        verify_ok = bool(summary.get("ok"))
        elapsed = time.time() - started
        result = {
            "ok": verify_ok,
            "verify": True,
            "cancelled": cancelled,
            "mode": args.mode,
            "elapsed_seconds": round(elapsed, 3),
            "archived": int(summary.get("archived", 0)),
            "unused": int(summary.get("unused", 0)),
            "verified": int(summary.get("verified", 0)),
            "missing": int(summary.get("missing", 0)),
            "mismatch": int(summary.get("mismatch", 0)),
            "extra": int(summary.get("extra", 0)),
            "part": int(summary.get("part", 0)),
            "errors": int(summary.get("errors", 0)),
            "no_manifest": int(summary.get("no_manifest", 0)),
            "unused_bytes": int(summary.get("unused_bytes", 0)),
            "report": summary.get("report", ""),
            "output": output_display,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            say("")
            say("-" * 56)
            say("复查通过" if verify_ok else "复查未通过")
            say("已归档(跳过): %d" % result["archived"])
            say("未归档(应有): %d" % result["unused"])
            say("输出中匹配: %d" % result["verified"])
            say("缺失: %d" % result["missing"])
            say("内容不一致: %d" % result["mismatch"])
            say("多余文件: %d" % result["extra"])
            say(".part 残留: %d" % result["part"])
            say("读取失败: %d" % result["errors"])
            if result["no_manifest"]:
                say("缺少复查清单: 是（无法判断移动模式的完整性）")
            if result["report"]:
                say("复查报告: " + result["report"])
            say("用时: %.1f 秒" % elapsed)
        if cancelled:
            return EXIT_INTERRUPTED
        return EXIT_OK if verify_ok else EXIT_VERIFY_FAILED

    cancelled = bool(summary.get("cancelled"))
    failed = int(summary.get("failed", 0))
    elapsed = time.time() - started
    result = {
        "ok": (not cancelled) and failed == 0,
        "cancelled": cancelled,
        "dry_run": bool(summary.get("dry")),
        "mode": args.mode,
        "copy_mode": COPY_NAME.get(summary.get("copy_mode"), args.copy_mode),
        "workers": args.workers,
        "elapsed_seconds": round(elapsed, 3),
        "archived": int(summary.get("archived", 0)),
        "unique": int(summary.get("unique", 0)),
        "copied": int(summary.get("copied", 0)),
        "failed": failed,
        "output": None if summary.get("dry") else output_display,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        say("")
        say("-" * 56)
        if cancelled:
            say("已取消")
        elif result["dry_run"]:
            say("试运行完成（未复制任何文件）")
        elif failed:
            say("完成，但有 %d 个文件失败" % failed)
        else:
            say("完成（%s）" % ("移动" if result["copy_mode"] == "move" else "复制"))
        say("已归档(跳过): %d" % result["archived"])
        say("未归档: %d" % result["unique"])
        if not result["dry_run"]:
            say("成功处理: %d" % result["copied"])
            say("失败: %d" % failed)
            say("输出目录: " + output_display)
        say("用时: %.1f 秒" % elapsed)
        if result["dry_run"]:
            say("")
            say("下一步: 去掉 --dry-run 再执行一次才会真正复制/移动。")

    if cancelled:
        return EXIT_INTERRUPTED
    if failed:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
