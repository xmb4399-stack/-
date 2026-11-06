# -*- coding: utf-8 -*-
"""
elder_core.py —— 老年视频播放器 · 核心逻辑层

从原 PyQt5 版本（老年人播放器 美观版.py）移植而来。
这一层刻意不依赖任何界面库，Windows / Linux / Android 都可以直接跑。

移植时保留的原有行为：
  * 「主文件夹 -> 分类 -> 剧集 -> 视频」四层目录结构
  * 播放满 3 分钟才写入历史记录，续播时回退 10 秒
  * 设置项与播放记录以 JSON 形式落盘

移植时做的必要改动：
  * 配置目录从「视频主文件夹内的 .player_config」改为应用私有目录。
    因为安卓 11+ 的存储沙箱不允许应用往 /sdcard 根目录写文件。
  * 找不到「桌面」，改为手机存储里的 Movies/老人视频。
  * 文件排序改为「自然排序」：第 2 集会排在 第 10 集 前面（原版按字符串排，
    第 10 集反而更靠前，看连续剧时顺序是乱的）。
  * 分类文件夹下如果直接堆了视频（没有剧集子文件夹），也会被识别出来。
    原版遇到这种情况会显示「没有任何分类」。
"""

import os
import re
import json
import shutil
from pathlib import Path

from kivy.utils import platform

IS_ANDROID = (platform == 'android')

# ---------- 常量 ----------
APP_NAME = '老年视频播放器'
DEFAULT_ROOT_NAME = '老人视频'

CONFIG_DIR_NAME = '.player_config'
SETTINGS_FILE = 'settings.json'
STATE_FILE = 'playback_state.json'
ROOT_PATH_FILE = 'last_root_path.txt'
THUMBNAIL_DIR = 'thumbnails'
ICON_DIR = 'home_icons'

# 主页三个大按钮（前两个对应视频文件夹，第三个是虚拟分类）
CAT_HUANGMEI = '戏曲'
CAT_TV = '电视剧'
CAT_HISTORY = '历史播放'
FOLDER_CATEGORIES = (CAT_HUANGMEI, CAT_TV)

# 分类文件夹的旧名字 —— 老用户的文件夹还叫「黄梅戏」，不能因为改了按钮名就读不到。
# 扫描时这些名字会被归到对应分类里（见 scan_categories）。
CAT_FOLDER_ALIASES = {
    '黄梅戏': CAT_HUANGMEI,
    CAT_HUANGMEI: CAT_HUANGMEI,
    CAT_TV: CAT_TV,
}

# 所有会被扫描到的视频后缀
ALL_VIDEO_EXT = ('.mp4', '.m4v', '.mkv', '.mov', '.avi', '.webm', '.3gp',
                 '.ts', '.flv', '.wmv', '.rmvb', '.rm', '.mpg', '.mpeg',
                 '.vob', '.f4v', '.m2ts', '.mts')

# 安卓「系统播放器」（MediaCodec 硬解）能稳定播放的后缀。
# rmvb / rm / flv / wmv / avi 这些老格式它放不了 —— 这是系统限制。
#
# 不过应用还自带了一个可选的 libVLC 内核（见 elder_vlc.py），
# 那个能放这些老格式。所以这个清单只在「libVLC 不可用或被关掉」时
# 才生效，由 can_play(with_vlc=...) 决定用哪一套判断。
ANDROID_SAFE_EXT = ('.mp4', '.m4v', '.mkv', '.webm', '.3gp', '.ts', '.mov')

# 断点续播
MIN_RESUME_DURATION = 180 * 1000   # 播放超过 3 分钟才记录进度
RESUME_OFFSET = 10 * 1000          # 续播时回退 10 秒，避免卡在片尾

# 打开 App 时的屏幕方向（设置页可以改）
ORIENTATION_SETTING = 'orientation'
ORIENTATION_LANDSCAPE = 'landscape'
ORIENTATION_PORTRAIT = 'portrait'

# 主页三个入口的自定义图标：key（存文件名用，纯英文） -> 分类名
HOME_ICON_KEYS = (
    ('xiqu', CAT_HUANGMEI),
    ('tv', CAT_TV),
    ('history', CAT_HISTORY),
)

