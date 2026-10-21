import os
import sys
import json
import time
import hashlib
import shutil
import threading
import queue
import ctypes
import webbrowser
from ctypes import wintypes
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, scrolledtext

CHUNK_SIZE = 1024 * 1024
SAMPLE_SIZE = 64 * 1024
CONFIG_NAME = "archive_dedup_config.json"
MANIFEST_NAME = "_archive_dedup_manifest.json"
REPORT_NAME = "归档完整性校验报告.txt"
LEGACY_REPORT_NAMES = ("复查报告.txt",)
IGNORED_OUTPUT_NAMES = (MANIFEST_NAME, REPORT_NAME) + LEGACY_REPORT_NAMES
IGNORED_OUTPUT_NAMES_LOWER = {name.lower() for name in IGNORED_OUTPUT_NAMES}
APP_NAME = "素材归档去重工具"
APP_VERSION = "1.3.0"
PROJECT_URL = "https://github.com/GSJNZH/archive-dedup"
DEFAULT_OUTPUT_NAME = "09_04-未使用画面"
LOG_MAX_LINES = 5000
LOG_TRIM_LINES = 2000

UI_BG = "#f5f5f7"
UI_CARD = "#ffffff"
UI_BORDER = "#e5e5ea"
UI_TEXT = "#1d1d1f"
UI_MUTED = "#6e6e73"
UI_ACCENT = "#0071e3"
UI_ACCENT_DARK = "#0077ed"
UI_FIELD = "#f2f2f7"
UI_GREEN = "#34c759"
UI_SHADOW = "#e8e8ed"
UI_LOG_BG = "#fbfbfd"
UI_LOG_FG = "#1d1d1f"


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def enable_dpi_awareness():
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

MODE_TURBO = "极速(大小+修改时间,不读内容)"
MODE_FAST = "快速(采样哈希,推荐)"
MODE_EXACT = "精确(全量MD5)"
MODES = [MODE_FAST, MODE_TURBO, MODE_EXACT]

COPY_MODE_COPY = "复制(安全,保留原素材)"
COPY_MODE_MOVE = "同盘移动(瞬间,会移除原素材中的文件)"
COPY_MODES = [COPY_MODE_COPY, COPY_MODE_MOVE]

WORKER_CHOICES = ["1 (机械盘推荐)", "2", "4", "8 (SSD可试)"]

IS_WINDOWS = (os.name == "nt")


def _round_points(x1, y1, x2, y2, radius):
    radius = max(1, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]


class RoundedPanel(tk.Canvas):
    """白色圆角卡片，内部放一个 tk.Frame。"""

    def __init__(self, parent, height=180, radius=18, padding=20):
        super().__init__(parent, height=height, bg=UI_BG,
                         highlightthickness=0, bd=0)
        self.radius = radius
        self.padding = padding
        self.inner = tk.Frame(self, bg=UI_CARD)
        self._window = self.create_window(padding, padding, anchor="nw",
                                          window=self.inner)
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event=None):
        width = max(self.winfo_width(), 10)
        height = max(self.winfo_height(), 10)
        self.delete("panel")
        self.create_polygon(_round_points(1, 3, width - 1, height - 1, self.radius),
                            smooth=True, splinesteps=36,
                            fill=UI_SHADOW, outline="", tags="panel")
        self.create_polygon(_round_points(1, 1, width - 1, height - 3, self.radius),
                            smooth=True, splinesteps=36,
                            fill=UI_CARD, outline=UI_BORDER, width=1,
                            tags="panel")
        self.tag_lower("panel")
        self.itemconfigure(self._window,
                           width=max(20, width - self.padding * 2),
                           height=max(20, height - self.padding * 2))


