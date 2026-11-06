# -*- coding: utf-8 -*-
"""
elder_vlc.py —— 可选的 libVLC 播放内核（让 App 能放 RMVB / WMV / AVI / FLV）

【为什么需要这个模块】
安卓系统自带的播放器只认 H.264 的 mp4 / mkv 那几样，RMVB、RM、WMV、AVI、
FLV 这些老格式它一律解不了 —— 这是系统限制，改代码没用。

那为什么 MX Player、VLC 这些播放器什么都能放？
因为它们**自己带了一套 FFmpeg 解码器**，根本不靠系统。
（MX Player 就有三套引擎：系统硬解、自研的 HW+、以及跑在 CPU 上的多线程
FFmpeg 软解。代价是它的安装包有 87～105 MB，而本应用只有十几 MB。）

这个模块就是把 VLC 的内核（libVLC）接进来，让本应用也具备这个能力。

【它是怎么工作的】
libVLC 没法画在 Kivy 的画布上 —— 它画在安卓原生的 SurfaceView 上。
所以做法是：往安卓的视图树里插一个 SurfaceView，让它的大小和位置
跟着 Kivy 里那个「视频占位控件」走，再把这个 SurfaceView 交给 libVLC。

两个必须处理的细节：
  1. SurfaceView 要设 setZOrderMediaOverlay(True)。Kivy 的画面也是一层
     SurfaceView，不这样设的话 VLC 的画面会被 Kivy 整个盖住，只有声音。
     （社区里「Kivy 播视频有声音没画面」基本都是踩在这个点上。）
  2. SurfaceView 不能抢触摸事件（setClickable(False)）。它在 Kivy 之上，
     安卓的触摸分发是从上往下找第一个「愿意处理」的控件，它不消费，
     事件就会继续往下传，下面 Kivy 的暂停、快进按钮才能正常点。

【降级 —— 这一点很重要】
只要这个模块在某个环境里跑不起来（依赖没打进 APK、初始化失败、设备不兼容、
画面挂不上去），VlcVideo.probe() 就会返回 False，上层会自动回退到 Kivy
自带的系统播放器。**应用不会因为 libVLC 出问题就不可用。**

接口上它刻意和 kivy.uix.video.Video 保持一致（source / state / position /
duration / seek / volume），这样播放界面那套逻辑两套内核可以共用。
"""

from kivy.clock import Clock
from kivy.core.window import Window

try:
    from kivy.utils import platform as _platform
    IS_ANDROID = (_platform == 'android')
except Exception:                                  # pragma: no cover
    IS_ANDROID = False

try:
    from jnius import autoclass
except Exception:                                  # 桌面环境没有 jnius
    autoclass = None


# libVLC 的播放状态码，对应 org.videolan.libvlc.MediaPlayer$State
STATE_NOTHING = 0
STATE_OPENING = 1
STATE_BUFFERING = 2
STATE_PLAYING = 3
STATE_PAUSED = 4
STATE_STOPPED = 5
STATE_ENDED = 6
STATE_ERROR = 7

# android.R.id.content —— Activity 里承载所有界面的那个 FrameLayout
ANDROID_CONTENT_ID = 0x01020002


def _activity():
    """拿到当前正在运行的 Activity。"""
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    return PythonActivity.mActivity


def _ui(fn):
    """
    把一段操作排到安卓 UI 线程去执行。

    Python 代码在 p4a 里跑在单独的线程上，不是安卓的 UI 线程，
    而动 View 是必须要在 UI 线程做的。
    """
    if not IS_ANDROID:
        return
    try:
        from android.runnable import run_on_ui_thread
        run_on_ui_thread(fn)()
    except Exception:
        pass


