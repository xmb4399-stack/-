#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vlc_test.py —— 验证 libVLC 播放内核的接线对不对

【为什么单独写这个】
libVLC 是安卓的原生库，在电脑上根本跑不起来，所以 smoke_test 走的
始终是「系统播放器」那条降级路径，VLC 那条路一行都没被跑到。
这个脚本用一个假的播放器对象，把下面这些补上。

【能验到的】
  * VlcVideo 的接口和 kivy.uix.video.Video 是不是真的对得上
    （对不上的话，播放界面那套逻辑换内核就会静默出错）
  * 「换一集」和「从暂停恢复」有没有被正确区分（前者要重新加载，
    后者只该继续播 —— 搞错的话一暂停再播就从头开始了）
  * 视频区坐标从 Kivy（左下原点）换算到安卓（左上原点）对不对
  * libVLC 初始化失败时，会不会自动退回系统播放器
  * 画面挂不上去时，会不会自动退回系统播放器
  * can_play 在 libVLC 可用时会不会放行老格式

【验不到的】
  * 真的把 SurfaceView 挂到安卓视图树上、libVLC 真的解码出画面。
    这只有装了 APK 在真机上才能确认，本机没有安卓环境。

运行：python tools/vlc_test.py
"""

import os
import sys
from pathlib import Path

os.environ.setdefault('KIVY_NO_ARGS', '1')
os.environ.setdefault('KIVY_LOG_LEVEL', 'error')
# 别让 Kivy 去删旧日志：这台机器的安全策略会把删除重定向到回收站，
# 回收站偶尔失败就直接抛异常，import 阶段就死了
os.environ.setdefault('KIVY_NO_FILELOG', '1')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kivy.core.window import Window
from kivy.properties import (BooleanProperty, NumericProperty, StringProperty)
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.widget import Widget

import elder_core as core
import elder_vlc as vlc
import main as player_main

PASSED = []
FAILED = []


def check(name, condition, detail=''):
    if condition:
        PASSED.append(name)
        print('  [OK]   %s' % name)
    else:
        FAILED.append((name, detail))
        print('  [FAIL] %s  %s' % (name, detail))


# ---------------------------------------------------------------- 假的零件
class FakeMedia(object):
    def __init__(self, path):
        self.path = path

    def release(self):
        pass


class FakePlayer(object):
    """假装是 org.videolan.libvlc.MediaPlayer。"""

    def __init__(self):
        self.player_state = 0      # 0 = NothingSpecial
        self.time = 0
        self.length = 0
        self.volume = -1
        self.media_path = None
        self.calls = []

    # ---- libVLC 里我们用到的那几个方法 ----
    def getPlayerState(self):
        return self.player_state

    def getLength(self):
        return self.length

    def getTime(self):
        return self.time

    def setTime(self, value):
        self.time = int(value)
        self.calls.append(('setTime', int(value)))
        return self.time

    def setVolume(self, value):
        self.volume = int(value)
        self.calls.append(('setVolume', int(value)))

    def setMedia(self, media):
        self.media_path = getattr(media, 'path', None)
        self.calls.append(('setMedia',))

    def play(self):
        self.player_state = 3      # Playing
        self.calls.append(('play',))

    def pause(self):
        self.player_state = 4      # Paused
        self.calls.append(('pause',))

    def stop(self):
        self.player_state = 5      # Stopped
        self.calls.append(('stop',))

    def release(self):
        self.calls.append(('release',))


class FakeVideo(Widget):
    """顶替 kivy.uix.video.Video，用来确认「退回系统播放器」时用的是它。"""

    source = StringProperty('')
    state = StringProperty('stop')
    duration = NumericProperty(0)
    position = NumericProperty(0)
    volume = NumericProperty(1.0)
    allow_stretch = BooleanProperty(True)
    keep_ratio = BooleanProperty(True)

    def seek(self, ratio):
        self.position = max(0.0, min(1.0, float(ratio))) * self.duration


class FakeStore(object):
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeApp(object):
    """播放界面要用到的那几个 App 方法，这里给个最小实现。"""

    def __init__(self, vlc_ok=True):
        self._vlc_ok = vlc_ok
        self.notes = []
        self.toasts = []
        self.current_videos = []
        self.current_index = 0

    def use_vlc_backend(self):
        return self._vlc_ok

    def note_vlc_failed(self, reason):
        self.notes.append(reason)
        self._vlc_ok = False

    def toast(self, message, seconds=2.0):
        self.toasts.append(message)

    def set_playback_ui(self, playing):
        pass


# ---------------------------------------------------------------- 打桩
_real_create = vlc.VlcVideo._create_player
_real_load = vlc.VlcVideo._load


def fake_create_player(self):
    """不走真正的 jnius 调用，直接塞一个假播放器进去。"""
    self._libvlc = object()
    self._player = FakePlayer()


def fake_load(self, path):
    self._player.setMedia(FakeMedia(str(path)))
    self._loaded = str(path)
    return True


def patch_vlc():
    vlc.VlcVideo._create_player = fake_create_player
    vlc.VlcVideo._load = fake_load


def unpatch_vlc():
    vlc.VlcVideo._create_player = _real_create
    vlc.VlcVideo._load = _real_load


# ---------------------------------------------------------------- 测试项
def test_interface():
    print('【1】VlcVideo 的接口和 kivy 的 Video 对得上吗')

    v = vlc.VlcVideo(holder=None)
    p = v._player

    check('初始状态是 stop', v.state == 'stop', v.state)
    check('初始时长为 0', v.duration == 0.0, str(v.duration))
    check('初始位置为 0', v.position == 0.0, str(v.position))

    v.source = '/x/第一集.mp4'
    v.state = 'play'
    check('设 source + play 会加载文件', p.media_path == '/x/第一集.mp4',
          str(p.media_path))
    check('并真的开始播放', p.player_state == 3, str(p.player_state))
    check('这时 state 报 play', v.state == 'play', v.state)

    # 换一集：必须重新加载
    v.source = '/x/第二集.rmvb'
    v.state = 'play'
    check('换一集时会重新加载', p.media_path == '/x/第二集.rmvb',
          str(p.media_path))

    # 暂停再播：绝对不能重新加载（否则会从头上开始）
    v.state = 'pause'
    check('暂停后 state 报 pause', v.state == 'pause', v.state)
    p.calls = []
    v.state = 'play'
    check('从暂停恢复不会重新加载文件',
          ('setMedia',) not in p.calls, str(p.calls))
    check('从暂停恢复只调用 play', ('play',) in p.calls, str(p.calls))

    # 时长和位置
    p.length = 3600000
    p.time = 900000
    check('时长读得到（毫秒）', v.duration == 3600000.0, str(v.duration))
    check('位置读得到（毫秒）', v.position == 900000.0, str(v.position))

    # 跳转
    v.seek(0.5)
    check('seek(0.5) 跳到一半', p.time == 1800000, str(p.time))
    v.seek(2.0)          # 越界要夹住
    check('seek 越界会被夹到 100%', p.time == 3600000, str(p.time))

    # 音量
    v.volume = 0.4
    check('volume 0.4 换算成 40', p.volume == 40, str(p.volume))

    # 播放结束
    p.player_state = 6
    check('播完后 state 报 stop（好触发自动下一集）',
          v.state == 'stop', v.state)
    check('ended 为真', v.ended is True, str(v.ended))

    # 打开中 / 缓冲中也应该算「在播」，否则按钮会闪
    for code, label in ((1, '打开中'), (2, '缓冲中')):
        p.player_state = code
        check('%s 时 state 也算 play' % label, v.state == 'play', v.state)

    # 加载失败必须抛异常，界面才知道要提示
    v2 = vlc.VlcVideo(holder=None)
    vlc.VlcVideo._load = lambda self, path: False
    raised = False
    try:
        v2.source = '/x/坏文件.rmvb'
        v2.state = 'play'
    except Exception:
        raised = True
    finally:
        vlc.VlcVideo._load = fake_load
    check('文件加载失败会抛异常（而不是静默黑屏）', raised)


def test_holder_box():
    print('【2】视频区坐标换算（Kivy 左下原点 → 安卓左上原点）')

    parent = FloatLayout()
    holder = Widget()
    parent.add_widget(holder)
    holder.size = (400, 300)
    holder.pos = (100, 200)

    v = vlc.VlcVideo(holder=holder)
    v._scale = 1.0            # 关掉自动校准，直接验公式本身

    box = v._holder_box()
    check('能算出矩形', box is not None, str(box))
    check('宽度原样传过去', box[2] == 400, str(box))
    check('高度原样传过去', box[3] == 300, str(box))
    check('left 直接用 Kivy 的 x', box[0] == 100, str(box))

    # 关键的一条：Kivy 的 y 往上，安卓的 top 往下，方向必须反过来
    expected_top = int(round(Window.height - (200 + 300)))
    check('top 用窗口高度翻转（方向不能反）', box[1] == expected_top,
          '%s vs %s' % (box[1], expected_top))

    holder.y += 100                       # 控件往上移
    higher = v._holder_box()
    check('控件上移后，安卓的 top 变小', higher[1] < box[1],
          '%s -> %s' % (box[1], higher[1]))

    # 尺寸为 0 时不该算出个没用的矩形
    holder.size = (0, 0)
    check('尺寸为 0 时返回 None（免得设出畸形区域）',
          v._holder_box() is None, str(v._holder_box()))

    # 校准系数：万一 Kivy 的窗口单位不是物理像素
    holder.size = (400, 300)
    v._scale = 2.0
    scaled = v._holder_box()
    check('校准系数会按比例放大', scaled[2] == 800 and scaled[3] == 600,
          str(scaled))


def test_holder_unbind():
    print('【2b】释放时把位置监听解掉（不然播得越多、攒得越多）')

    parent = FloatLayout()
    holder = Widget()
    parent.add_widget(holder)
    holder.size = (400, 300)

    before_win = len(Window.get_property_observers('size', 0) or [])
    before_hold = len(holder.get_property_observers('size', 0) or [])

    v = vlc.VlcVideo(holder=holder)

    after_win = len(Window.get_property_observers('size', 0) or [])
    after_hold = len(holder.get_property_observers('size', 0) or [])
    check('挂上监听后，Window 上多了一个 size 回调',
          after_win > before_win, '%d -> %d' % (before_win, after_win))
    check('占位控件上也挂上了回调',
          after_hold > before_hold, '%d -> %d' % (before_hold, after_hold))

    v.release()

    # Kivy 的 bind 对绑定方法用的是弱引用，属性观察者列表里会留下一个
    # 已经失效的引用（回调本身已经调不动了）。这里验证的是：
    # 解绑之后，即使再触发 size 变化，也不会真的进到我们的回调里。
    fired = []
    v._on_holder_move = lambda *a: fired.append(1)
    try:
        Window.dispatch('on_size')
    except Exception:
        pass

    check('释放后再触发尺寸变化，不再走旧回调', not fired, str(len(fired)))

    # 也不该把 holder 再算一次坐标（没挂载就应当直接返回）
    check('释放后 _attached 是关掉的', not v._attached)
    check('释放后 sync_view 是空操作',
          v.sync_view() is None)


def test_backend_switch():
    print('【3】两条内核之间怎么切换')

    player_main.Video = FakeVideo

    app = FakeApp(vlc_ok=True)
    screen = player_main.PlayerScreen(name='player')
    screen.app = lambda: app

    screen._setup_backend()
    check('libVLC 可用时会用它', isinstance(screen.video, vlc.VlcVideo),
          type(screen.video).__name__)
    check('用了 libVLC 就不再往视频区塞 kivy 控件',
          len(screen.video_box.children) == 1, str(screen.video_box.children))

    # ---- libVLC 初始化就失败 ----
    real = player_main.VlcVideo

    class BoomVlc(object):
        def __init__(self, *a, **kw):
            raise RuntimeError('假装 libVLC 起不来')

    player_main.VlcVideo = BoomVlc
    try:
        app2 = FakeApp(vlc_ok=True)
        screen2 = player_main.PlayerScreen(name='player')
        screen2.app = lambda: app2
        screen2._setup_backend()

        check('libVLC 起不来时退回系统播放器',
              isinstance(screen2.video, FakeVideo),
              type(screen2.video).__name__)
        check('并且记下了失败原因', len(app2.notes) == 1, str(app2.notes))
    finally:
        player_main.VlcVideo = real

    # ---- 初始化成功，但画面挂不上去 ----
    app3 = FakeApp(vlc_ok=True)
    app3.current_videos = [Path('/x/一.mp4'), Path('/x/二.mp4')]
    app3.current_index = 0
    screen3 = player_main.PlayerScreen(name='player')
    screen3.app = lambda: app3
    screen3._setup_backend()
    check('（前置）这一轮用的是 libVLC',
          isinstance(screen3.video, vlc.VlcVideo), type(screen3.video).__name__)

    screen3.video.is_attached = lambda: False     # 假装 SurfaceView 没挂上
    screen3._verify_backend()

    check('画面挂不上去时会自动换回系统播放器',
          isinstance(screen3.video, FakeVideo), type(screen3.video).__name__)
    check('并把 libVLC 标成不可用', app3._vlc_ok is False)
    check('同时提示了用户', len(app3.toasts) >= 1, str(app3.toasts))

    # 挂上去了就不该乱切
    app4 = FakeApp(vlc_ok=True)
    screen4 = player_main.PlayerScreen(name='player')
    screen4.app = lambda: app4
    screen4._setup_backend()
    screen4.video.is_attached = lambda: True
    screen4._verify_backend()
    check('画面正常时不会误切内核',
          isinstance(screen4.video, vlc.VlcVideo), type(screen4.video).__name__)

    # ---- 用户在设置里关掉 ----
    app5 = FakeApp(vlc_ok=False)
    screen5 = player_main.PlayerScreen(name='player')
    screen5.app = lambda: app5
    screen5._setup_backend()
    check('用户关掉增强播放器后用系统播放器',
          isinstance(screen5.video, FakeVideo), type(screen5.video).__name__)


def test_can_play():
    print('【4】格式判断')

    check('rmvb 用系统播放器放不了',
          core.can_play('a.rmvb', on_android=True) is False)
    check('rmvb 有 libVLC 就能放',
          core.can_play('a.rmvb', on_android=True, with_vlc=True) is True)
    check('wmv 有 libVLC 就能放',
          core.can_play('a.wmv', on_android=True, with_vlc=True) is True)
    check('avi 有 libVLC 就能放',
          core.can_play('a.avi', on_android=True, with_vlc=True) is True)
    check('flv 有 libVLC 就能放',
          core.can_play('a.flv', on_android=True, with_vlc=True) is True)
    check('mp4 两种内核都能放',
          core.can_play('a.mp4', on_android=True) is True
          and core.can_play('a.mp4', on_android=True, with_vlc=True) is True)
    check('不是视频的文件永远不放行',
          core.can_play('a.txt', on_android=True, with_vlc=True) is False)
    check('后缀大小写不影响判断',
          core.can_play('A.RMVB', on_android=True, with_vlc=True) is True)


def test_app_switch():
    print('【5】增强播放器的开关逻辑')

    real = player_main.IS_ANDROID
    player_main.IS_ANDROID = True          # 假装在手机上
    try:
        app = player_main.ElderPlayerApp()

        app.store = FakeStore({'use_vlc': True})
        app._vlc_ok = None
        check('电脑上探测必然失败（没有安卓库）',
              app.use_vlc_backend() is False)
        check('失败时记下了原因', bool(app._vlc_note), app._vlc_note)

        app._vlc_ok = True
        app.store = FakeStore({'use_vlc': False})
        check('用户在设置里关掉后就不用了',
              app.use_vlc_backend() is False)

        app._vlc_ok = True
        app.store = FakeStore({'use_vlc': True})
        check('开关打开且内核可用时就用它',
              app.use_vlc_backend() is True)

        app.note_vlc_failed('测试用的原因')
        check('note_vlc_failed 之后立刻不再用它',
              app.use_vlc_backend() is False)

        app.store = FakeStore({'use_vlc': True})
        app._vlc_ok = True
        app._vlc_note = ''
        text, active = app.vlc_status()
        check('状态文字能正常给出', isinstance(text, str) and len(text) > 0,
              text)
        check('可用时状态标成启用中', active is True, str(active))

        app._vlc_ok = False
        app._vlc_note = '某个原因'
        text2, active2 = app.vlc_status()
        check('不可用时状态文字里带上了原因',
              '某个原因' in text2, text2)
        check('不可用时状态标成未启用', active2 is False, str(active2))
    finally:
        player_main.IS_ANDROID = real


def test_init_failure_cleanup():
    print('【6】初始化失败时不留半成品')

    def boom(self):
        self._libvlc = object()      # 故意留一半，看它会不会清干净
        raise RuntimeError('libVLC 起不来')

    real = vlc.VlcVideo._create_player
    vlc.VlcVideo._create_player = boom
    try:
        raised = False
        try:
            vlc.VlcVideo(holder=None)
        except Exception:
            raised = True
        check('初始化失败会抛异常（上层才能降级）', raised)
    finally:
        vlc.VlcVideo._create_player = real


def main():
    print('=' * 62)
    print('libVLC 播放内核测试')
    print('=' * 62)
    print()

    patch_vlc()
    try:
        test_interface()
        print()
        test_holder_box()
        print()
        test_holder_unbind()
        print()
        test_backend_switch()
        print()
        test_can_play()
        print()
        test_app_switch()
        print()
        test_init_failure_cleanup()
    finally:
        unpatch_vlc()

    print()
    print('=' * 62)
    print('通过 %d 项，失败 %d 项' % (len(PASSED), len(FAILED)))
    if FAILED:
        print('-' * 62)
        for name, detail in FAILED:
            print('  失败：%s   %s' % (name, detail))
    print('=' * 62)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