# 安卓上视频主文件夹的候选位置（按优先级）
ANDROID_ROOT_CANDIDATES = (
    '/storage/emulated/0/Movies/' + DEFAULT_ROOT_NAME,
    '/storage/emulated/0/' + DEFAULT_ROOT_NAME,
    '/storage/emulated/0/Videos/' + DEFAULT_ROOT_NAME,
    '/storage/emulated/0/Download/' + DEFAULT_ROOT_NAME,
    '/sdcard/Movies/' + DEFAULT_ROOT_NAME,
    '/sdcard/' + DEFAULT_ROOT_NAME,
)

# 安卓「/sdcard」只是个软链接，统一用真实路径，省得权限判断出岔子
ANDROID_SDCARD = '/storage/emulated/0'

# 系统文件夹的中文叫法。
# 手机和电脑上这些都是英文（DCIM / Pictures / Download…），老人看不懂，
# 图片选择器里一律显示「中文（原名）」，既看得懂又能跟文件管理器对上。
FOLDER_CN = {
    'dcim': '相机相册',
    'camera': '相机',
    'pictures': '图片',
    'picture': '图片',
    'photos': '照片',
    'screenshots': '截屏',
    'screenshot': '截屏',
    'download': '下载',
    'downloads': '下载',
    'documents': '文档',
    'document': '文档',
    'movies': '视频',
    'movie': '视频',
    'videos': '视频',
    'video': '视频',
    'music': '音乐',
    'audio': '录音',
    'desktop': '桌面',
    'sdcard': '手机存储',
    'storage': '手机存储',
    'emulated': '手机存储',
    'android': '安卓系统数据',
    'data': '应用数据',
    'users': '用户',
    'public': '公共',
    'weixin': '微信',
    'micromsg': '微信',
    'tencent': '腾讯',
    'bluetooth': '蓝牙接收',
    'whatsapp': 'WhatsApp',
    'icon': '图标',
    'icons': '图标',
    'image': '图片',
    'images': '图片',
    'img': '图片',
    'pic': '图片',
    'pics': '图片',
}


def folder_label(path):
    """
    文件夹在界面上叫什么。

    认识的名字显示成「相机相册（DCIM）」，不认识的原样返回。
    根目录那种叫「0」的（/storage/emulated/0）也给个正常名字。
    """
    path = Path(path)
    name = path.name or str(path)
    if name == '0' and 'emulated' in str(path).replace('\\', '/').lower():
        return '手机存储'
    cn = FOLDER_CN.get(name.lower())
    if cn and cn != name:
        return '%s（%s）' % (cn, name)
    return name


def photo_quick_dirs():
    """
    图片选择器顶部的快捷入口：[(中文名, Path), ...]，只返回真实存在的。

    有了这几个按钮，老人不用一层层翻英文目录名，
    点「相机相册」就直接到手机照片那层。
    """
    home = Path.home()
    if IS_ANDROID:
        sdcard = Path(ANDROID_SDCARD)
        candidates = [
            ('相机相册', sdcard / 'DCIM' / 'Camera'),
            ('手机相册', sdcard / 'Pictures'),
            ('下载', sdcard / 'Download'),
            ('手机存储', sdcard),
        ]
    else:
        candidates = [
            ('桌面', home / 'Desktop'),
            ('图片', home / 'Pictures'),
            ('下载', home / 'Downloads'),
            ('用户目录', home),
        ]
    result = []
    for label, path in candidates:
        try:
            if path.is_dir():
                result.append((label, path))
        except OSError:
            continue
    return result


# ---------- 小工具 ----------
def natural_key(name):
    """自然排序：让 第2集 排在 第10集 前面。"""
    parts = re.split(r'(\d+)', name)
    return [int(t) if t.isdigit() else t.lower() for t in parts]


def format_time(ms):
    """毫秒 -> mm:ss（超过 1 小时自动变成 h:mm:ss）。"""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return '00:00'
    if ms < 0:
        ms = 0
    total = ms // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return '%d:%02d:%02d' % (h, m, s)
    return '%02d:%02d' % (m, s)


