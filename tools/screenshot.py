# -*- coding: utf-8 -*-
"""
screenshot.py —— 把各个界面截下来，肉眼确认排版有没有崩（共 12 张）

用假视频控件跑（桌面没有安卓的解码器），主要看布局、字号、配色、中文显示。

运行：python tools/screenshot.py
输出：tools/shots/*.png

截图方式：另起一个进程，让**系统**截全屏，再按窗口矩形裁出来。

试过两条路都不行：
  * Kivy 的 Window.screenshot() —— 在 Windows 高缩放（125%/150%）下会读到半帧的
    缓冲，出来的图行错位甚至纯黑；
  * 在本进程里调 GDI 截屏 —— 窗口是自己画的 OpenGL 画面，GDI 拿不到，截出来全黑。
所以每次截图都 fork 一个纯 Pillow 的小进程去抓，抓完就退出。
"""

import os
import sys
import shutil
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHOTS = HERE / 'shots'

# --------------------------------------------------------------- 截图子进程
# 这个分支必须在 import kivy 之前处理：子进程只做截屏，不要建窗口
if __name__ == '__main__' and '--grab' in sys.argv:
    import ctypes
    import time
    from ctypes import wintypes

    from PIL import ImageGrab

    name = sys.argv[sys.argv.index('--grab') + 1]
    SHOTS.mkdir(parents=True, exist_ok=True)

    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()          # 否则坐标会被系统按缩放虚拟化

    hwnd = user32.FindWindowW(None, '老年视频播放器')
    if not hwnd:
        # 找不到窗口就别截：截全屏只会得到一张「别人家窗口」的图，
        # 混在产物里比没有更糟
        print('WARN 没找到播放器窗口，放弃这次截图', file=sys.stderr)
        sys.exit(2)

    # 提到最前 —— 否则可能截到盖在上面的浏览器/编辑器。
    #
    # 光调 SetForegroundWindow 不一定管用：Windows 不允许非前台进程
    # 抢焦点，调用会被静默忽略，结果截到的是别人家的窗口（踩过）。
    # 这里用 AttachThreadInput 把本线程挂到当前前台窗口的输入队列上，
    # 再 SetForegroundWindow 就能生效；最后校验一次是不是真到前台了。
    try:
        kernel32 = ctypes.windll.kernel32
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = kernel32.GetCurrentThreadId()
        if tid_fg and tid_fg != tid_me:
            user32.AttachThreadInput(tid_fg, tid_me, True)
        user32.ShowWindow(hwnd, 9)                       # SW_RESTORE
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                            0x0001 | 0x0002)             # HWND_TOPMOST
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0,
                            0x0001 | 0x0002)             # HWND_NOTOPMOST
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
        if tid_fg and tid_fg != tid_me:
            user32.AttachThreadInput(tid_fg, tid_me, False)
    except Exception:
        pass
    time.sleep(0.4)
    if user32.GetForegroundWindow() != hwnd:
        # 抢不到焦点就直说，别拿一张别人的截图冒充
        print('WARN 截图时窗口没到前台，这张图可能不对', file=sys.stderr)

    img = ImageGrab.grab()
    # 只取客户区：去掉标题栏和窗口边框，留下的是 Kivy 真正的绘制区域
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    box = (pt.x, pt.y, pt.x + r.right, pt.y + r.bottom)
    if box[2] > box[0] and box[3] > box[1]:
        img = img.crop(box)
    img.save(str(SHOTS / (name + '.png')))
    sys.exit(0)

os.environ.setdefault('KIVY_NO_ARGS', '1')
os.environ.setdefault('KIVY_LOG_LEVEL', 'error')
# 别让 Kivy 去删旧日志：这台机器的安全策略会把删除重定向到回收站，
# 回收站偶尔失败就直接抛异常，import 阶段就死了
os.environ.setdefault('KIVY_NO_FILELOG', '1')

sys.path.insert(0, str(HERE.parent))

import subprocess

from kivy.clock import Clock
from kivy.core.window import Window
from kivy.properties import (BooleanProperty, NumericProperty, StringProperty)
from kivy.uix.widget import Widget

import elder_core as core
import main as player_main


class FakeVideo(Widget):
    source = StringProperty('')
    state = StringProperty('stop')
    duration = NumericProperty(0)
    position = NumericProperty(0)
    volume = NumericProperty(1.0)
    allow_stretch = BooleanProperty(True)
    keep_ratio = BooleanProperty(True)

    def seek(self, ratio):
        self.position = max(0.0, min(1.0, float(ratio))) * self.duration


def build_photos(root):
    """给选图页造几张真照片 —— 只是几个纯色块 + 大数字，够看出缩略图生效了。

    选图页要看的正是「有没有图、认不认得出是哪张」，
    没有真图片文件就截不出任何说服力。
    """
    from PIL import Image, ImageDraw

    folder = root / '相册'
    folder.mkdir(parents=True, exist_ok=True)
    palette = [(214, 88, 78), (72, 138, 214), (86, 168, 108),
               (232, 168, 62), (140, 106, 190), (60, 150, 158)]
    for i, color in enumerate(palette, 1):
        img = Image.new('RGB', (640, 480), color)
        d = ImageDraw.Draw(img)
        d.ellipse([220, 140, 420, 340], fill=(255, 255, 255))
        d.text((300, 220), str(i), fill=color)
        img.save(str(folder / ('照片%d.jpg' % i)), quality=82)
    return folder


