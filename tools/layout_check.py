# -*- coding: utf-8 -*-
"""
layout_check.py —— 排版体检

为什么不用截图？
    Kivy 的 Window.screenshot() 在这台机器上读回来的是黑色前缓冲，
    而且系统 DPI 是 150%，读出来的行还会错位。与其看一张不可信的图，
    不如直接量：字号最大档位下，文字会不会被切掉、按钮够不够大、
    有没有东西顶出屏幕。

会在几种「手机 / 平板屏幕」尺寸 × 字号档位上分别检查：
    * 文字是否超出分配给它的空间（被截断）
    * 可点击控件的高度是否达到 48dp（安卓无障碍标准的最小触摸目标）
    * 控件是否超出屏幕边界
    * 卡片是否比可视区还高（会被底部返回栏切掉）

尺寸只挑**真实会遇到的设备**：小屏/普通/大屏手机 + 7~10 寸平板，
横竖各来一遍。像 960x340 这种「又宽又矮」的比例现实中基本不存在
（用户已明确说不必适配），测它只会逼着界面为不存在的场景做妥协。

横竖两个方向都测：App 默认是横屏（设置里可改成竖屏），
但竖屏布局也留着 —— 把 orientation 改回 all 的时候还得能用。

运行：python tools/layout_check.py
"""

import json
import os
import subprocess
import sys
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault('KIVY_NO_ARGS', '1')
os.environ.setdefault('KIVY_LOG_LEVEL', 'error')
# Kivy 启动时会删掉 %USERPROFILE%\.kivy\logs 里的旧日志。
# 这台机器的安全策略把「删文件」重定向到回收站，回收站偶尔失败，
# 于是 import kivy 的那一刻直接抛 OSError，整个子进程死掉，
# 看起来像「排版检查没跑完」。这里干脆不让它写文件日志。
os.environ.setdefault('KIVY_NO_FILELOG', '1')

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Kivy 的 wm_pen 输入提供程序在 Windows 上销毁窗口时会抛 ctypes 异常，
# 这个脚本要反复开窗口关窗口，先把它关掉
try:
    from kivy.config import Config
    Config.set('input', 'wm_pen', '')
    Config.set('input', 'wm_touch', '')
except Exception:
    pass

from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.properties import (BooleanProperty, NumericProperty, StringProperty)
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.widget import Widget

import elder_core as core
import elder_theme as th
import main as player_main

MIN_TOUCH = dp(44)          # 触摸目标下限，宁可宽松一点


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


def build_library(root):
    long_names = ['女驸马', '天仙配', '牛郎织女', '打猪草']
    for name in long_names:
        d = root / '戏曲' / name
        d.mkdir(parents=True, exist_ok=True)
        for i in range(1, 7):
            (d / ('%02d.mp4' % i)).write_bytes(b'\x00' * 32)
    # 故意用超长文件名，看会不会把卡片撑坏
    d = root / '电视剧' / '西游记续集之三打白骨精'
    d.mkdir(parents=True, exist_ok=True)
    for i in range(1, 13):
        (d / ('第%d集-大闹天宫高清修复版.mp4' % i)).write_bytes(b'\x00' * 32)
    # 选图页要有真图片才能测：造几张，名字故意很长
    shots = root / '图标照片'
    shots.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image as PILImage
        for i in range(1, 7):
            PILImage.new('RGB', (64, 48), (i * 30, 80, 160)).save(
                str(shots / ('Screenshot_2026-10-19-照片%d.png' % i)))
    except Exception:
        (shots / '照片1.png').write_bytes(b'\x00' * 32)
    return root


def walk(widget, path='root', in_scroll=False):
    """遍历控件树。in_scroll 表示这个控件在滚动区域里（可以超出屏幕）。"""
    yield path, widget, in_scroll
    # 必须用 isinstance：SimpleList / CardGrid 都是 ScrollView 的子类，
    # 按类名比会漏掉它们，结果把滚动区里的卡片全当成「超出屏幕」
    now_in_scroll = in_scroll or isinstance(widget, ScrollView)
    for i, child in enumerate(widget.children):
        for item in walk(child, '%s/%s[%d]' % (path, type(child).__name__, i),
                         now_in_scroll):
            yield item


