import os
import sys
import json
import time
import hashlib
import shutil
import threading
import queue
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

CHUNK_SIZE = 1024 * 1024
SAMPLE_SIZE = 64 * 1024
CONFIG_NAME = "archive_dedup_config.json"
MANIFEST_NAME = "_archive_dedup_manifest.json"
REPORT_NAME = "复查报告.txt"
DEFAULT_OUTPUT_NAME = "09_04-未使用画面"
LOG_MAX_LINES = 5000
LOG_TRIM_LINES = 2000

MODE_TURBO = "极速(大小+修改时间,不读内容)"
MODE_FAST = "快速(采样哈希,推荐)"
MODE_EXACT = "精确(全量MD5)"
MODES = [MODE_FAST, MODE_TURBO, MODE_EXACT]

COPY_MODE_COPY = "复制(安全,保留原素材)"
COPY_MODE_MOVE = "同盘移动(瞬间,会移除原素材中的文件)"
COPY_MODES = [COPY_MODE_COPY, COPY_MODE_MOVE]

WORKER_CHOICES = ["1 (机械盘推荐)", "2", "4", "8 (SSD可试)"]

IS_WINDOWS = (os.name == "nt")

if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CopyFileW = _kernel32.CopyFileW
    _CopyFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.BOOL]
    _CopyFileW.restype = wintypes.BOOL
else:
    _CopyFileW = None


def _long_path(p):
    p = os.path.abspath(p)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def fast_copy(src, dst):
    if not IS_WINDOWS or _CopyFileW is None:
        return False
    if not _CopyFileW(_long_path(src), _long_path(dst), False):
        return False
    try:
        st = os.stat(src)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass
    return True


def copy_one(src, dst):
    d = os.path.dirname(dst)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = dst + ".part"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    try:
        copied = False
        if IS_WINDOWS:
            try:
                copied = fast_copy(src, tmp)
            except OSError:
                copied = False
        if not copied:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def move_one(src, dst):
    d = os.path.dirname(dst)
    if d:
        os.makedirs(d, exist_ok=True)
    if os.path.exists(dst):
        raise OSError("目标已存在: " + dst)
    os.rename(src, dst)


def same_volume(a, b):
    try:
        da = os.path.splitdrive(os.path.abspath(a))[0].lower()
        db = os.path.splitdrive(os.path.abspath(b))[0].lower()
        return bool(da) and da == db
    except Exception:
        return False


def strip_quotes(s):
    s = s.strip()
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        s = s[1:-1].strip()
    return s


def normalize_path(p):
    return os.path.normcase(os.path.abspath(os.path.expanduser(strip_quotes(p))))


def same_path(a, b):
    return normalize_path(a) == normalize_path(b)


def is_inside(path, parent):
    """path 等于 parent 或位于 parent 之下（Windows 下忽略大小写）。"""
    p = normalize_path(path)
    r = normalize_path(parent).rstrip("\\/")
    if not r:
        r = os.sep
    return p == r or p.startswith(r + os.sep)


def config_path():
    base = os.path.dirname(os.path.abspath(sys.argv[0]))
    try:
        probe = os.path.join(base, ".write_probe")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return os.path.join(base, CONFIG_NAME)
    except OSError:
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(appdata, CONFIG_NAME)


def full_hash(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def sample_key(path, size):
    with open(path, "rb") as f:
        head = f.read(SAMPLE_SIZE)
        tail = b""
        if size > SAMPLE_SIZE * 2:
            f.seek(size - SAMPLE_SIZE)
            tail = f.read(SAMPLE_SIZE)
        elif size > SAMPLE_SIZE:
            tail = f.read()
    return (size, hashlib.md5(head).hexdigest(), hashlib.md5(tail).hexdigest())


def make_key(path, mode):
    st = os.stat(path)
    size = st.st_size
    if mode == MODE_TURBO:
        return (size, st.st_mtime_ns)
    if mode == MODE_FAST:
        return sample_key(path, size)
    return (size, full_hash(path))


def key_display(key, mode):
    if mode == MODE_TURBO:
        return "size+mtime:%d" % key[1]
    if mode == MODE_FAST:
        return "%s..%s" % (key[1][:8], key[2][:8])
    return key[1][:16] + "..."


def list_files(root_dir, exclude_dirs=None):
    exclude_dirs = set(normalize_path(d) for d in (exclude_dirs or []))
    result = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs
                   if normalize_path(os.path.join(root, d)) not in exclude_dirs]
        for name in files:
            result.append(os.path.join(root, name))
    return result


def sum_sizes(files):
    s = 0
    for p in files:
        try:
            s += os.path.getsize(p)
        except OSError:
            pass
    return s


def fmt_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for u in units:
        if n < 1024.0:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f PB" % n


def disk_free(path):
    """返回 path 所在卷的可用字节数；路径不存在时向上找最近的存在目录。"""
    probe = os.path.abspath(path)
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def check_disk_space(path, need):
    if need <= 0:
        return True, ""
    free = disk_free(path)
    if free is None:
        return True, ""
    reserve = max(64 * 1024 * 1024, int(need * 0.01))
    if need + reserve > free:
        return False, ("输出磁盘空间不足: 需要 %s, 可用 %s, 还差 %s。"
                       % (fmt_size(need), fmt_size(free),
                          fmt_size(need + reserve - free)))
    return True, ""