class RoundedButton(tk.Canvas):
    """扁平圆角按钮，兼容 Tk 的 config(state=...)。"""

    def __init__(self, parent, text, command, kind="primary",
                 width=120, height=38, font=None):
        try:
            bg = parent.cget("bg")
        except Exception:
            bg = UI_CARD
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._text = text
        self._command = command
        self._kind = kind
        self._bw = width
        self._bh = height
        self._font = font or ("Segoe UI", 10)
        self._state = "normal"
        self._hover = False
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._draw()

    def _palette(self):
        if self._state == "disabled":
            if self._kind == "primary":
                return "#e5e5ea", "#8e8e93", ""
            if self._kind == "ghost":
                return "", "#aeaeb2", ""
            return UI_FIELD, "#c7c7cc", ""
        if self._kind == "primary":
            return (UI_ACCENT_DARK if self._hover else UI_ACCENT), "#ffffff", ""
        if self._kind == "secondary":
            return ("#e8e8ed" if self._hover else UI_FIELD), UI_TEXT, ""
        return "", (UI_ACCENT if self._hover else UI_MUTED), ""

    def _draw(self):
        self.delete("all")
        fill, fg, outline = self._palette()
        if fill:
            self.create_polygon(_round_points(1, 1, self._bw - 1, self._bh - 1, 10),
                                smooth=True, splinesteps=24,
                                fill=fill, outline=outline, width=1)
        self.create_text(self._bw / 2, self._bh / 2, text=self._text,
                         fill=fg, font=self._font)

    def _on_click(self, event):
        if self._state != "disabled" and self._command:
            self._command()

    def _on_enter(self, event):
        self._hover = True
        self._draw()

    def _on_leave(self, event):
        self._hover = False
        self._draw()

    def set_state(self, state):
        self._state = state
        self.configure(cursor="hand2" if state != "disabled" else "arrow")
        self._draw()

    def set_text(self, text):
        self._text = text
        self._draw()

    def config(self, **kwargs):
        if "state" in kwargs:
            self.set_state(kwargs.pop("state"))
        if "text" in kwargs:
            self.set_text(kwargs.pop("text"))
        if kwargs:
            super().config(**kwargs)

    configure = config


class ToggleSwitch(tk.Canvas):
    def __init__(self, parent, variable, command=None):
        super().__init__(parent, width=44, height=24, bg=UI_CARD,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.variable = variable
        self.command = command
        self.variable.trace_add("write", lambda *args: self._draw())
        self.bind("<Button-1>", lambda event: self.toggle())
        self._draw()

    def toggle(self):
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()

    def _draw(self):
        self.delete("all")
        on = bool(self.variable.get())
        fill = UI_GREEN if on else "#e5e5ea"
        self.create_polygon(_round_points(1, 2, 43, 22, 10),
                            smooth=True, splinesteps=24,
                            fill=fill, outline="")
        x = 31 if on else 13
        self.create_oval(x - 9, 3, x + 9, 21, fill="#ffffff", outline="")


class PillEntry(tk.Canvas):
    def __init__(self, parent, textvariable, font):
        super().__init__(parent, height=38, bg=UI_CARD,
                         highlightthickness=0, bd=0)
        self._font = font
        self._focused = False
        self.entry = tk.Entry(self, textvariable=textvariable,
                              bg=UI_FIELD, fg=UI_TEXT, relief="flat",
                              bd=0, highlightthickness=0,
                              insertbackground=UI_TEXT,
                              selectbackground="#d2e3fc",
                              selectforeground=UI_TEXT,
                              font=font)
        self._window = self.create_window(12, 19, anchor="w",
                                          window=self.entry, width=100, height=24)
        self.bind("<Configure>", self._redraw)
        self.bind("<Button-1>", lambda event: self.entry.focus_set())
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)

    def _redraw(self, event=None):
        width = max(self.winfo_width(), 10)
        height = max(self.winfo_height(), 10)
        self.delete("bg")
        self.create_polygon(_round_points(1, 1, width - 1, height - 1, 10),
                            smooth=True, splinesteps=24,
                            fill=UI_FIELD,
                            outline=(UI_ACCENT if self._focused else UI_FIELD),
                            width=1, tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._window, width=max(20, width - 24), height=24)

    def _on_focus_in(self, event):
        self._focused = True
        self._redraw()

    def _on_focus_out(self, event):
        self._focused = False
        self._redraw()


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
        # 单个指纹由 大小 + 头 64KB MD5 + 尾 64KB MD5 组合而成。
        # 不额外读盘，但能避免“头相同”看起来像同一个文件。
        raw = "%d|%s|%s" % (key[0], key[1], key[2])
        return "fp=" + hashlib.md5(raw.encode("utf-8")).hexdigest()
    return "md5=" + key[1]