def is_video_file(path):
    """是不是我们认得的视频文件。"""
    try:
        return path.is_file() and path.suffix.lower() in ALL_VIDEO_EXT
    except OSError:
        return False


def can_play(path, on_android=None, with_vlc=False):
    """
    这个文件在当前平台上大概率能播吗。

    with_vlc=True 表示自带的 libVLC 内核可用。那种情况下 RMVB / RM /
    WMV / AVI / FLV 这些老格式全都能放（VLC 自己就带 FFmpeg 解码器），
    所以只要是视频文件就算能播 —— 这也是加 libVLC 的意义所在。
    """
    if on_android is None:
        on_android = IS_ANDROID
    ext = Path(path).suffix.lower()
    if ext not in ALL_VIDEO_EXT:
        return False
    if with_vlc:
        return True
    if on_android:
        return ext in ANDROID_SAFE_EXT
    return True


def list_videos(folder):
    """列出一个文件夹里的视频，按自然顺序排序。"""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    try:
        files = [f for f in folder.iterdir() if is_video_file(f)]
    except OSError:
        return []
    files.sort(key=lambda p: natural_key(p.name))
    return files


def app_private_dir():
    """
    应用私有目录，存放设置和播放记录。
    安卓上这是 /data/data/<包名>/files，不受存储权限影响，永远可写。
    """
    if IS_ANDROID:
        try:
            from kivy.app import App
            app = App.get_running_app()
            if app is not None and app.user_data_dir:
                return Path(app.user_data_dir)
        except Exception:
            pass
        return Path('/data/data/org.elder.videoplayer/files')
    return Path(__file__).resolve().parent / 'userdata'


# ---------- 目录扫描 ----------
def scan_categories(root):
    """
    扫描主文件夹，返回 {分类名: [剧集Path, ...]}。

    支持三种摆法（后两种原版都会漏掉，这里补上）：
        老人视频/黄梅戏/女驸马/女驸马01.mp4     <- 标准三层
        老人视频/电视剧/单本剧.mp4             <- 分类下直接放视频
        老人视频/西游记/01.mp4
        老人视频/西游记/花絮.mp4               <- 两种混着放

    混着放的时候，分类文件夹自己也会作为一个「剧集」返回，
    界面上会标成「未分集的视频」。宁可多显示一项，也不能把用户的文件吞掉。
    """
    result = {}
    root = Path(root)
    if not root.is_dir():
        return result

    try:
        entries = list(root.iterdir())
    except OSError:
        return result

    for cat_dir in sorted(entries, key=lambda p: natural_key(p.name)):
        if not cat_dir.is_dir():
            continue
        # 跳过隐藏目录（比如 .player_config、.thumbnails）
        if cat_dir.name.startswith('.'):
            continue

        series = []
        try:
            subs = list(cat_dir.iterdir())
        except OSError:
            subs = []

        for sub in sorted(subs, key=lambda p: natural_key(p.name)):
            if sub.is_dir() and not sub.name.startswith('.') and list_videos(sub):
                series.append(sub)

        # 分类文件夹根目录下直接放着的视频
        if list_videos(cat_dir):
            series.append(cat_dir)

        if series:
            # 旧名字（黄梅戏）归到新分类（戏曲）下；
            # 两个文件夹都存在时合并到一起，不丢东西
            key = CAT_FOLDER_ALIASES.get(cat_dir.name, cat_dir.name)
            result.setdefault(key, []).extend(series)

    return result


def scan_root_report(root):
    """给设置页用的诊断信息：这个主文件夹到底能不能用。"""
    root = Path(root)
    info = {
        'path': str(root),
        'exists': root.is_dir(),
        'readable': False,
        'category_count': 0,
        'video_count': 0,
        'message': '',
    }
    if not info['exists']:
        info['message'] = '主文件夹不存在，需要先创建。'
        return info

    try:
        info['readable'] = os.access(str(root), os.R_OK)
    except Exception:
        info['readable'] = False

    if not info['readable']:
        info['message'] = '没有读取权限，需要授予存储权限。'
        return info

    cats = scan_categories(root)
    info['category_count'] = len(cats)
    info['video_count'] = sum(len(list_videos(s)) for ss in cats.values() for s in ss)
    if not cats:
        info['message'] = '文件夹是空的，或者里面没有视频。'
    else:
        info['message'] = '正常，找到 %d 个分类、%d 个剧集。' % (
            len(cats), sum(len(v) for v in cats.values()))
    return info