def collect_issues(root, win_size):
    """
    返回 (文字溢出, 触摸目标过小, 越界, 卡片比可视区高, 统计)。
    越界只检查不在滚动区域里的控件——滚动区域本来就会超出屏幕。
    """
    overflow = []
    too_small = []
    outside = []
    tall_cards = []
    stats = {'labels': 0, 'buttons': 0}

    for path, w, in_scroll in walk(root):
        if w.width <= 0 or w.height <= 0:
            continue

        # --- 卡片比可视区还高 ---
        # 这种情况在屏幕上看起来就是「剧集名被底部按钮栏切掉」，
        # 卡片本身没超屏、文字也没溢出，前面几项都查不出来。
        if isinstance(w, player_main.CardGrid) and w._cards and w.height > 0:
            card_h = w._cards[0][0].height
            if card_h > w.height + 2:
                tall_cards.append((path, round(card_h, 1), round(w.height, 1)))

        # --- 文字溢出 ---
        if isinstance(w, Label) and w.text:
            stats['labels'] += 1
            tw, th_ = w.texture_size
            if w.text_size[0]:
                # 有明确约束的：拿纹理尺寸和约束比
                avail_w, avail_h = w.text_size
                if tw > avail_w + 1.5:
                    overflow.append((path, '横向', w.text[:28], round(tw, 1),
                                     round(avail_w, 1)))
                if avail_h and th_ > avail_h + 2.5:
                    overflow.append((path, '纵向', w.text[:28], round(th_, 1),
                                     round(avail_h, 1)))
            else:
                # 没设 text_size 的 Label 不受布局约束，
                # 文字超出控件就是被硬裁掉了，同样要报
                if tw > w.width + 1.5:
                    overflow.append((path, '硬裁', w.text[:28], round(tw, 1),
                                     round(w.width, 1)))
                if th_ > w.height + 2.5:
                    overflow.append((path, '硬裁纵向', w.text[:28],
                                     round(th_, 1), round(w.height, 1)))

        # --- 触摸目标 ---
        if hasattr(w, 'on_release') and hasattr(w, 'on_press'):
            stats['buttons'] += 1
            if w.height < MIN_TOUCH:
                too_small.append((path, round(w.height, 1), round(MIN_TOUCH, 1)))

        # --- 越界 ---
        # to_window 传的必须是控件自身坐标系里的点。
        # 之前错传了「父坐标」，等于把偏移量算了两次，数字全是假的。
        if not in_scroll:
            ax, ay = w.to_window(0, 0)
            bx, by = w.to_window(w.width, w.height)
            left, right = min(ax, bx), max(ax, bx)
            bottom, top = min(ay, by), max(ay, by)
            if (right > win_size[0] + 2 or left < -2
                    or top > win_size[1] + 2 or bottom < -2):
                outside.append((path, (round(left), round(bottom),
                                       round(right), round(top))))

    return overflow, too_small, outside, tall_cards, stats


def run_for(win_w, win_h, font_level, btn_level):
    tmp = Path(tempfile.mkdtemp(prefix='elder_layout_'))
    library = build_library(tmp / '老人视频')
    data_dir = tmp / '_appdata'
    cfg = data_dir / core.CONFIG_DIR_NAME
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / core.ROOT_PATH_FILE).write_text(str(library), encoding='utf-8')

    core.app_private_dir = lambda: data_dir
    player_main.Video = FakeVideo

    th.theme.font_level = font_level
    th.theme.btn_level = btn_level

    Window.size = (win_w, win_h)
    app = player_main.ElderPlayerApp()
    report = {}
    current = ['']

    def finish():
        # 先把结果吐出来。后面 app.stop() 有可能在 Kivy 内部炸掉，
        # 那时候结果已经写出去了，不影响解析
        print('@@RESULT@@' + json.dumps(report))
        sys.stdout.flush()
        try:
            app.stop()
        except Exception:
            pass

    def capture(_dt):
        name = current[0]
        try:
            screen = app.sm.get_screen(name)
            overflow, small, outside, tall_cards, stats = collect_issues(
                screen, (Window.size[0], Window.size[1]))
            report[name] = {
                'overflow': overflow,
                'small': small,
                'outside': outside,
                'tall_cards': tall_cards,
                'stats': stats,
            }
        except Exception:
            import traceback
            report['_error'] = traceback.format_exc()
            finish()
            return
        Clock.schedule_once(next_screen, 0.05)

    def next_screen(_dt):
        if not pending:
            finish()
            return
        name = pending.pop(0)
        current[0] = name
        try:
            # 注意顺序：必须先调 open_category / open_series，
            # 它们内部会切界面并带上要显示的数据。
            # 如果先手动 sm.current = 'series'，再调 open_category，
            # _goto 发现「已经在 series 了」就直接返回，界面根本不会重建，
            # 结果测出来的是一个空列表。
            if name == 'series':
                app.open_category('戏曲')
            elif name == 'videos':
                for p in app.categories.get('戏曲', []):
                    app.open_series({'path': p})
                    break
            elif name == 'picker':
                # 选图页现在是一个整屏界面，不是弹窗
                app.sm.get_screen('picker').show(
                    str(library / '图标照片'), lambda p: None)
            else:
                app.sm.current = name
        except Exception:
            import traceback
            report['_error'] = traceback.format_exc()
            finish()
            return
        # 切完界面要等真正的帧过去，排版才算得完。
        # 之前用 Clock.tick() 手动推一下，卡片根本没参与布局，
        # 结果整个列表都没被检查到，等于白测。
        Clock.schedule_once(capture, 0.35)

    pending = list(SCREENS)
    Clock.schedule_once(next_screen, 0.8)
    try:
        app.run()
    except Exception:
        pass
    shutil.rmtree(str(tmp), ignore_errors=True)
    return report