def write_manifest(output_dir, source_dir, archive_dir, mode, copy_mode,
                   source_count, source_bytes, files, sizes, keys, emitter):
    """把本次应处理的文件清单写到输出目录，供移动模式/事后复查使用。"""
    records = []
    for path, size, key in zip(files, sizes, keys):
        if key is None:
            continue
        records.append({"rel": os.path.relpath(path, source_dir),
                        "size": size,
                        "key": list(key)})
    payload = {"version": 1,
               "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "mode": mode,
               "copy_mode": copy_mode,
               "source_count": source_count,
               "source_bytes": source_bytes,
               "source": source_dir,
               "archive": archive_dir,
               "files": records}
    path = os.path.join(output_dir, MANIFEST_NAME)
    tmp = path + ".part"
    try:
        os.makedirs(output_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        emitter.log("复查清单: %s (%d 项)" % (path, len(records)))
    except OSError as e:
        emitter.log("警告: 无法写入复查清单: " + str(e))
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def report_fingerprint(key, mode):
    if mode == MODE_TURBO:
        return "size+mtime:%d" % key[1]
    if mode == MODE_FAST:
        return "head=%s tail=%s" % (key[1], key[2])
    return "md5=%s" % key[1]


def write_verify_report(output_dir, source_dir, archive_dir, mode, stats, expected):
    """复查通过后，在输出目录写一份可读的 TXT 报告。"""
    project = os.path.basename(os.path.normpath(source_dir)) or "未命名项目"
    digest = hashlib.sha256()
    for rel in sorted(expected):
        size, key = expected[rel]
        digest.update(("%s\t%d\t%s\n"
                       % (rel, size, report_fingerprint(key, mode))).encode("utf-8"))
    lines = [
        "素材归档去重 · 复查报告",
        "=" * 60,
        "项目名称: " + project,
        "复查时间: " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "复查结果: 通过",
        "",
        "[目录]",
        "归档目录: " + archive_dir,
        "原素材目录: " + source_dir,
        "输出目录: " + output_dir,
        "处理方式: " + stats.get("copy_mode", "未知"),
        "",
        "[统计]",
        "归档文件: %d 个 / %s" % (stats["archive_count"], fmt_size(stats["archive_bytes"])),
        "源文件: %d 个 / %s" % (stats["source_count"], fmt_size(stats["source_bytes"])),
        "已归档(跳过): %d 个 / %s" % (stats["archived_count"], fmt_size(stats["archived_bytes"])),
        "未归档(应有): %d 个 / %s" % (len(expected), fmt_size(stats["expected_bytes"])),
        "输出匹配: %d 个 / %s" % (stats["verified"], fmt_size(stats["verified_bytes"])),
        "缺失: %d" % stats["missing"],
        "内容不一致: %d" % stats["mismatch"],
        "多余文件: %d" % stats["extra"],
        ".part 残留: %d" % stats["part"],
        "读取失败: %d" % stats["errors"],
        "输出集合指纹(SHA256): " + digest.hexdigest(),
        "",
        "[文件清单]",
        "序号\t相对路径\t大小\t指纹",
    ]
    for index, rel in enumerate(sorted(expected), 1):
        size, key = expected[rel]
        safe_rel = rel.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        lines.append("%d\t%s\t%s\t%s"
                     % (index, safe_rel, fmt_size(size), report_fingerprint(key, mode)))

    if mode == MODE_FAST:
        fingerprint_note = "快速模式 = 文件头 64KB MD5 + 文件尾 64KB MD5。"
    elif mode == MODE_EXACT:
        fingerprint_note = "精确模式 = 文件全量 MD5。"
    else:
        fingerprint_note = "极速模式 = 文件大小 + 修改时间，不读取内容，不含内容哈希。"

    lines += [
        "",
        "[说明]",
        "比对模式: " + mode,
        "指纹说明: " + fingerprint_note,
        "本报告由 archive_dedup 在复查通过后自动生成；复查时会忽略本文件。",
        "",
    ]

    path = os.path.join(output_dir, REPORT_NAME)
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)
        return path
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return ""


def fmt_time(sec):
    if sec is None or sec < 0 or sec > 86400 * 30:
        return "--:--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%02d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


class Emitter:
    def __init__(self, msg_queue, batch=50, interval=0.1):
        self.q = msg_queue
        self.buf = []
        self.lock = threading.Lock()
        self.batch = batch
        self.interval = interval
        self.last = time.time()

    def log(self, s):
        with self.lock:
            self.buf.append(s)
            if len(self.buf) >= self.batch or time.time() - self.last >= self.interval:
                self._flush_locked()

    def _flush_locked(self):
        if self.buf:
            self.q.put(("logs", "\n".join(self.buf)))
            self.buf = []
            self.last = time.time()

    def flush(self):
        with self.lock:
            self._flush_locked()