def build_library(root):
    for series, count in (('女驸马', 6), ('天仙配', 3)):
        d = root / '戏曲' / series
        d.mkdir(parents=True, exist_ok=True)
        for i in range(1, count + 1):
            (d / ('%02d.mp4' % i)).write_bytes(b'\x00' * 32)
    d = root / '电视剧' / '西游记'
    d.mkdir(parents=True, exist_ok=True)
    for i in range(1, 13):
        (d / ('第%d集.mp4' % i)).write_bytes(b'\x00' * 32)
    return root


def main_shot():
    tmp = Path(tempfile.mkdtemp(prefix='elder_shot_'))
    if SHOTS.exists():
        shutil.rmtree(str(SHOTS))
    SHOTS.mkdir(parents=True, exist_ok=True)
    os.chdir(str(SHOTS))

    library = build_library(tmp / '老人视频')
    photos = build_photos(tmp)
    data_dir = tmp / '_appdata'
    cfg = data_dir / core.CONFIG_DIR_NAME
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / core.ROOT_PATH_FILE).write_text(str(library), encoding='utf-8')

    core.app_private_dir = lambda: data_dir
    player_main.Video = FakeVideo
    # App 默认横屏（设置里可以改成竖屏），截图就按横屏手机的比例来，
    # 否则截出来的是竖屏布局，跟真机上看到的不一样。
    Window.size = (860, 420)

    app = player_main.ElderPlayerApp()

    def step_home():
        app.sm.current = 'home'

    def step_series():
        app.open_category('戏曲')

    def step_videos():
        target = None
        for p in app.categories.get('戏曲', []):
            if p.name == '女驸马':
                target = p
        app.open_series({'path': target})

    def step_player():
        app.open_video({'index': 1})
        ps = app.sm.get_screen('player')
        ps.video.duration = 2716000.0
        ps.video.position = 1284000.0
        ps.now_playing.text = '女驸马  ·  第 2 / 6 集'
        ps.seek.value = 0.47
        ps.time_label.text = '21:24 / 45:16'

    def step_history():
        app.store.upsert_record(library / '戏曲' / '女驸马', 1, 1284000)
        app.store.upsert_record(library / '电视剧' / '西游记', 4, 620000)
        app.store.reload_records()
        app.sm.current = 'history'

    def step_settings():
        app.sm.current = 'settings'

    def set_settings_scroll(value):
        """把设置页往下滚，好拍到中间和底部的卡片。"""
        def _do(_dt):
            screen = app.sm.get_screen('settings')
            for widget in screen.content.children:
                if isinstance(widget, player_main.SimpleList):
                    widget.scroll_y = value
                    return
        Clock.schedule_once(_do, 0.2)

    def step_settings_mid():
        app.sm.current = 'settings'
        # 主页图标卡片比一屏还高一点，往上滚到它开头的位置，
        # 这样「戏曲 / 电视剧 / 历史播放」三行能连着看见
        set_settings_scroll(0.42)

    def step_settings_orient():
        app.sm.current = 'settings'
        set_settings_scroll(0.82)

    def step_settings_bottom():
        app.sm.current = 'settings'
        set_settings_scroll(0.0)

    def step_settings_about():
        app.sm.current = 'settings'
        set_settings_scroll(0.16)

    def step_help():
        app.sm.current = 'help'

    def step_picker():
        # 直接指到造好的相册目录，不然截出来的是这台电脑的用户目录
        app.sm.get_screen('picker').show(str(photos), lambda path: None)

    plan = [
        ('01_home', step_home),
        ('02_series', step_series),
        ('03_videos', step_videos),
        ('04_player', step_player),
        ('05_history', step_history),
        ('06_settings', step_settings),
        ('06b_settings_orient', step_settings_orient),
        ('07_settings_mid', step_settings_mid),
        ('07b_picker', step_picker),
        ('08_settings_about', step_settings_about),
        ('09_settings_bottom', step_settings_bottom),
        ('10_help', step_help),
    ]

    # 一步步来：切屏 -> 等画面渲染 -> 起子进程抓图 -> 等它退出 -> 下一步。
    #
    # 关键是「不能阻塞主循环」：
    #   * 用 subprocess.run 等子进程，Kivy 的消息循环就停了，窗口会被系统当成
    #     「未响应」，GDI 抓到的是纯黑；
    #   * 不等待又会让子进程跑到下一屏，图名对不上。
    # 所以用 Popen + Clock 轮询，主循环继续跑，两边都不误。
    state = {'index': 0}

    def next_step(_dt=None):
        index = state['index']
        if index >= len(plan):
            app.stop()
            return
        name, action = plan[index]
        action()
        # 留一点时间让这一屏真正画出来
        Clock.schedule_once(lambda dt: grab(name), 0.45)

    def grab(name):
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--grab', name])
        poll(proc, name)

    def poll(proc, name):
        if proc.poll() is None:
            Clock.schedule_once(lambda dt: poll(proc, name), 0.1)
            return
        state['index'] += 1
        Clock.schedule_once(next_step, 0.15)

    Clock.schedule_once(next_step, 1.0)

    app.run()

    shutil.rmtree(str(tmp), ignore_errors=True)
    files = sorted(SHOTS.glob('*.png'))
    print('\n截图 %d 张：' % len(files))
    for f in files:
        print('  %s  %d 字节' % (f.name, f.stat().st_size))
    return 0


if __name__ == '__main__':
    sys.exit(main_shot())
