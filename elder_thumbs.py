# -*- coding: utf-8 -*-
"""
elder_thumbs.py —— 缩略图生成

原版用 OpenCV（cv2.VideoCapture 读第一帧）来生成缩略图。
问题是：OpenCV 在安卓上打包极其麻烦（体积巨大、recipe 经常编译失败），
为了一个缩略图去扛这个风险不划算。

这里换了个思路：
  安卓 -> 直接调用系统自带的 android.media.MediaMetadataRetriever 抽帧。
        这是安卓原生 API，零额外依赖，速度快，还能用硬解。
  桌面 -> 有 cv2 就用 cv2，没有就直接走色块兜底（本地调试用）。

不管哪条路，只要失败就静默返回 None，界面自动退回「彩色圆角块 + 首字」，
也就是原版里 thumbnail 为 None 时的那套方案。永远不因为缩略图把程序搞崩。
"""

import os
import threading
from collections import deque

from kivy.clock import Clock
from kivy.utils import platform

from elder_core import IS_ANDROID, thumb_cache_path


class ThumbnailLoader(object):
    """
    异步缩略图加载器。

    用法：
        loader.request(视频路径, 回调)
    回调一定在主线程被调用，参数是 Kivy Texture 或者 None。
    生成工作在后台线程做，不会卡住界面。
    """

    def __init__(self, store, size=256):
        self.store = store
        self.size = int(size)
        self.enabled = True

        self._mem = {}          # 视频路径 -> Texture / None（None 表示试过但失败了）
        self._images = {}       # 保引用，防止纹理被回收
        self._waiters = {}      # 视频路径 -> [回调, ...]
        self._queue = deque()
        self._queued = set()
        self._lock = threading.Lock()
        self._thread = None
        self._stop_flag = False
        # 视频多的时候纹理也是很占显存的，超过上限就把最早的丢掉。
        # 丢掉之后如果还要用，会重新从磁盘缓存读回来，不亏。
        self._max_mem = 240

    # ------------------------------------------------------------ 对外接口
    def request(self, video_path, callback):
        """要一张缩略图，拿到后在主线程回调。"""
        key = str(video_path)

        if not self.enabled:
            self._later(callback, None)
            return

        if key in self._mem:
            self._later(callback, self._mem[key])
            return

        with self._lock:
            self._waiters.setdefault(key, []).append(callback)
            need_queue = key not in self._queued
            if need_queue:
                self._queued.add(key)
                self._queue.append(key)
        if need_queue:
            self._ensure_worker()

    def cached(self, video_path):
        """已经生成好的话直接返回纹理，否则 None（不触发后台生成）。"""
        return self._mem.get(str(video_path))

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if not enabled:
            with self._lock:
                self._queue.clear()
                self._queued.clear()
                waiters = list(self._waiters.values())
                self._waiters.clear()
            for group in waiters:
                for cb in group:
                    self._later(cb, None)

    def clear(self):
        """清空内存缓存（改尺寸档位时用）。"""
        self._mem.clear()
        self._images.clear()

    def clear_disk_cache(self):
        """删掉磁盘上的缩略图缓存。"""
        removed = 0
        try:
            for f in self.store.thumb_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        except Exception:
            pass
        self.clear()
        return removed

    # ------------------------------------------------------------ 后台线程
    @staticmethod
    def _later(callback, value):
        Clock.schedule_once(lambda dt: callback(value), 0)

    def _ensure_worker(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag = False
        self._thread = threading.Thread(target=self._run, name='thumb-loader')
        self._thread.daemon = True
        self._thread.start()

    def _run(self):
        try:
            while not self._stop_flag:
                try:
                    with self._lock:
                        key = self._queue.popleft()
                except IndexError:
                    return
                cache_file = None
                try:
                    cache_file = self._generate(key)
                except Exception:
                    cache_file = None
                self._deliver(key, cache_file)
        finally:
            # pyjnius 起的线程用完要解绑，否则 JVM 侧会慢慢泄漏
            if IS_ANDROID:
                try:
                    from jnius import detach
                    detach()
                except Exception:
                    pass

    def _deliver(self, key, cache_file):
        def _apply(_dt):
            tex = None
            if cache_file:
                tex = self._load_texture(cache_file)
            with self._lock:
                self._mem[key] = tex
                group = self._waiters.pop(key, [])
                self._queued.discard(key)
                self._prune_locked()
            for cb in group:
                try:
                    cb(tex)
                except Exception:
                    pass
        Clock.schedule_once(_apply, 0)

    def _prune_locked(self):
        """超出上限时，按插入顺序丢掉最早的（dict 在 Python 3.7+ 保持插入序）。"""
        if len(self._mem) <= self._max_mem:
            return
        overflow = len(self._mem) - self._max_mem
        for old_key in list(self._mem.keys())[:overflow]:
            self._mem.pop(old_key, None)
            self._images.pop(old_key, None)

    def _load_texture(self, path):
        """在主线程把磁盘上的 JPG 变成纹理。"""
        try:
            from kivy.core.image import Image as CoreImage
            img = CoreImage(str(path))
            tex = img.texture
            if tex is None:
                return None
            self._images[str(path)] = img
            try:
                tex.mag_filter = 'linear'
                tex.min_filter = 'linear'
            except Exception:
                pass
            return tex
        except Exception:
            return None

    # ------------------------------------------------------------ 生成
    def _generate(self, video_path):
        cache_file = thumb_cache_path(self.store, video_path, self.size)
        try:
            if cache_file.exists() and cache_file.stat().st_size > 0:
                return str(cache_file)
        except OSError:
            pass

        if IS_ANDROID:
            ok = _extract_android(video_path, cache_file, self.size)
        else:
            ok = _extract_desktop(video_path, cache_file, self.size)

        if not ok:
            # 失败就清掉可能残留的空文件，免得下次被误判成「已缓存」
            try:
                if cache_file.exists() and cache_file.stat().st_size == 0:
                    cache_file.unlink()
            except Exception:
                pass
            return None
        return str(cache_file)


def _safe_call(fn):
    try:
        fn()
    except Exception:
        pass


def _extract_android(video_path, cache_file, size):
    """
    用安卓自带的 MediaMetadataRetriever 抓一帧存成 JPG。

    走 Java 侧的 FileOutputStream 直接落盘，不把字节数组搬回 Python ——
    pyjnius 在 jbyteArray 上的转换行为各版本不一致，绕开它最稳。
    """
    try:
        from jnius import autoclass
    except Exception:
        return False

    retriever = None
    frame = None
    scaled = None
    stream = None
    try:
        Bitmap = autoclass('android.graphics.Bitmap')
        CompressFormat = autoclass('android.graphics.Bitmap$CompressFormat')
        MediaMetadataRetriever = autoclass('android.media.MediaMetadataRetriever')
        FileOutputStream = autoclass('java.io.FileOutputStream')

        retriever = MediaMetadataRetriever()
        retriever.setDataSource(str(video_path))
        # -1 = 随便取一个同步帧，通常就是片头，速度最快
        frame = retriever.getFrameAtTime(-1)
        if frame is None:
            return False

        w = int(frame.getWidth())
        h = int(frame.getHeight())
        if w <= 0 or h <= 0:
            return False

        # 存成比显示尺寸大一倍，够清晰又不至于把磁盘缓存撑爆
        longest = max(w, h)
        target = int(size) * 2
        if longest > target:
            ratio = float(target) / float(longest)
            scaled = Bitmap.createScaledBitmap(
                frame, max(1, int(w * ratio)), max(1, int(h * ratio)), True)
        else:
            scaled = frame

        stream = FileOutputStream(str(cache_file))
        written = scaled.compress(CompressFormat.JPEG, 82, stream)
        stream.flush()
        return bool(written)
    except Exception:
        return False
    finally:
        if stream is not None:
            _safe_call(stream.close)
        if scaled is not None and scaled is not frame:
            _safe_call(scaled.recycle)
        if frame is not None:
            _safe_call(frame.recycle)
        if retriever is not None:
            _safe_call(retriever.release)


def _extract_desktop(video_path, cache_file, size):
    """桌面端：装了 OpenCV 就顺手用一下，没装就算了（直接走色块兜底）。"""
    try:
        import cv2
    except Exception:
        return False
    cap = None
    try:
        cap = cv2.VideoCapture(str(video_path))
        ok, frame = cap.read()
        if not ok or frame is None:
            return False
        h, w = frame.shape[:2]
        longest = max(w, h)
        target = int(size) * 2
        if longest > target:
            ratio = float(target) / float(longest)
            frame = cv2.resize(frame, (max(1, int(w * ratio)), max(1, int(h * ratio))))
        return bool(cv2.imwrite(str(cache_file), frame,
                                [int(cv2.IMWRITE_JPEG_QUALITY), 82]))
    except Exception:
        return False
    finally:
        if cap is not None:
            _safe_call(cap.release)