def find_existing_root():
    """在候选路径里找一个真的存在的主文件夹。找不到返回 None。"""
    if IS_ANDROID:
        for cand in ANDROID_ROOT_CANDIDATES:
            p = Path(cand)
            if p.is_dir() and os.access(str(p), os.R_OK):
                return p
        return None
    p = Path.home() / 'Desktop' / DEFAULT_ROOT_NAME
    return p if p.is_dir() else None


# ---------- 配置读写 ----------
class Store(object):
    """
    负责三件事：主文件夹路径、设置、播放记录。
    全部落在应用私有目录，安卓上无需任何权限。
    """

    def __init__(self, base_dir=None):
        self.base = Path(base_dir) if base_dir else app_private_dir()
        self.config_dir = self.base / CONFIG_DIR_NAME
        self.thumb_dir = self.base / THUMBNAIL_DIR
        self.icon_dir = self.base / ICON_DIR
        self._safe_mkdir(self.config_dir)
        self._safe_mkdir(self.thumb_dir)
        self._safe_mkdir(self.icon_dir)

        self.settings = self._read_json(SETTINGS_FILE, {})
        if not isinstance(self.settings, dict):
            self.settings = {}

        raw_records = self._read_json(STATE_FILE, [])
        # 兼容老格式：单个 dict 也当成一条记录
        if isinstance(raw_records, dict):
            raw_records = [raw_records] if raw_records else []
        if not isinstance(raw_records, list):
            raw_records = []
        self.records = raw_records

        self.root_path = self._read_root_path()

    # ---- 基础 ----
    @staticmethod
    def _safe_mkdir(path):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _read_json(self, filename, default):
        target = self.config_dir / filename
        if not target.exists():
            return default
        try:
            with open(str(target), 'r', encoding='utf-8') as fp:
                return json.load(fp)
        except Exception:
            return default

    def _write_json(self, filename, data):
        target = self.config_dir / filename
        tmp = target.with_suffix(target.suffix + '.tmp')
        try:
            with open(str(tmp), 'w', encoding='utf-8') as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            # 原子替换，避免写到一半断电把配置写坏
            if target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass
            os.replace(str(tmp), str(target))
            return True
        except Exception:
            return False

    # ---- 主文件夹 ----
    def _read_root_path(self):
        target = self.config_dir / ROOT_PATH_FILE
        if target.exists():
            try:
                text = target.read_text(encoding='utf-8').strip()
                if text:
                    return Path(text)
            except Exception:
                pass
        found = find_existing_root()
        if found:
            return found
        return self.default_root()

    def default_root(self):
        if IS_ANDROID:
            return Path(ANDROID_ROOT_CANDIDATES[0])
        return Path.home() / 'Desktop' / DEFAULT_ROOT_NAME

    def set_root_path(self, new_path):
        self.root_path = Path(new_path)
        target = self.config_dir / ROOT_PATH_FILE
        try:
            target.write_text(str(self.root_path), encoding='utf-8')
            return True
        except Exception:
            return False

    def try_create_root(self):
        """尝试创建主文件夹（只有拿到完全访问权限才可能成功）。"""
        try:
            self.root_path.mkdir(parents=True, exist_ok=True)
            return self.root_path.is_dir()
        except Exception:
            return False

    # ---- 设置 ----
    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value
        return self._write_json(SETTINGS_FILE, self.settings)

    def save_settings(self):
        return self._write_json(SETTINGS_FILE, self.settings)

    # ---- 打开 App 时的屏幕方向 ----
    def default_landscape(self):
        """默认横屏。用户没选过、或者设置坏了，都按横屏算。"""
        return self.get_setting(ORIENTATION_SETTING,
                                ORIENTATION_LANDSCAPE) != ORIENTATION_PORTRAIT

    def set_default_landscape(self, landscape):
        self.set_setting(ORIENTATION_SETTING,
                         ORIENTATION_LANDSCAPE if landscape
                         else ORIENTATION_PORTRAIT)

    # ---- 主页图标 ----
    def home_icon(self, key):
        """取这个入口的自定义图标，没有就返回 None（界面会用默认色块）。"""
        try:
            for p in sorted(self.icon_dir.glob(key + '.*')):
                if p.is_file():
                    return p
        except Exception:
            pass
        return None

    def clear_home_icon(self, key):
        """删掉自定义图标，恢复成默认的彩色方块。"""
        removed = False
        try:
            for p in list(self.icon_dir.glob(key + '.*')):
                try:
                    p.unlink()
                    removed = True
                except OSError:
                    pass
        except Exception:
            pass
        return removed

    def save_home_icon(self, key, source):
        """
        把用户选的图片存成这个入口的图标。

        会先缩到 512px —— 手机上拍的照片动辄 4000x3000，
        直接当纹理用要占几十 MB 显存，老机器容易崩。
        没有 Pillow 就原样拷贝，大图也能显示，只是没那么省。
        """
        source = Path(source)
        if not source.is_file():
            return None
        self._safe_mkdir(self.icon_dir)
        self.clear_home_icon(key)

        try:
            from PIL import Image, ImageOps
            img = Image.open(str(source))
            # 手机拍的照片是靠 EXIF 里的 Orientation 标记「告诉」看图软件怎么转的，
            # 像素本身还是横着的。Pillow 不会自动转，直接缩图存下来就成了倒着的
            # —— 用户实测「上传后图片是颠倒过来的」就是这个。
            # exif_transpose 会把像素真的转正，存出来的 PNG 就不再带方向标记。
            try:
                fixed = ImageOps.exif_transpose(img)
                if fixed is not None:
                    img = fixed
            except Exception:
                pass
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGBA')
            img.thumbnail((512, 512), Image.LANCZOS)
            target = self.icon_dir / (key + '.png')
            img.save(str(target), 'PNG')
            return target
        except Exception:
            target = self.icon_dir / (key + source.suffix.lower())
            try:
                shutil.copyfile(str(source), str(target))
                return target
            except Exception:
                return None

    # ---- 播放记录 ----
    def reload_records(self):
        raw = self._read_json(STATE_FILE, [])
        if isinstance(raw, dict):
            raw = [raw] if raw else []
        if not isinstance(raw, list):
            raw = []
        self.records = raw
        return self.records

    def save_records(self):
        return self._write_json(STATE_FILE, self.records)

    def upsert_record(self, series_path, video_index, position):
        """
        写入/更新一条播放记录。
        沿用原版规则：同一剧集只保留最近一条。
        """
        record = {
            'series_path': str(series_path),
            'video_index': int(video_index),
            'position': int(position),
        }
        # 同一剧集只留最新一条，并且挪到最前面
        # （历史播放列表里，最近看的就在最上面）
        self.records = [r for r in self.records
                        if not (isinstance(r, dict)
                                and r.get('series_path') == str(series_path))]
        self.records.insert(0, record)
        # 别让记录无限增长
        if len(self.records) > 50:
            del self.records[50:]
        return self.save_records()

    def valid_records(self):
        """
        过滤出还能用的记录（文件夹还在、视频还在），并补齐展示需要的信息。
        返回 [(record, series_path, video_path, video_files), ...]，最近播放的排在前面。
        """
        valid = []
        for rec in self.records:
            if not isinstance(rec, dict):
                continue
            series_str = rec.get('series_path')
            if not series_str:
                continue
            series_path = Path(series_str)
            if not series_path.is_dir():
                continue
            videos = list_videos(series_path)
            if not videos:
                continue
            idx = rec.get('video_index', 0)
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                idx = 0
            if idx < 0 or idx >= len(videos):
                idx = 0
            valid.append((rec, series_path, videos[idx], videos))
        return valid


def thumb_cache_path(store, video_path, size):
    """
    缩略图缓存路径。
    原版用 md5(路径+尺寸) 做文件名，这里沿用同样的思路。
    """
    import hashlib
    key = '%s|%d' % (str(video_path), int(size))
    name = hashlib.md5(key.encode('utf-8')).hexdigest() + '.jpg'
    return store.thumb_dir / name