def write_verify_report(output_dir, source_dir, archive_dir, mode, stats, expected):
    """校验通过后，在输出目录写一份可读的 TXT 报告。"""
    project = os.path.basename(os.path.normpath(source_dir)) or "未命名项目"
    digest = hashlib.sha256()
    for rel in sorted(expected):
        size, key = expected[rel]
        digest.update(("%s\t%d\t%s\n"
                       % (rel, size, report_fingerprint(key, mode))).encode("utf-8"))
    lines = [
        "素材归档去重 · 归档完整性校验报告",
        "=" * 60,
        "项目名称: " + project,
        "软件版本: %s v%s" % (APP_NAME, APP_VERSION),
        "GitHub 仓库: " + PROJECT_URL,
        "校验时间: " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "校验结果: 通过",
        "",
        "[目录]",
        "归档目录: " + archive_dir,
        "原素材目录: " + source_dir,
        "输出目录: " + output_dir,
        "处理方式: " + stats.get("copy_mode", "未知"),
        "",
        "[校验统计]",
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
        "[文件校验清单]",
        "序号\t相对路径\t大小\t指纹",
    ]
    for index, rel in enumerate(sorted(expected), 1):
        size, key = expected[rel]
        safe_rel = rel.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        lines.append("%d\t%s\t%s\t%s"
                     % (index, safe_rel, fmt_size(size), report_fingerprint(key, mode)))

    if mode == MODE_FAST:
        fingerprint_note = ("快速指纹 = MD5(大小|头64KB MD5|尾64KB MD5)；"
                            "头相同、尾不同也会得到不同值。")
    elif mode == MODE_EXACT:
        fingerprint_note = "精确模式 = 文件全量 MD5。"
    else:
        fingerprint_note = "极速模式 = 文件大小 + 修改时间，不读取内容，不含内容哈希。"

    lines += [
        "",
        "[校验说明]",
        "比对模式: " + mode,
        "指纹说明: " + fingerprint_note,
        "本报告由 %s v%s 在校验通过后自动生成；后续校验时会忽略本文件。"
        % (APP_NAME, APP_VERSION),
        "",
    ]

    path = os.path.join(output_dir, REPORT_NAME)
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)
        for legacy in LEGACY_REPORT_NAMES:
            old = os.path.join(output_dir, legacy)
            if os.path.isfile(old):
                try:
                    os.remove(old)
                except OSError:
                    pass
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

        for report_name in (REPORT_NAME,) + LEGACY_REPORT_NAMES:
            stale_report = os.path.join(output_dir, report_name)
            try:
                if os.path.isfile(stale_report):
                    os.remove(stale_report)
                    emitter.log("已清除旧校验报告，等待本次校验后重新生成。")
            except OSError as e:
                emitter.log("警告: 无法清除旧校验报告: " + str(e))
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
            if rel.lower() in IGNORED_OUTPUT_NAMES_LOWER:
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
        root.title("%s v%s" % (APP_NAME, APP_VERSION))
        root.geometry("1080x900")
        root.minsize(1000, 760)
        icon = resource_path("assets/app.ico")
        if os.path.isfile(icon):
            try:
                root.iconbitmap(icon)
            except tk.TclError:
                pass

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

        self.root.update_idletasks()
        width, height = 1080, 900
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self.poll)

    def _setup_styles(self):
        families = set(tkfont.families(self.root))
        if "Microsoft YaHei UI" in families:
            self.font_family = "Microsoft YaHei UI"
        elif "Segoe UI" in families:
            self.font_family = "Segoe UI"
        else:
            self.font_family = "TkDefaultFont"
        if "Cascadia Mono" in families:
            self.mono_family = "Cascadia Mono"
        elif "Consolas" in families:
            self.mono_family = "Consolas"
        else:
            self.mono_family = "Courier New"

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        ff = self.font_family
        style.configure(".", background=UI_BG, foreground=UI_TEXT, font=(ff, 10))
        style.configure("Apple.TCombobox", fieldbackground=UI_FIELD,
                        background=UI_FIELD, foreground=UI_TEXT,
                        bordercolor=UI_FIELD, lightcolor=UI_FIELD,
                        darkcolor=UI_FIELD, arrowcolor=UI_MUTED,
                        padding=6, relief="flat")
        style.map("Apple.TCombobox",
                  fieldbackground=[("readonly", UI_FIELD), ("active", "#ececf1")],
                  selectbackground=[("readonly", UI_FIELD)],
                  selectforeground=[("readonly", UI_TEXT)],
                  arrowcolor=[("active", UI_ACCENT)])
        style.configure("Apple.Horizontal.TProgressbar",
                        troughcolor="#e5e5ea", background=UI_ACCENT,
                        bordercolor="#e5e5ea", lightcolor=UI_ACCENT,
                        darkcolor=UI_ACCENT, thickness=5)
        style.configure("Vertical.TScrollbar", background="#d1d1d6",
                        troughcolor=UI_BG, bordercolor=UI_BG,
                        arrowcolor="#8e8e93")
        self.root.option_add("*TCombobox*Listbox.background", UI_CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", UI_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", UI_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _build_ui(self):
        self.root.configure(bg=UI_BG)
        self._setup_styles()
        ff = self.font_family

        header = tk.Frame(self.root, bg=UI_BG)
        header.pack(fill="x", padx=30, pady=(24, 6))
        logo_path = resource_path("assets/app.png")
        if os.path.isfile(logo_path):
            try:
                self.logo_image = tk.PhotoImage(file=logo_path).subsample(4, 4)
                tk.Label(header, image=self.logo_image, bg=UI_BG).pack(
                    side="left", padx=(0, 14))
            except tk.TclError:
                pass
        title_box = tk.Frame(header, bg=UI_BG)
        title_box.pack(side="left")
        tk.Label(title_box, text="素材归档去重", bg=UI_BG, fg=UI_TEXT,
                 font=(ff, 26, "bold")).pack(anchor="w")
        tk.Label(title_box, text="按内容指纹筛出未归档素材，自动校验并生成报告",
                 bg=UI_BG, fg=UI_MUTED, font=(ff, 10)).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(header, bg=UI_BG)
        right.pack(side="right")
        tk.Label(right, text="v%s" % APP_VERSION, bg=UI_BG, fg=UI_MUTED,
                 font=(ff, 10)).pack(anchor="e")
        link = tk.Label(right, text="GitHub  ↗", bg=UI_BG, fg=UI_ACCENT,
                        font=(ff, 10), cursor="hand2")
        link.pack(anchor="e", pady=(3, 0))
        link.bind("<Button-1>", lambda event: webbrowser.open(PROJECT_URL))
        link.bind("<Enter>", lambda e: link.configure(font=(ff, 10, "underline")))
        link.bind("<Leave>", lambda e: link.configure(font=(ff, 10)))

        body = tk.Frame(self.root, bg=UI_BG)
        body.pack(fill="both", expand=True, padx=30, pady=(6, 20))

        dir_card = RoundedPanel(body, height=282, padding=22)
        dir_card.pack(fill="x", pady=(0, 14))
        dir_inner = dir_card.inner
        dir_inner.grid_columnconfigure(1, weight=1)
        self._section_header(dir_inner, "选择目录",
                             "归档工程、原素材与未归档输出位置", 0)
        self._add_path_row(dir_inner, 1, "归档文件夹", self.archive_var,
                           self._pick_archive, self._open_archive)
        self._add_path_row(dir_inner, 2, "原素材文件夹", self.source_var,
                           self._pick_source, self._open_source)
        self._add_path_row(dir_inner, 3, "输出文件夹", self.output_var,
                           self._pick_output, self._open_output)
        tk.Label(dir_inner, text="默认输出：<原素材文件夹>\\09_04-未使用画面",
                 bg=UI_CARD, fg=UI_MUTED, font=(ff, 9)).grid(
            row=4, column=1, columnspan=3, sticky="w", pady=(4, 0))

        opt_card = RoundedPanel(body, height=222, padding=22)
        opt_card.pack(fill="x", pady=(0, 14))
        opt = opt_card.inner
        self._section_header(opt, "执行选项",
                             "选择比对强度、处理方式与运行行为", 0, 3)

        row1 = tk.Frame(opt, bg=UI_CARD)
        row1.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        tk.Label(row1, text="比对模式", bg=UI_CARD, fg=UI_MUTED,
                 font=(ff, 9)).pack(side="left")
        ttk.Combobox(row1, textvariable=self.mode_var, values=MODES,
                     state="readonly", width=22,
                     style="Apple.TCombobox").pack(side="left", padx=(8, 20))
        tk.Label(row1, text="处理方式", bg=UI_CARD, fg=UI_MUTED,
                 font=(ff, 9)).pack(side="left")
        ttk.Combobox(row1, textvariable=self.copy_var, values=COPY_MODES,
                     state="readonly", width=29,
                     style="Apple.TCombobox").pack(side="left", padx=(8, 20))
        tk.Label(row1, text="并发", bg=UI_CARD, fg=UI_MUTED,
                 font=(ff, 9)).pack(side="left")
        ttk.Combobox(row1, textvariable=self.workers_var, values=WORKER_CHOICES,
                     state="readonly", width=14,
                     style="Apple.TCombobox").pack(side="left", padx=(8, 0))

        row2 = tk.Frame(opt, bg=UI_CARD)
        row2.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        self.dry_var = tk.BooleanVar(value=True)
        self.verbose_var = tk.BooleanVar(value=True)
        self.auto_verify_var = tk.BooleanVar(value=True)
        self._add_toggle(row2, "试运行", self.dry_var)
        self._add_toggle(row2, "详细日志", self.verbose_var)
        self._add_toggle(row2, "完成后自动校验", self.auto_verify_var)

        self.btn_cancel = RoundedButton(row2, "取消", self.cancel, kind="ghost",
                                        width=70, height=38, font=(ff, 10))
        self.btn_cancel.set_state("disabled")
        self.btn_cancel.pack(side="right", padx=(8, 0))
        self.btn_verify = RoundedButton(row2, "校验输出", self.verify,
                                        kind="secondary", width=104, height=38,
                                        font=(ff, 10))
        self.btn_verify.pack(side="right", padx=(8, 0))
        self.btn_start = RoundedButton(row2, "开始处理", self.start, kind="primary",
                                       width=112, height=38,
                                       font=(ff, 10, "bold"))
        self.btn_start.pack(side="right")

        status = tk.Frame(body, bg=UI_BG)
        status.pack(fill="x", pady=(0, 10))
        self.progress = ttk.Progressbar(status, maximum=100,
                                        style="Apple.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(status, textvariable=self.status_var, bg=UI_BG, fg=UI_MUTED,
                 font=(ff, 9), width=40, anchor="e").pack(
            side="left", padx=(14, 0))

        log_card = RoundedPanel(body, height=120, padding=18)
        log_card.pack(fill="both", expand=True)
        log_inner = log_card.inner
        head = tk.Frame(log_inner, bg=UI_CARD)
        head.pack(fill="x")
        tk.Label(head, text="运行日志", bg=UI_CARD, fg=UI_TEXT,
                 font=(ff, 14, "bold")).pack(side="left")
        tk.Label(head, text="实时输出", bg=UI_CARD, fg=UI_MUTED,
                 font=(ff, 9)).pack(side="right")
        self.log = scrolledtext.ScrolledText(
            log_inner, height=10, wrap="none", font=(self.mono_family, 9),
            bg=UI_LOG_BG, fg=UI_LOG_FG, insertbackground=UI_TEXT,
            relief="flat", borderwidth=0, padx=12, pady=10,
            highlightthickness=1, highlightbackground="#ececf1",
            highlightcolor="#ececf1")
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.log.configure(state="disabled")
        self._append_logs([
            "素材归档去重工具 v%s 已就绪。" % APP_VERSION,
            "选择三个目录后，先“试运行”确认清单，再开始处理。",
            "处理完成后会自动校验，并在输出目录生成校验报告。",
        ])

    def _section_header(self, parent, title, subtitle, row=0, columns=4):
        box = tk.Frame(parent, bg=UI_CARD)
        box.grid(row=row, column=0, columnspan=columns, sticky="ew",
                 pady=(0, 10))
        tk.Label(box, text=title, bg=UI_CARD, fg=UI_TEXT,
                 font=(self.font_family, 15, "bold")).pack(anchor="w")
        tk.Label(box, text=subtitle, bg=UI_CARD, fg=UI_MUTED,
                 font=(self.font_family, 9)).pack(anchor="w", pady=(2, 0))

    def _add_path_row(self, parent, row, label, var, on_pick, on_open):
        tk.Label(parent, text=label, bg=UI_CARD, fg=UI_MUTED,
                 font=(self.font_family, 9), width=10, anchor="w").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        field = PillEntry(parent, var, (self.font_family, 10))
        field.grid(row=row, column=1, sticky="ew", pady=5)
        browse = RoundedButton(parent, "浏览", on_pick, kind="secondary",
                               width=66, height=34,
                               font=(self.font_family, 9))
        browse.grid(row=row, column=2, padx=(10, 8), pady=5)
        link = tk.Label(parent, text="打开", bg=UI_CARD, fg=UI_ACCENT,
                        font=(self.font_family, 9), cursor="hand2")
        link.grid(row=row, column=3, sticky="w", pady=5)
        link.bind("<Button-1>", lambda event: on_open())
        link.bind("<Enter>", lambda e: link.configure(
            font=(self.font_family, 9, "underline")))
        link.bind("<Leave>", lambda e: link.configure(
            font=(self.font_family, 9)))

    def _add_toggle(self, parent, text, variable):
        box = tk.Frame(parent, bg=UI_CARD)
        box.pack(side="left", padx=(0, 18))
        switch = ToggleSwitch(box, variable)
        switch.pack(side="left")
        label = tk.Label(box, text=text, bg=UI_CARD, fg=UI_TEXT,
                         font=(self.font_family, 9), cursor="hand2")
        label.pack(side="left", padx=(6, 0))
        label.bind("<Button-1>", lambda event: switch.toggle())

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
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()