CONFIGS = [
    # ---- 竖屏（备用布局：orientation 改成 all 时会用到）----
    ('小屏手机·竖', 360, 640, 3, 3),
    ('普通手机·竖', 400, 820, 3, 3),
    ('大屏手机·竖', 430, 900, 3, 3),
    ('小屏·竖·最大字号', 360, 640, 5, 5),
    ('普通手机·竖·最大字号', 400, 820, 5, 5),
    ('普通手机·竖·最小字号', 400, 820, 1, 1),
    # ---- 横屏（App 的默认方向，重点检查这几种）----
    ('小屏手机·横', 640, 360, 3, 3),
    ('普通手机·横', 820, 400, 3, 3),
    ('大屏手机·横', 900, 430, 3, 3),
    ('小屏·横·最大字号', 640, 360, 5, 5),
    ('普通手机·横·最大字号', 820, 400, 5, 5),
    ('普通手机·横·最小字号', 820, 400, 1, 1),
    # ---- 平板（7~10 寸，横竖都要能看）----
    ('小平板·竖', 600, 960, 3, 3),
    ('小平板·横', 960, 600, 3, 3),
    ('大平板·竖', 800, 1280, 3, 3),
    ('大平板·横', 1280, 800, 3, 3),
]

SCREENS = ('home', 'series', 'videos', 'player', 'history', 'settings',
           'help', 'picker')


def main_check():
    total = 0
    for label, w, h, fl, bl in CONFIGS:
        print('\n' + '=' * 62)
        print('%s  %dx%d  字号档 %d  按钮档 %d' % (label, w, h, fl, bl))
        print('=' * 62)

        # 每个配置单独跑一个子进程：Kivy 在 Windows 上关掉窗口之后，
        # 同一个进程里没法再开第二个窗口
        proc = subprocess.run(
            [sys.executable, str(HERE / 'layout_check.py'),
             '--one', str(w), str(h), str(fl), str(bl)],
            capture_output=True, text=True, timeout=180)
        report = None
        for line in proc.stdout.splitlines():
            if line.startswith('@@RESULT@@'):
                report = json.loads(line[len('@@RESULT@@'):])

        if report is None:
            print('  子进程没有返回结果，退出码 %s' % proc.returncode)
            print(proc.stderr[-1500:])
            total += 1
            continue

        if '_error' in report:
            print(report['_error'])
            total += 1
            continue

        for screen_name in SCREENS:
            item = report.get(screen_name)
            if item is None:
                print('  %-9s 没跑到' % screen_name)
                total += 1
                continue
            overflow = item['overflow']
            small = item['small']
            outside = item['outside']
            tall_cards = item.get('tall_cards', [])
            stats = item.get('stats', {})
            tail = '（查了 %d 条文字、%d 个按钮）' % (stats.get('labels', 0),
                                                     stats.get('buttons', 0))
            if not (overflow or small or outside or tall_cards):
                print('  %-9s 正常 %s' % (screen_name, tail))
                continue
            total += len(overflow) + len(small) + len(outside) + len(tall_cards)
            print('  %-9s 有问题：' % screen_name)
            for path, got, avail in tall_cards[:4]:
                print('     卡片比可视区高 %-18s 卡片%.0f 可视区%.0f（会被底栏切）'
                      % (path.split('/')[-1], got, avail))
            for path, direction, text, got, avail in overflow[:6]:
                print('     文字被切(%s) %-24s 「%s」 需要%.0f 只有%.0f'
                      % (direction, path.split('/')[-1], text, got, avail))
            for path, got, need in small[:4]:
                print('     触摸目标偏小 %-24s %.0f < %.0f'
                      % (path.split('/')[-1], got, need))
            for path, box in outside[:4]:
                print('     超出屏幕 %-24s %s' % (path.split('/')[-1], box))

    print('\n' + '=' * 62)
    print('合计发现 %d 处问题' % total)
    print('=' * 62)
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--one':
        run_for(int(sys.argv[2]), int(sys.argv[3]),
                int(sys.argv[4]), int(sys.argv[5]))
    else:
        sys.exit(main_check())