class VlcVideo(object):
    """
    一个「长得像 kivy.uix.video.Video」的播放器，真正干活的是 libVLC。

    用法上和 Kivy 的 Video 一样：
        v = VlcVideo(holder=某个Widget)
        v.attach_view()
        v.source = '/storage/emulated/0/xxx.rmvb'
        v.state = 'play'
        print(v.duration, v.position)
        v.seek(0.5)
        v.state = 'pause'
        v.detach_view()
        v.release()
    """

    # ---- 可用性探测（全局只做一次）----
    _probed = False
    _ok = False
    _why = ''

    @classmethod
    def probe(cls, force=False):
        """
        当前环境能不能用 libVLC。结果会缓存，不会每次都去试。

        返回 True/False。失败原因可以用 why_not() 取。
        """
        if cls._probed and not force:
            return cls._ok

        cls._probed = True
        cls._ok = False
        cls._why = ''

        if not IS_ANDROID:
            cls._why = '当前不是安卓环境'
            return False
        if autoclass is None:
            cls._why = 'pyjnius 不可用'
            return False
        try:
            autoclass('org.videolan.libvlc.LibVLC')
        except Exception as exc:
            cls._why = 'APK 里没有找到 libVLC（%s）' % exc
            return False

        cls._ok = True
        return True

    @classmethod
    def why_not(cls):
        """上次探测失败的原因，给设置页显示用。"""
        return cls._why

    # ------------------------------------------------------------------
    def __init__(self, holder=None, **kwargs):
        self._holder = holder
        self._libvlc = None
        self._player = None
        self._surface = None
        self._content = None
        self._attached = False
        self._scale = None
        self._last_box = None
        self._sync_pending = False

        self._source = ''
        self._loaded = ''          # 当前已经塞进播放器的文件
        self._volume = 1.0

        # 这几个是为了和 kivy.uix.video.Video 的接口对齐，
        # 播放界面（PlayerScreen）会去设它们
        self.allow_stretch = True
        self.keep_ratio = True
        self.fit_mode = 'contain'
        self.options = {}

        try:
            self._create_player()
        except Exception:
            self.release()
            raise

        self._bind_holder()

    # ---- 初始化 ----
    def _create_player(self):
        activity = _activity()
        ArrayList = autoclass('java.util.ArrayList')
        LibVLC = autoclass('org.videolan.libvlc.LibVLC')
        MediaPlayer = autoclass('org.videolan.libvlc.MediaPlayer')

        options = ArrayList()
        # 不自动去匹配外挂字幕（本地看戏用不上，还多个扫描动作）
        options.add('--no-sub-autodetect-file')
        # 别把播放统计写日志，老人机的存储能省一点是一点
        options.add('--no-stats')

        try:
            self._libvlc = LibVLC(activity, options)
        except Exception:
            # 有些版本不吃 List 这个重载，退回最简构造
            self._libvlc = LibVLC(activity)

        self._player = MediaPlayer(self._libvlc)

    # ---- 和占位控件对齐位置 ----
    def _bind_holder(self):
        holder = self._holder
        if holder is None:
            return
        holder.bind(pos=self._on_holder_move, size=self._on_holder_move)
        Window.bind(size=self._on_holder_move)

    def _unbind_holder(self):
        """
        解绑。

        播放界面每次进入都会新建一个播放器（设置可能改过、上次可能失败过），
        不解绑的话，旧的回调会一直挂在占位控件和 Window 上，
        播得越多、攒得越多。
        """
        holder = self._holder
        if holder is not None:
            try:
                holder.unbind(pos=self._on_holder_move, size=self._on_holder_move)
            except Exception:
                pass
        try:
            Window.unbind(size=self._on_holder_move)
        except Exception:
            pass

    def _on_holder_move(self, *_args):
        # 布局可能分几步算完，等下一帧再取坐标才是最终的
        if self._sync_pending:
            return
        self._sync_pending = True

        def _later(_dt):
            self._sync_pending = False
            self.sync_view()

        Clock.schedule_once(_later, 0)

    def _screen_scale(self):
        """
        Kivy 的窗口单位 → 安卓物理像素 的换算系数。

        Kivy 在安卓上一般已经是物理像素（系数就是 1），但不同 SDL 版本、
        不同的缩放设置下不保证。这里拿安卓报出来的真实屏幕宽度校准一次，
        免得视频区的位置算偏了。
        """
        if self._scale is not None:
            return self._scale

        scale = 1.0
        try:
            activity = _activity()
            DisplayMetrics = autoclass('android.util.DisplayMetrics')
            metrics = DisplayMetrics()
            activity.getWindowManager().getDefaultDisplay().getRealMetrics(metrics)
            real_w = float(metrics.widthPixels)
            kivy_w = float(Window.width)
            if kivy_w > 0 and real_w > 0:
                scale = real_w / kivy_w
        except Exception:
            scale = 1.0

        # 只接受看起来合理的倍数，算出离谱的值就当作不需要换算
        if not (0.2 <= scale <= 8.0):
            scale = 1.0

        self._scale = scale
        return scale

    def _holder_box(self):
        """
        算出视频区在安卓屏幕坐标里的矩形，返回 (left, top, 宽, 高)，单位像素。

        安卓的坐标原点在左上角，Kivy 的在左下角，所以要翻一下 y。
        """
        holder = self._holder
        if holder is None:
            return None
        try:
            x, y = holder.to_window(holder.x, holder.y)
            w = float(holder.width)
            h = float(holder.height)
            win_h = float(Window.height)
        except Exception:
            return None

        if w < 2 or h < 2 or win_h < 2:
            return None

        s = self._screen_scale()
        left = int(round(x * s))
        top = int(round((win_h - (y + h)) * s))
        width = int(round(w * s))
        height = int(round(h * s))

        if width < 2 or height < 2:
            return None
        return (left, top, width, height)

    def sync_view(self):
        """把 SurfaceView 的位置大小，重新对齐到 Kivy 的占位控件上。"""
        if not self._attached or self._surface is None:
            return
        box = self._holder_box()
        if box is None or box == self._last_box:
            return
        self._last_box = box
        left, top, width, height = box
        _ui(lambda: self._apply_box(left, top, width, height))

    def _apply_box(self, left, top, width, height):
        # 这个函数一定在 UI 线程里跑
        try:
            ViewGroup = autoclass('android.view.ViewGroup')
            self._surface.setX(float(left))
            self._surface.setY(float(top))
            self._surface.setLayoutParams(
                ViewGroup.LayoutParams(int(width), int(height)))
        except Exception:
            pass

    # ---- 挂载 / 卸载画面 ----
    def is_attached(self):
        return bool(self._attached and self._surface is not None)

    def attach_view(self):
        """把画面接到屏幕上，进播放界面时调一次。"""
        if not IS_ANDROID or self._player is None or self._attached:
            return
        self._attached = True        # 先占位，避免重复挂
        _ui(self._do_attach)

    def _do_attach(self):
        try:
            SurfaceView = autoclass('android.view.SurfaceView')
            ViewGroup = autoclass('android.view.ViewGroup')
            activity = _activity()

            surface = SurfaceView(activity)
            # 盖在 Kivy 的图层之上，否则画面会被 Kivy 挡得只剩声音
            surface.setZOrderMediaOverlay(True)
            # 不抢触摸，让事件穿透到下面 Kivy 的按钮上
            surface.setClickable(False)
            surface.setFocusable(False)
            try:
                surface.setFocusableInTouchMode(False)
            except Exception:
                pass

            self._surface = surface

            vout = self._player.getVLCVout()
            vout.setVideoView(surface)
            vout.attachViews()

            content = activity.findViewById(ANDROID_CONTENT_ID)
            if content is None:
                content = activity.getWindow().getDecorView()
            self._content = content
            # 先给个 1x1，真正的尺寸马上由 sync_view 补上
            content.addView(surface, ViewGroup.LayoutParams(1, 1))

            box = self._holder_box()
            if box:
                self._last_box = box
                self._apply_box(*box)
        except Exception:
            self._attached = False
            self._surface = None

    def detach_view(self):
        """把画面摘下来，离开播放界面时调。"""
        if not IS_ANDROID:
            return
        self._attached = False
        self._last_box = None
        _ui(self._do_detach)

    def _do_detach(self):
        player, surface, content = self._player, self._surface, self._content
        try:
            if player is not None:
                player.getVLCVout().detachViews()
        except Exception:
            pass
        try:
            if content is not None and surface is not None:
                content.removeView(surface)
        except Exception:
            pass
        self._surface = None
        self._content = None

    def release(self):
        """彻底释放，换内核或者退出应用时调。"""
        try:
            self._unbind_holder()
        except Exception:
            pass
        try:
            self.detach_view()
        except Exception:
            pass
        try:
            if self._player is not None:
                self._player.stop()
        except Exception:
            pass
        try:
            if self._player is not None:
                self._player.release()
        except Exception:
            pass
        try:
            if self._libvlc is not None:
                self._libvlc.release()
        except Exception:
            pass
        self._player = None
        self._libvlc = None

    # ---- 播放控制 ----
    def _load(self, path):
        """把文件塞进播放器。"""
        try:
            Media = autoclass('org.videolan.libvlc.Media')
            Uri = autoclass('android.net.Uri')
            File = autoclass('java.io.File')
            media = Media(self._libvlc, Uri.fromFile(File(str(path))))
        except Exception:
            return False

        ok = False
        try:
            self._player.setMedia(media)
            ok = True
        except Exception:
            ok = False
        finally:
            # setMedia 会自己持有引用，这里可以立刻放掉
            try:
                media.release()
            except Exception:
                pass

        if ok:
            self._loaded = str(path)
        return ok

    # ---- 和 kivy.uix.video.Video 对齐的接口 ----
    @property
    def source(self):
        return self._source

    @source.setter
    def source(self, value):
        self._source = str(value or '')

    @property
    def state(self):
        """
        返回 'play' / 'pause' / 'stop'。

        这里问的是 libVLC 的真实状态，不是记下来的意图 ——
        播放中途出错、播到结尾，都能如实反映出来。
        """
        if self._player is None:
            return 'stop'
        try:
            code = int(self._player.getPlayerState())
        except Exception:
            return 'stop'
        # 正在打开和缓冲也算「在播」，否则按钮会在加载时闪一下
        if code in (STATE_OPENING, STATE_BUFFERING, STATE_PLAYING):
            return 'play'
        if code == STATE_PAUSED:
            return 'pause'
        return 'stop'

    @state.setter
    def state(self, value):
        if self._player is None:
            raise RuntimeError('播放器还没准备好')

        if value == 'play':
            # 换文件了才重新加载，从暂停恢复就只是继续
            if self._source and self._loaded != self._source:
                if not self._load(self._source):
                    raise RuntimeError('这个视频打不开')
            self._player.play()
        elif value == 'pause':
            try:
                self._player.pause()
            except Exception:
                pass
        elif value == 'stop':
            try:
                self._player.stop()
            except Exception:
                pass

    @property
    def duration(self):
        """片长，毫秒。还没读出来时返回 0。"""
        if self._player is None:
            return 0.0
        try:
            length = int(self._player.getLength())
        except Exception:
            return 0.0
        return float(length) if length > 0 else 0.0

    @property
    def position(self):
        """当前播到哪了，毫秒。"""
        if self._player is None:
            return 0.0
        try:
            current = int(self._player.getTime())
        except Exception:
            return 0.0
        return float(current) if current > 0 else 0.0

    def seek(self, ratio):
        """按比例跳转，0.0～1.0 —— 和 Video.seek 的参数一样。"""
        if self._player is None:
            return
        try:
            ratio = max(0.0, min(1.0, float(ratio)))
        except (TypeError, ValueError):
            return
        try:
            length = int(self._player.getLength())
            if length > 0:
                self._player.setTime(int(length * ratio))
        except Exception:
            pass

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value):
        try:
            vol = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return
        self._volume = vol
        if self._player is None:
            return
        try:
            self._player.setVolume(int(round(vol * 100)))
        except Exception:
            pass

    @property
    def ended(self):
        """这一集是不是播完了。"""
        if self._player is None:
            return False
        try:
            return int(self._player.getPlayerState()) == STATE_ENDED
        except Exception:
            return False