def worker(archive_dir, source_dir, output_dir, dry_run, verbose, mode,
           copy_mode, workers, msg_queue, stop_event):
    emitter = Emitter(msg_queue)
    try:
        t_all = time.time()
        emitter.log("比对模式: " + mode)
        if mode == MODE_FAST:
            emitter.log("  说明: 每个文件只读头尾各 64KB 计算指纹,不读全量内容。")
        elif mode == MODE_TURBO:
            emitter.log("  说明: 只用文件大小+修改时间判断,完全不读内容。")
        else:
            emitter.log("  说明: 全量 MD5,最准但最慢。")
        emitter.log("复制方式: " + copy_mode)
        emitter.flush()

        emitter.log("[0/3] 统计文件数量和总大小...")
        archive_files = list_files(archive_dir)
        exclude = []
        if is_inside(output_dir, source_dir):
            exclude = [output_dir]
        source_files = list_files(source_dir, exclude)
        total_a_size = sum_sizes(archive_files)
        total_s_size = sum_sizes(source_files)
        emitter.log("  归档文件夹:   %d 个文件, %s" % (len(archive_files), fmt_size(total_a_size)))
        emitter.log("  原素材文件夹: %d 个文件, %s" % (len(source_files), fmt_size(total_s_size)))
        emitter.flush()

        emitter.log("[1/3] 扫描归档文件夹...")
        key_set = set()
        n = len(archive_files)
        t1 = time.time()
        last_prog = 0.0
        for i, p in enumerate(archive_files):
            if stop_event.is_set():
                emitter.log("已取消。")
                emitter.flush()
                msg_queue.put(("done", {"cancelled": True, "archived": 0,
                                        "unique": 0, "copied": 0, "failed": 0,
                                        "dry": dry_run}))
                return
            size = 0
            disp = "ERROR"
            err = ""
            try:
                size = os.path.getsize(p)
                key = make_key(p, mode)
                key_set.add(key)
                disp = key_display(key, mode)
            except OSError as e:
                err = " | 读取失败: " + str(e)
            if verbose or i == 0 or i == n - 1 or (i + 1) % 50 == 0:
                elapsed = max(time.time() - t1, 0.001)
                fps = (i + 1) / elapsed
                eta = (n - i - 1) / fps if fps > 0 else -1
                emitter.log("[1/3] 归档 %d/%d | %s | %s | %s | %.1f 文件/秒 | 剩余 %s%s" % (
                    i + 1, n, os.path.basename(p), fmt_size(size), disp, fps, fmt_time(eta), err))
            now = time.time()
            if now - last_prog >= 0.1 or i == n - 1:
                last_prog = now
                elapsed = max(now - t1, 0.001)
                fps = (i + 1) / elapsed
                pct = ((i + 1) / n * 40.0) if n else 40.0
                msg_queue.put(("progress", (pct, "扫描归档 %d/%d | %.1f 文件/秒" % (i + 1, n, fps))))
        emitter.log("[1/3] 归档扫描完成, %d 个文件, 用时 %s" % (n, fmt_time(time.time() - t1)))
        emitter.flush()

        emitter.log("[2/3] 比对原素材文件夹...")
        unique_files = []
        unique_sizes = []
        unique_keys = []
        archived_count = 0
        n_s = len(source_files)
        t2 = time.time()
        last_prog = 0.0
        for i, p in enumerate(source_files):
            if stop_event.is_set():
                emitter.log("已取消。")
                emitter.flush()
                msg_queue.put(("done", {"cancelled": True,
                                        "archived": archived_count,
                                        "unique": len(unique_files),
                                        "copied": 0, "failed": 0,
                                        "dry": dry_run}))
                return
            size = 0
            disp = ""
            is_archived = False
            err = ""
            key = None
            try:
                size = os.path.getsize(p)
                key = make_key(p, mode)
                disp = key_display(key, mode)
                if key in key_set:
                    is_archived = True
            except OSError as e:
                err = " | 读取失败: " + str(e)
            if is_archived:
                archived_count += 1
            else:
                unique_files.append(p)
                unique_sizes.append(size)
                unique_keys.append(key)
            if verbose or i == 0 or i == n_s - 1 or (i + 1) % 50 == 0:
                elapsed = max(time.time() - t2, 0.001)
                fps = (i + 1) / elapsed
                eta = (n_s - i - 1) / fps if fps > 0 else -1
                emitter.log("[2/3] 原素材 %d/%d | %s | %s | %s | %s | %.1f 文件/秒 | 剩余 %s%s" % (
                    i + 1, n_s, os.path.basename(p), fmt_size(size), disp,
                    "已归档" if is_archived else "未归档", fps, fmt_time(eta), err))
            now = time.time()
            if now - last_prog >= 0.1 or i == n_s - 1:
                last_prog = now
                elapsed = max(now - t2, 0.001)
                fps = (i + 1) / elapsed
                pct = 40.0 + ((i + 1) / n_s * 40.0) if n_s else 80.0
                msg_queue.put(("progress", (pct, "比对原素材 %d/%d | %.1f 文件/秒" % (i + 1, n_s, fps))))

        total_u_size = sum(unique_sizes)
        emitter.log("[2/3] 比对完成。已归档(跳过): %d, 未归档(需上传): %d, 共 %s" % (
            archived_count, len(unique_files), fmt_size(total_u_size)))
        emitter.flush()

        if copy_mode == COPY_MODE_COPY and same_volume(source_dir, output_dir):
            emitter.log("提示: 输出目录与原素材在同一磁盘，复制会额外写入 %s；"
                        "如确认归档完整，可改用“同盘移动”瞬间完成"
                        "（会移除原素材中的未归档文件）。" % fmt_size(total_u_size))
            emitter.flush()

        if dry_run:
            for p in unique_files:
                emitter.log("  [独有] " + os.path.relpath(p, source_dir))
            emitter.log("试运行结束, 未复制任何文件。用时 %s" % fmt_time(time.time() - t_all))
            emitter.flush()
            msg_queue.put(("done", {"archived": archived_count,
                                    "unique": len(unique_files),
                                    "copied": 0, "failed": 0, "dry": True,
                                    "copy_mode": copy_mode}))
            return

        if copy_mode == COPY_MODE_MOVE and not same_volume(source_dir, output_dir):
            emitter.log("警告: 输出目录与原素材不在同一磁盘,移动会退化为复制+删除,已自动改为复制模式。")
            copy_mode = COPY_MODE_COPY

        if copy_mode == COPY_MODE_COPY:
            enough, reason = check_disk_space(output_dir, total_u_size)
            if not enough:
                emitter.log("错误: " + reason)
                emitter.flush()
                msg_queue.put(("error", reason))
                return

        stale_report = os.path.join(output_dir, REPORT_NAME)
        try:
            if os.path.isfile(stale_report):
                os.remove(stale_report)
                emitter.log("已清除旧复查报告，等待本次复查后重新生成。")
        except OSError as e:
            emitter.log("警告: 无法清除旧复查报告: " + str(e))
        emitter.flush()

        n_u = len(unique_files)
        if n_u:
            write_manifest(output_dir, source_dir, archive_dir, mode, copy_mode,
                           len(source_files), total_s_size,
                           unique_files, unique_sizes, unique_keys, emitter)
            emitter.flush()
        if n_u:
            emitter.log("[3/3] 开始%s未归档文件..." % ("移动" if copy_mode == COPY_MODE_MOVE else "复制"))
        else:
            emitter.log("[3/3] 没有需要处理的未归档文件。")
        emitter.flush()

        lock = threading.Lock()
        state = {"done": 0, "ok": 0, "failed": 0, "bytes": 0}
        t3 = time.time()
        last_report = [0.0]
        act = "移动" if copy_mode == COPY_MODE_MOVE else "复制"

        def task(item):
            i, p, size = item
            if stop_event.is_set():
                return
            rel = os.path.relpath(p, source_dir)
            dst = os.path.join(output_dir, rel)
            ok = False
            err = ""
            try:
                if copy_mode == COPY_MODE_MOVE:
                    move_one(p, dst)
                else:
                    copy_one(p, dst)
                ok = True
            except Exception as e:
                err = " | 失败: " + str(e)
            now = time.time()
            with lock:
                state["done"] += 1
                if ok:
                    state["ok"] += 1
                    state["bytes"] += size
                else:
                    state["failed"] += 1
                dn = state["done"]
                do_report = (now - last_report[0] >= 0.15) or (dn >= n_u)
                if do_report:
                    last_report[0] = now
                    b = state["bytes"]
                    o = state["ok"]
            if err:
                emitter.log("  " + rel + err)
            if do_report:
                elapsed = max(now - t3, 0.001)
                speed = b / elapsed
                eta = (total_u_size - b) / speed if speed > 0 else -1
                if verbose:
                    emitter.log("[3/3] %s %d/%d | %s | %s | 累计 %s / %s | %s/s | 剩余 %s" % (
                        act, dn, n_u, rel, fmt_size(size), fmt_size(b),
                        fmt_size(total_u_size), fmt_size(speed), fmt_time(eta)))
                pct = 80.0 + (dn / n_u * 20.0) if n_u else 100.0
                msg_queue.put(("progress", (pct, "%s %d/%d | %s/s" % (
                    act, dn, n_u, fmt_size(speed)))))

        items = [(i, p, s) for i, (p, s) in enumerate(zip(unique_files, unique_sizes))]
        workers = max(1, int(workers))
        if workers <= 1 or n_u <= 1:
            for it in items:
                task(it)
        else:
            task_q = queue.Queue()
            for it in items:
                task_q.put(it)

            def run():
                while not stop_event.is_set():
                    try:
                        it = task_q.get_nowait()
                    except queue.Empty:
                        return
                    task(it)

            threads = []
            for _ in range(min(workers, n_u)):
                th = threading.Thread(target=run, daemon=True)
                th.start()
                threads.append(th)
            for th in threads:
                th.join()

        emitter.flush()
        with lock:
            ok_count = state["ok"]
            failed_count = state["failed"]
        if stop_event.is_set():
            emitter.log("已取消。成功 %d, 失败 %d / %d, 未处理的文件仍在原素材中。"
                        % (ok_count, failed_count, n_u))
            emitter.flush()
            msg_queue.put(("done", {"cancelled": True,
                                    "archived": archived_count,
                                    "unique": n_u,
                                    "copied": ok_count,
                                    "failed": failed_count,
                                    "dry": False,
                                    "output": output_dir,
                                    "copy_mode": copy_mode}))
            return
        emitter.log("全部完成。成功 %d, 失败 %d, 共 %d。总用时 %s"
                    % (ok_count, failed_count, n_u, fmt_time(time.time() - t_all)))
        emitter.flush()
        msg_queue.put(("done", {"archived": archived_count,
                                "unique": n_u,
                                "copied": ok_count,
                                "failed": failed_count,
                                "dry": False,
                                "output": output_dir,
                                "copy_mode": copy_mode}))
    except Exception as e:
        emitter.log("错误: " + repr(e))
        emitter.flush()
        msg_queue.put(("error", repr(e)))


def verify_worker(archive_dir, source_dir, output_dir, mode, verbose,
                  msg_queue, stop_event, write_report=True):
    """复查：原素材中未归档的文件，是否都已在输出目录中且内容一致。"""
    emitter = Emitter(msg_queue)

    def cancelled_payload(archived=0, unused=0, verified=0):
        return {"verify": True, "cancelled": True, "ok": False,
                "archived": archived, "unused": unused, "verified": verified,
                "missing": 0, "mismatch": 0, "extra": 0, "part": 0,
                "errors": 0, "unused_bytes": 0}

    try:
        t_all = time.time()
        emitter.log("复查模式: " + mode)
        emitter.log("  检查原素材中未被归档覆盖的文件，是否都在输出目录中且内容一致。")
        emitter.flush()

        if os.path.exists(output_dir) and not os.path.isdir(output_dir):
            raise OSError("输出路径不是文件夹: " + output_dir)
        output_exists = os.path.isdir(output_dir)
        if not output_exists:
            emitter.log("警告: 输出目录不存在，按空目录复查：所有未归档文件都会记为缺失。")
        emitter.flush()

        manifest_path = os.path.join(output_dir, MANIFEST_NAME)
        manifest = None
        if output_exists and os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("files"), list):
                    manifest = data
            except (OSError, ValueError) as e:
                emitter.log("警告: 复查清单读取失败: " + str(e))
        manifest_source_count = 0
        manifest_source_bytes = 0
        manifest_copy_mode = ""
        if manifest:
            try:
                manifest_source_count = int(manifest.get("source_count", 0))
                manifest_source_bytes = int(manifest.get("source_bytes", 0))
            except (TypeError, ValueError):
                manifest_source_count = 0
                manifest_source_bytes = 0
            manifest_copy_mode = str(manifest.get("copy_mode", ""))
            manifest_mode = manifest.get("mode")
            if manifest_mode in MODES:
                mode = manifest_mode
            emitter.log("使用复查清单: %d 项 (模式 %s)"
                        % (len(manifest["files"]), mode))
        else:
            emitter.log("没有复查清单，按当前原素材重新比对。")
        emitter.flush()

        archive_files = list_files(archive_dir)
        exclude = []
        if is_inside(output_dir, source_dir):
            exclude = [output_dir]
        source_files = list_files(source_dir, exclude)
        read_errors = 0

        emitter.log("[1/4] 扫描归档文件夹...")
        key_set = set()
        archive_bytes = 0
        n = len(archive_files)
        for i, p in enumerate(archive_files):
            if stop_event.is_set():
                emitter.log("已取消。")
                emitter.flush()
                msg_queue.put(("done", cancelled_payload()))
                return
            try:
                key = make_key(p, mode)
                key_set.add(key)
                archive_bytes += key[0]
            except OSError as e:
                read_errors += 1
                emitter.log("  [归档读取失败] " + p + ": " + str(e))
            if (i + 1) % 50 == 0 or i == n - 1:
                pct = ((i + 1) / n * 25.0) if n else 25.0
                msg_queue.put(("progress", (pct, "复查: 扫描归档 %d/%d" % (i + 1, n))))
        emitter.log("  归档: %d 个文件" % n)
        emitter.flush()

        emitter.log("[2/4] 比对原素材，找出未归档文件...")
        expected = {}
        archived_count = 0
        archived_bytes = 0
        source_bytes = 0
        n_s = len(source_files)
        for i, p in enumerate(source_files):
            if stop_event.is_set():
                emitter.log("已取消。")
                emitter.flush()
                msg_queue.put(("done", cancelled_payload(archived_count, len(expected))))
                return
            try:
                size = os.path.getsize(p)
                key = make_key(p, mode)
            except OSError as e:
                read_errors += 1
                emitter.log("  [读取失败] " + p + ": " + str(e))
                continue
            source_bytes += size
            if key in key_set:
                archived_count += 1
                archived_bytes += size
            else:
                expected[os.path.relpath(p, source_dir)] = (size, key)
            if (i + 1) % 50 == 0 or i == n_s - 1:
                pct = 25.0 + ((i + 1) / n_s * 25.0) if n_s else 50.0
                msg_queue.put(("progress", (pct, "复查: 比对原素材 %d/%d" % (i + 1, n_s))))

        source_expected = expected
        report_source_count = len(source_files)
        report_source_bytes = source_bytes
        if manifest and manifest_source_count > 0:
            report_source_count = manifest_source_count
            report_source_bytes = manifest_source_bytes
        if manifest:
            from_manifest = {}
            for rec in manifest["files"]:
                try:
                    rel = os.path.normpath(str(rec["rel"]))
                    size = int(rec["size"])
                    key = tuple(rec["key"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not rel or os.path.isabs(rel) or rel.startswith(".."):
                    continue
                from_manifest[rel] = (size, key)
            if set(from_manifest) != set(source_expected):
                emitter.log("提示: 清单 %d 项，原素材当前未归档 %d 项，以清单为准。"
                            % (len(from_manifest), len(source_expected)))
            expected = from_manifest
        emitter.log("  已归档: %d, 未归档(应有): %d" % (archived_count, len(expected)))
        emitter.flush()

        emitter.log("[3/4] 扫描输出目录...")
        out_map = {}
        part_files = []
        out_files = list_files(output_dir) if output_exists else []
        n_o = len(out_files)
        for i, p in enumerate(out_files):
            if stop_event.is_set():
                emitter.log("已取消。")
                emitter.flush()
                msg_queue.put(("done", cancelled_payload(archived_count, len(expected))))
                return
            rel = os.path.relpath(p, output_dir)
            if rel.lower() in (MANIFEST_NAME.lower(), REPORT_NAME.lower()):
                continue
            if rel.lower().endswith(".part"):
                part_files.append(rel)
                continue
            try:
                out_map[rel] = (os.path.getsize(p), make_key(p, mode))
            except OSError as e:
                read_errors += 1
                emitter.log("  [输出读取失败] " + rel + ": " + str(e))
            if (i + 1) % 50 == 0 or i == n_o - 1:
                pct = 50.0 + ((i + 1) / n_o * 25.0) if n_o else 75.0
                msg_queue.put(("progress", (pct, "复查: 扫描输出 %d/%d" % (i + 1, n_o))))
        emitter.log("  输出: %d 个文件" % n_o)
        emitter.flush()

        emitter.log("[4/4] 逐项核对...")
        missing = []
        mismatch = []
        verified = 0
        verified_bytes = 0
        for idx, (rel, (size, key)) in enumerate(expected.items()):
            if stop_event.is_set():
                emitter.log("已取消。")
                emitter.flush()
                msg_queue.put(("done", cancelled_payload(archived_count, len(expected), verified)))
                return
            item = out_map.get(rel)
            if item is None:
                missing.append(rel)
            elif item[0] != size or item[1] != key:
                mismatch.append(rel)
            else:
                verified += 1
                verified_bytes += size
                if verbose:
                    emitter.log("  [一致] " + rel)
            if (idx + 1) % 200 == 0:
                pct = 75.0 + ((idx + 1) / len(expected) * 25.0)
                msg_queue.put(("progress", (pct, "复查: 核对 %d/%d" % (idx + 1, len(expected)))))

        extra = [rel for rel in out_map if rel not in expected]

        no_manifest = 0
        if not manifest and not expected and out_map:
            no_manifest = 1
            emitter.log("警告: 没有复查清单，且原素材中已无未归档文件；"
                        "可能使用了移动模式，无法判断是否全部移动。")

        def report(label, items):
            for rel in items[:200]:
                emitter.log("  " + label + " " + rel)
            if len(items) > 200:
                emitter.log("  ... 还有 %d 个%s" % (len(items) - 200, label))

        report("[缺失]", missing)
        report("[不一致]", mismatch)
        report("[多余]", extra)
        report("[.part]", part_files)

        total_expected = sum(size for size, _ in expected.values())
        ok = (not missing and not mismatch and not extra
              and not part_files and read_errors == 0 and no_manifest == 0)
        emitter.log("复查结果: %s" % ("通过" if ok else "未通过"))
        emitter.log("  未归档应有: %d 个, %s" % (len(expected), fmt_size(total_expected)))
        emitter.log("  输出匹配:   %d 个, %s" % (verified, fmt_size(verified_bytes)))
        emitter.log("  缺失: %d, 内容不一致: %d, 多余: %d, .part: %d, 读取失败: %d"
                    % (len(missing), len(mismatch), len(extra), len(part_files), read_errors))
        if no_manifest:
            emitter.log("  缺少复查清单，且无法从原素材推断应移动的文件。")

        report_path = ""
        if ok and write_report and expected:
            report_path = write_verify_report(
                output_dir, source_dir, archive_dir, mode,
                {"archive_count": n,
                 "archive_bytes": archive_bytes,
                 "source_count": report_source_count,
                 "source_bytes": report_source_bytes,
                 "copy_mode": manifest_copy_mode or "未知（无清单）",
                 "archived_count": archived_count,
                 "archived_bytes": archived_bytes,
                 "expected_bytes": total_expected,
                 "verified": verified,
                 "verified_bytes": verified_bytes,
                 "missing": len(missing),
                 "mismatch": len(mismatch),
                 "extra": len(extra),
                 "part": len(part_files),
                 "errors": read_errors},
                expected)
            if report_path:
                emitter.log("复查报告: " + report_path)
            else:
                emitter.log("警告: 复查报告写入失败。")
        emitter.log("总用时 %s" % fmt_time(time.time() - t_all))
        emitter.flush()
        msg_queue.put(("progress", (100.0, "复查完成")))
        msg_queue.put(("done", {"verify": True, "cancelled": False, "ok": ok,
                                "archived": archived_count,
                                "unused": len(expected),
                                "verified": verified,
                                "missing": len(missing),
                                "mismatch": len(mismatch),
                                "extra": len(extra),
                                "part": len(part_files),
                                "errors": read_errors,
                                "no_manifest": no_manifest,
                                "unused_bytes": total_expected,
                                "report": report_path,
                                "output": output_dir}))
    except Exception as e:
        emitter.log("错误: " + repr(e))
        emitter.flush()
        msg_queue.put(("error", repr(e)))


class App:
    def __init__(self, root):
        self.root = root
        root.title("素材归档去重工具(内核复制/多线程/同盘移动)")
        root.geometry("1020x760")
        root.minsize(900, 640)

        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.thread = None
        self.log_lines = 0
        self.copy_summary = None
        self.last_run = None

        self.archive_var = tk.StringVar()
        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.mode_var = tk.StringVar(value=MODE_FAST)
        self.copy_var = tk.StringVar(value=COPY_MODE_COPY)
        self.workers_var = tk.StringVar(value=WORKER_CHOICES[0])

        self._build_ui()
        self._load_config()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self.poll)

    def _build_ui(self):
        frm = ttk.LabelFrame(self.root, text="1. 指定目录")
        frm.pack(fill="x", padx=10, pady=8)
        frm.columnconfigure(1, weight=1)

        self._add_path_row(frm, 0, "归档文件夹\n(含工程+已打包素材):",
                           self.archive_var, self._pick_archive, self._open_archive)
        self._add_path_row(frm, 1, "原素材文件夹:",
                           self.source_var, self._pick_source, self._open_source)
        self._add_path_row(frm, 2, "未归档补充\n输出目录:",
                           self.output_var, self._pick_output, self._open_output)

        opt = ttk.LabelFrame(self.root, text="2. 选项与执行")
        opt.pack(fill="x", padx=10, pady=4)

        row1 = ttk.Frame(opt)
        row1.pack(fill="x", padx=6, pady=3)
        ttk.Label(row1, text="比对:").pack(side="left", padx=(4, 2))
        ttk.Combobox(row1, textvariable=self.mode_var, values=MODES,
                     state="readonly", width=24).pack(side="left", padx=4)
        ttk.Label(row1, text="复制方式:").pack(side="left", padx=(12, 2))
        ttk.Combobox(row1, textvariable=self.copy_var, values=COPY_MODES,
                     state="readonly", width=32).pack(side="left", padx=4)
        ttk.Label(row1, text="并发:").pack(side="left", padx=(12, 2))
        ttk.Combobox(row1, textvariable=self.workers_var, values=WORKER_CHOICES,
                     state="readonly", width=16).pack(side="left", padx=4)

        row2 = ttk.Frame(opt)
        row2.pack(fill="x", padx=6, pady=3)
        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="试运行(只列清单)", variable=self.dry_var).pack(
            side="left", padx=6)
        self.verbose_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="详细日志", variable=self.verbose_var).pack(
            side="left", padx=6)
        self.auto_verify_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="完成后自动复查", variable=self.auto_verify_var).pack(
            side="left", padx=6)
        self.btn_cancel = ttk.Button(row2, text="取消", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="right", padx=6)
        self.btn_start = ttk.Button(row2, text="开始", command=self.start)
        self.btn_start.pack(side="right", padx=6)
        self.btn_verify = ttk.Button(row2, text="复查输出", command=self.verify)
        self.btn_verify.pack(side="right", padx=6)

        prog = ttk.Frame(self.root)
        prog.pack(fill="x", padx=10, pady=6)
        self.progress = ttk.Progressbar(prog, maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(prog, textvariable=self.status_var, width=48).pack(side="left", padx=8)

        logfrm = ttk.LabelFrame(self.root, text="3. 实时日志 / 文件清单")
        logfrm.pack(fill="both", expand=True, padx=10, pady=8)
        self.log = scrolledtext.ScrolledText(logfrm, height=18, wrap="none")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        self.log.configure(state="disabled")

    def _add_path_row(self, parent, row, label, var, on_pick, on_open):
        ttk.Label(parent, text=label, justify="left").grid(
            row=row, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(parent, textvariable=var).grid(
            row=row, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(parent, text="浏览...", command=on_pick).grid(
            row=row, column=2, padx=4, pady=6)
        ttk.Button(parent, text="打开", command=on_open, width=6).grid(
            row=row, column=3, padx=4, pady=6)

    @staticmethod
    def _path_text(var):
        value = strip_quotes(var.get())
        return os.path.normpath(value) if value else value

    def _pick_dir(self, title, var):
        init = var.get().strip() or os.path.expanduser("~")
        p = filedialog.askdirectory(title=title, initialdir=init)
        if p:
            p = os.path.normpath(p)
            var.set(p)
            self._save_config()
        return p

    def _pick_archive(self):
        self._pick_dir("选择归档文件夹", self.archive_var)

    def _pick_source(self):
        p = self._pick_dir("选择原素材文件夹", self.source_var)
        if p:
            self.output_var.set(
                os.path.normpath(os.path.join(p, DEFAULT_OUTPUT_NAME)))
            self._save_config()

    def _pick_output(self):
        self._pick_dir("选择未归档补充输出目录(可新建空文件夹)", self.output_var)

    def _open_path(self, var):
        p = strip_quotes(var.get())
        if p and os.path.isdir(p):
            os.startfile(p)
        elif p:
            messagebox.showwarning("提示", "目录不存在:\n" + p)
        else:
            messagebox.showwarning("提示", "还没有填写路径。")

    def _open_archive(self):
        self._open_path(self.archive_var)

    def _open_source(self):
        self._open_path(self.source_var)

    def _open_output(self):
        self._open_path(self.output_var)

    def _load_config(self):
        p = config_path()
        if not os.path.isfile(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for key, var in (("archive", self.archive_var),
                             ("source", self.source_var),
                             ("output", self.output_var)):
                value = cfg.get(key, "")
                if value:
                    value = os.path.normpath(value)
                var.set(value)
            if cfg.get("mode") in MODES:
                self.mode_var.set(cfg["mode"])
            if cfg.get("copy_mode") in COPY_MODES:
                self.copy_var.set(cfg["copy_mode"])
            if cfg.get("workers") in WORKER_CHOICES:
                self.workers_var.set(cfg["workers"])
        except Exception:
            pass

    def _save_config(self):
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump({"archive": self._path_text(self.archive_var),
                           "source": self._path_text(self.source_var),
                           "output": self._path_text(self.output_var),
                           "mode": self.mode_var.get(),
                           "copy_mode": self.copy_var.get(),
                           "workers": self.workers_var.get()},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _append_logs(self, chunks):
        if not chunks:
            return
        text = "\n".join(chunks) + "\n"
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log_lines += text.count("\n")
        if self.log_lines > LOG_MAX_LINES:
            self.log.delete("1.0", "%d.0" % LOG_TRIM_LINES)
            self.log_lines -= LOG_TRIM_LINES
        self.log.see("end")
        self.log.configure(state="disabled")

    def _check_paths(self, a, s, o):
        if not a or not os.path.isdir(a):
            messagebox.showwarning("提示", "请选择有效的归档文件夹。")
            return False
        if not s or not os.path.isdir(s):
            messagebox.showwarning("提示", "请选择有效的原素材文件夹。")
            return False
        if not o:
            messagebox.showwarning("提示", "请选择未归档补充输出目录。")
            return False
        if same_path(a, s):
            messagebox.showerror("错误", "归档文件夹和原素材文件夹不能是同一个。")
            return False
        if same_path(o, s):
            messagebox.showerror("错误", "输出目录不能等于原素材文件夹。")
            return False
        if is_inside(o, a):
            messagebox.showerror(
                "错误", "输出目录不能是归档文件夹，也不能位于归档文件夹内部。")
            return False
        if is_inside(a, o):
            messagebox.showerror("错误", "输出目录不能包含归档文件夹。")
            return False
        if os.path.exists(o) and not os.path.isdir(o):
            messagebox.showerror("错误", "输出路径已存在，但不是文件夹:\n" + o)
            return False
        return True

    def verify(self):
        if self.running:
            return
        a = self._path_text(self.archive_var)
        s = self._path_text(self.source_var)
        o = self._path_text(self.output_var)
        if not self._check_paths(a, s, o):
            return
        mode = self.mode_var.get()
        self.copy_summary = None
        self.last_run = (a, s, o, mode)

        self._save_config()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.log_lines = 0
        self.progress["value"] = 0
        self.status_var.set("复查中...")
        self.stop_event.clear()
        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_verify.config(state="disabled")
        self.btn_cancel.config(state="normal")

        self.thread = threading.Thread(
            target=verify_worker,
            args=(a, s, o, mode, self.verbose_var.get(),
                  self.queue, self.stop_event),
            daemon=True)
        self.thread.start()

    def start(self):
        if self.running:
            return
        a = self._path_text(self.archive_var)
        s = self._path_text(self.source_var)
        o = self._path_text(self.output_var)
        mode = self.mode_var.get()
        copy_mode = self.copy_var.get()
        try:
            workers = int(self.workers_var.get().split()[0])
        except Exception:
            workers = 1

        if not self._check_paths(a, s, o):
            return
        if copy_mode == COPY_MODE_MOVE and not self.dry_var.get():
            if not messagebox.askyesno(
                    "确认移动",
                    "同盘移动会从原素材文件夹移除未归档文件。\n"
                    "移动后原素材里只剩已归档的重复文件。\n\n"
                    "确定继续？"):
                return
        if not self.dry_var.get():
            if not os.path.isdir(o):
                if not messagebox.askyesno(
                        "新建输出目录",
                        "输出目录不存在，将新建：\n%s\n\n是否继续？" % o):
                    return
                try:
                    os.makedirs(o, exist_ok=True)
                except OSError as e:
                    messagebox.showerror("错误", "无法创建输出目录:\n" + str(e))
                    return
            try:
                existing = [x for x in os.listdir(o) if not x.startswith(".")]
            except OSError as e:
                messagebox.showerror("错误", "无法读取输出目录:\n" + str(e))
                return
            if existing:
                if not messagebox.askyesno(
                        "确认", "输出目录已存在且不为空，继续可能混入旧文件。\n是否继续？"):
                    return

        self._save_config()
        self.copy_summary = None
        self.last_run = (a, s, o, mode)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.log_lines = 0
        self.progress["value"] = 0
        self.status_var.set("运行中...")
        self.stop_event.clear()
        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_verify.config(state="disabled")
        self.btn_cancel.config(state="normal")

        self.thread = threading.Thread(
            target=worker,
            args=(a, s, o, self.dry_var.get(), self.verbose_var.get(),
                  mode, copy_mode, workers, self.queue, self.stop_event),
            daemon=True)
        self.thread.start()

    def cancel(self):
        if self.running:
            self.stop_event.set()
            self.status_var.set("正在取消...")

    def _finish_task(self):
        self.running = False
        self.btn_start.config(state="normal")
        self.btn_verify.config(state="normal")
        self.btn_cancel.config(state="disabled")

    def _begin_auto_verify(self, payload):
        self.copy_summary = payload
        if not self.last_run:
            self._finish_task()
            self._finish_copy(payload)
            return
        a, s, o, mode = self.last_run
        self._append_logs(["", "处理完成，开始自动复查..."])
        self.status_var.set("处理完成，正在自动复查...")
        self.progress["value"] = 0
        self.stop_event.clear()
        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_verify.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.thread = threading.Thread(
            target=verify_worker,
            args=(a, s, o, mode, self.verbose_var.get(),
                  self.queue, self.stop_event),
            daemon=True)
        self.thread.start()

    def _finish_copy(self, payload):
        self.progress["value"] = 100
        failed = int(payload.get("failed", 0))
        self.status_var.set("完成(有失败)" if failed else "完成")
        if payload.get("copy_mode") == COPY_MODE_MOVE:
            title = "完成(已移动)"
            body = ("已归档(跳过): %d\n未归档: %d\n已移动: %d\n失败: %d\n\n"
                    "原素材中的未归档文件已移动到补充目录。\n"
                    "原素材文件夹现在只剩已归档的重复文件，验证后可整个删除。\n\n"
                    "上传网盘只需上传:\n1) 归档文件夹\n2) 未归档补充输出目录\n\n"
                    "是否打开输出目录？")
        else:
            title = "完成(已复制)"
            body = ("已归档(跳过): %d\n未归档: %d\n已复制: %d\n失败: %d\n\n"
                    "上传网盘只需上传:\n1) 归档文件夹\n2) 未归档补充输出目录\n\n"
                    "是否打开输出目录？")
        if failed:
            title = "完成(有失败)"
            body = ("注意: 有 %d 个文件处理失败，详见日志。\n\n" % failed) + body
        if messagebox.askyesno(
                title,
                body % (payload["archived"], payload["unique"],
                        payload["copied"], failed)):
            out = payload.get("output")
            if out and os.path.isdir(out):
                os.startfile(out)

    def _finish_verify(self, payload):
        ok = bool(payload.get("ok"))
        self.progress["value"] = 100 if ok else 0
        self.status_var.set("复查通过" if ok else "复查未通过")

        body = ""
        copy_summary = self.copy_summary
        if copy_summary:
            body += ("复制/移动完成：已归档 %d，未归档 %d，成功 %d，失败 %d。\n\n"
                     % (int(copy_summary.get("archived", 0)),
                        int(copy_summary.get("unique", 0)),
                        int(copy_summary.get("copied", 0)),
                        int(copy_summary.get("failed", 0))))
        body += ("已归档(跳过): %d\n未归档(应有): %d\n输出中匹配: %d\n\n"
                 "缺失: %d\n内容不一致: %d\n多余文件: %d\n.part 残留: %d\n读取失败: %d\n"
                 % (int(payload.get("archived", 0)),
                    int(payload.get("unused", 0)),
                    int(payload.get("verified", 0)),
                    int(payload.get("missing", 0)),
                    int(payload.get("mismatch", 0)),
                    int(payload.get("extra", 0)),
                    int(payload.get("part", 0)),
                    int(payload.get("errors", 0))))
        if int(payload.get("no_manifest", 0)):
            body += "\n缺少复查清单，且原素材中已无未归档文件，无法判断完整性。\n"

        report = payload.get("report") or ""
        if ok:
            body += "\n未归档文件已全部在输出目录中，内容一致。\n"
            if report:
                body += "复查报告:\n" + report + "\n"
            elif int(payload.get("unused", 0)) == 0:
                body += "没有未归档文件，未生成复查报告。\n"
            else:
                body += "复查报告写入失败，详见日志。\n"
            body += "\n是否打开输出目录？"
            if messagebox.askyesno("复查通过", body):
                out = payload.get("output")
                if out and os.path.isdir(out):
                    os.startfile(out)
        else:
            body += "\n详见日志中的 [缺失] / [不一致] / [多余] / [.part] 条目。"
            if report:
                body += "\n复查报告: " + report
            messagebox.showwarning("复查未通过", body)
        self.copy_summary = None

    def poll(self):
        logs = []
        done_payload = None
        error_payload = None
        last_status = None
        last_pct = None
        count = 0
        while count < 600:
            try:
                kind, payload = self.queue.get_nowait()
            except queue.Empty:
                break
            count += 1
            if kind == "logs":
                logs.append(payload)
            elif kind == "progress":
                last_pct, last_status = payload
            elif kind == "done":
                done_payload = payload
            elif kind == "error":
                error_payload = payload

        if logs:
            self._append_logs(logs)
        if last_pct is not None:
            self.progress["value"] = last_pct
        if last_status is not None:
            self.status_var.set(last_status)

        if done_payload is not None:
            if done_payload.get("cancelled"):
                self._finish_task()
                self.copy_summary = None
                self.progress["value"] = 0
                self.status_var.set("已取消")
                messagebox.showinfo(
                    "已取消",
                    "任务已取消。\n"
                    "已处理的文件保留在输出目录；复制模式下原素材未受影响。")
            elif done_payload.get("verify"):
                self._finish_task()
                self._finish_verify(done_payload)
            elif done_payload.get("dry"):
                self._finish_task()
                self.progress["value"] = 100
                self.status_var.set("试运行完成")
                messagebox.showinfo(
                    "试运行完成",
                    "已归档(跳过): %d\n未归档(需上传): %d\n\n"
                    "取消勾选'试运行'后再执行，才会真正处理文件。"
                    % (done_payload["archived"], done_payload["unique"]))
            else:
                if self.auto_verify_var.get() and self.last_run is not None:
                    self._begin_auto_verify(done_payload)
                else:
                    self._finish_task()
                    self._finish_copy(done_payload)

        if error_payload is not None:
            self._finish_task()
            self.copy_summary = None
            self.status_var.set("出错")
            self._append_logs(["错误: " + error_payload])
            messagebox.showerror("出错", error_payload)

        self.root.after(50, self.poll)

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("确认退出",
                                       "任务正在运行，退出可能留下不完整的文件。\n确定退出？"):
                return
            self.stop_event.set()
        self._save_config()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()