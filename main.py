# -*- coding: utf-8 -*-
"""
main.py —— 老年视频播放器（安卓版）

原 PyQt5 版本的界面层在这里用 Kivy 重写，业务逻辑来自 elder_core。

界面一共六个：
    home      主页：三个大按钮（戏曲 / 电视剧 / 历史播放）+ 刷新 + 设置
    series    某个分类下的剧集列表
    videos    某个剧集下的视频列表
    player    播放界面：视频 + 进度条 + 快退/播放/快进 + 音量 + 返回
    history   历史播放
    settings  设置：字体大小、按钮大小、缩略图开关、主文件夹
另外还有一个 help 界面，用来在「没有权限 / 文件夹不存在」时手把手教用户。

安卓特有的适配都集中在文件后半部分的 AndroidBridge 里：
屏幕常亮、固定横屏、音量控制、后台返回、存储权限。
桌面端这些调用会自动跳过，方便在电脑上调试。
"""

import math
import os
import sys
from pathlib import Path

# Kivy 的窗口标题 / 图标
from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.properties import BooleanProperty, ListProperty, NumericProperty, StringProperty
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager, SlideTransition
from kivy.uix.scrollview import ScrollView
from kivy.uix.video import Video
from kivy.uix.widget import Widget
from kivy.utils import platform

import elder_core as core
import elder_theme as th
from elder_core import (CAT_HISTORY, CAT_HUANGMEI, CAT_TV, DEFAULT_ROOT_NAME,
                        RESUME_OFFSET, MIN_RESUME_DURATION,
                        Store, can_play, format_time, list_videos,
                        natural_key, scan_categories, scan_root_report)
from elder_theme import (COLORS, CJK_FONT, BigEntryButton, BigEntryTile, Card,
                         GradientBackground, PillButton, SectionLabel,
                         SpeakerIcon, TapCard, ThumbArea, TileCard, dp, sp, theme,
                         forget_icon_texture, load_icon_texture,
                         load_photo_texture)
from elder_thumbs import ThumbnailLoader
from elder_vlc import VlcVideo

IS_ANDROID = (platform == 'android')

# ---------------------------------------------------------------------------
#  屏幕方向：默认横屏，用户可以在「设置 → 打开时的屏幕方向」里改成竖屏
#
#  为什么默认横屏：老人一般把手机横过来看戏，竖着拿的时候字再大也看不了几行；
#  而且横屏下主页能把三个大入口并排摆开，一屏点完，不用上下找。
#
#  这里不再是写死的常量 —— 存在 store 的 orientation 设置里。
#  注意 buildozer.spec 里的 orientation 必须是 all，否则系统层面就锁死了，
#  用户在设置里改成竖屏也不会生效。
# ---------------------------------------------------------------------------


# ============================================================================
#  安卓桥接层 —— 所有跟系统打交道的调用都塞在这里，桌面端自动降级
# ============================================================================
class AndroidBridge(object):

    STREAM_MUSIC = 3

    @staticmethod
    def _activity():
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        return PythonActivity.mActivity

    @staticmethod
    def _on_ui_thread(fn):
        """把一段调用丢到安卓 UI 线程执行。"""
        try:
            from android.runnable import run_on_ui_thread
            run_on_ui_thread(fn)()
        except Exception:
            try:
                fn()
            except Exception:
                pass

    # ---- 存储权限 ----
    @staticmethod
    def request_storage_permissions(callback):
        if not IS_ANDROID:
            callback(True)
            return

        def _do():
            try:
                from android.permissions import (Permission, check_permission,
                                                 request_permissions)
            except Exception:
                callback(False)
                return

            wanted = []
            for name in ('READ_MEDIA_VIDEO', 'READ_MEDIA_AUDIO',
                         'READ_EXTERNAL_STORAGE', 'WRITE_EXTERNAL_STORAGE'):
                perm = getattr(Permission, name, None)
                if perm is not None and perm not in wanted:
                    wanted.append(perm)

            def _done(_perms, grants):
                Clock.schedule_once(lambda dt: callback(any(grants)), 0)

            try:
                request_permissions(wanted, _done)
            except Exception:
                callback(False)

        Clock.schedule_once(lambda dt: _do(), 0)

    @staticmethod
    def has_all_files_access():
        """有没有拿到「所有文件访问权限」（Android 11+ 才有这个概念）。"""
        if not IS_ANDROID:
            return True
        try:
            from jnius import autoclass
            Build = autoclass('android.os.Build$VERSION')
            if int(Build.SDK_INT) < 30:
                return True
            Environment = autoclass('android.os.Environment')
            return bool(Environment.isExternalStorageManager())
        except Exception:
            return False

    @staticmethod
    def open_all_files_access_settings():
        """
        跳到系统的「所有文件访问权限」页面。
        有了这个权限，应用才能自己创建主文件夹、才能读到 rmvb 这类
        没被系统媒体库收录的文件。
        """
        if not IS_ANDROID:
            return False
        try:
            from jnius import autoclass
            activity = AndroidBridge._activity()
            Settings = autoclass('android.provider.Settings')
            Intent = autoclass('android.content.Intent')
            Uri = autoclass('android.net.Uri')
            pkg = activity.getPackageName()

            def _go():
                try:
                    intent = Intent(
                        Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                    intent.setData(Uri.parse('package:' + str(pkg)))
                    activity.startActivity(intent)
                except Exception:
                    try:
                        intent = Intent(
                            Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION)
                        activity.startActivity(intent)
                    except Exception:
                        pass

            AndroidBridge._on_ui_thread(_go)
            return True
        except Exception:
            return False

    # ---- 屏幕常亮 ----
    @staticmethod
    def keep_screen_on(enabled):
        if not IS_ANDROID:
            return
        try:
            from jnius import autoclass
            activity = AndroidBridge._activity()
            LayoutParams = autoclass('android.view.WindowManager$LayoutParams')
            flag = LayoutParams.FLAG_KEEP_SCREEN_ON

            def _apply():
                try:
                    window = activity.getWindow()
                    if enabled:
                        window.addFlags(flag)
                    else:
                        window.clearFlags(flag)
                except Exception:
                    pass

            AndroidBridge._on_ui_thread(_apply)
        except Exception:
            pass

    # ---- 屏幕方向 ----
    @staticmethod
    def set_orientation(landscape):
        """
        landscape=True 横屏锁定，False 竖屏锁定，None 交还给系统（跟着重力感应）。

        真正的锁定发生在两个地方，缺一不可：
          * buildozer.spec 的 orientation —— 决定 App 启动那一刻是不是横的
          * 这里 —— 个别机型（尤其多窗口模式下）会忽略 manifest，得运行时再钉一次
        """
        if not IS_ANDROID:
            return
        try:
            from jnius import autoclass
            activity = AndroidBridge._activity()
            ActivityInfo = autoclass('android.content.pm.ActivityInfo')
            if landscape is None:
                target = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
            elif landscape:
                target = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
            else:
                target = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT

            def _apply():
                try:
                    activity.setRequestedOrientation(target)
                except Exception:
                    pass

            AndroidBridge._on_ui_thread(_apply)
        except Exception:
            pass

    @staticmethod
    def apply_default_orientation(landscape=None):
        """
        把屏幕扳回 App 的默认方向（默认横屏，用户可以在设置里改）。

        离开播放界面时调这个，而不是硬切成某个方向 —— 否则整个界面会原地
        转一圈，看着像闪屏。

        landscape 不传就按当前设置来。
        """
        if landscape is None:
            try:
                landscape = App.get_running_app().store.default_landscape()
            except Exception:
                landscape = True
        AndroidBridge.set_orientation(bool(landscape))

    # ---- 音量（直接调系统媒体音量，老人不用再去按侧键找）----
    @staticmethod
    def audio_manager():
        if not IS_ANDROID:
            return None
        try:
            from jnius import autoclass
            activity = AndroidBridge._activity()
            Context = autoclass('android.content.Context')
            return activity.getSystemService(Context.AUDIO_SERVICE)
        except Exception:
            return None

    @staticmethod
    def volume_range():
        am = AndroidBridge.audio_manager()
        if am is None:
            return (0, 100)
        try:
            return (0, int(am.getStreamMaxVolume(AndroidBridge.STREAM_MUSIC)))
        except Exception:
            return (0, 100)

    @staticmethod
    def volume_current():
        am = AndroidBridge.audio_manager()
        if am is None:
            return 60
        try:
            return int(am.getStreamVolume(AndroidBridge.STREAM_MUSIC))
        except Exception:
            return 60

    @staticmethod
    def set_volume(value):
        am = AndroidBridge.audio_manager()
        if am is None:
            return False
        try:
            am.setStreamVolume(AndroidBridge.STREAM_MUSIC, int(value), 0)
            return True
        except Exception:
            return False


# ============================================================================
#  一些小控件
# ============================================================================
class SeekBar(Widget):
    """
    进度条 / 音量条。
    Kivy 自带的 Slider 又细又不好看，而且对老人来说太难点，
    所以自己画一个：粗轨道 + 圆头把手，整条都能点。
    """

    value = NumericProperty(0.0)          # 0 ~ 1
    fill_color = ListProperty(list(COLORS['blue']))
    track_color = ListProperty([0.78, 0.82, 0.86, 0.65])
    knob_color = ListProperty(list(COLORS['blue_dark']))
    on_seek = None                        # 回调：def cb(ratio)

    def __init__(self, **kwargs):
        self.on_seek = kwargs.pop('on_seek', None)
        kwargs.setdefault('size_hint_y', None)
        kwargs.setdefault('height', dp(46))
        super(SeekBar, self).__init__(**kwargs)
        self._dragging = False
        from kivy.graphics import Color, Ellipse, RoundedRectangle
        with self.canvas:
            self._track_color = Color(*self.track_color)
            self._track = RoundedRectangle(pos=self.pos, size=self.size)
            self._fill_color = Color(*self.fill_color)
            self._fill = RoundedRectangle(pos=self.pos, size=self.size)
            self._knob_color = Color(*self.knob_color)
            self._knob = Ellipse(pos=self.pos, size=(dp(1), dp(1)))
        self.bind(pos=self._sync, size=self._sync, value=self._sync,
                  fill_color=self._sync_colors, track_color=self._sync_colors,
                  knob_color=self._sync_colors)
        self._sync_colors()
        self._sync()

    def _sync_colors(self, *_):
        self._track_color.rgba = self.track_color
        self._fill_color.rgba = self.fill_color
        self._knob_color.rgba = self.knob_color

    def _sync(self, *_):
        h = dp(14)
        track_h = max(dp(8), h * 0.6)
        y = self.y + (self.height - track_h) / 2.0
        r = track_h / 2.0
        self._track.pos = (self.x, y)
        self._track.size = (max(0, self.width), track_h)
        self._track.radius = [r]

        ratio = max(0.0, min(1.0, float(self.value)))
        fill_w = max(track_h, self.width * ratio) if self.width > 0 else 0
        self._fill.pos = (self.x, y)
        self._fill.size = (fill_w, track_h)
        self._fill.radius = [r]

        knob_d = max(dp(30), track_h * 2.4)
        self._knob.size = (knob_d, knob_d)
        self._knob.pos = (self.x + self.width * ratio - knob_d / 2.0,
                          self.center_y - knob_d / 2.0)

    def _ratio_from_x(self, x):
        if self.width <= 0:
            return 0.0
        return max(0.0, min(1.0, (x - self.x) / float(self.width)))

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return False
        self._dragging = True
        touch.grab(self)
        self._apply(touch.x)
        return True

    def on_touch_move(self, touch):
        if self._dragging and touch.grab_current is self:
            self._apply(touch.x)
            return True
        return False

    def on_touch_up(self, touch):
        if self._dragging and touch.grab_current is self:
            touch.ungrab(self)
            self._dragging = False
            self._apply(touch.x, final=True)
            return True
        return False

    def _apply(self, x, final=False):
        ratio = self._ratio_from_x(x)
        if self.on_seek is not None:
            try:
                self.on_seek(ratio, final)
            except TypeError:
                self.on_seek(ratio)


class BackButton(PillButton):
    """返回按钮：原版的 #FDCD60 底色。"""

    def __init__(self, text='⬅ 返回', callback=None, **kwargs):
        bg = list(COLORS['back_btn'])
        kwargs.setdefault('bg_color', bg)
        kwargs.setdefault('text_color', list(COLORS['text']))
        super(BackButton, self).__init__(text=text, **kwargs)
        # 按下时用深一点的黄，比原版多了点反馈
        self.pressed_color = th.shade(bg, 0.86)
        if callback is not None:
            self.bind(on_release=lambda *_: callback())


class FlatButton(PillButton):
    """带自定义底色的通用按钮。"""

    def __init__(self, text='', bg=None, fg=None, callback=None, **kwargs):
        bg = list(bg or COLORS['blue'])
        kwargs.setdefault('bg_color', bg)
        kwargs.setdefault('text_color', list(fg or COLORS['white']))
        super(FlatButton, self).__init__(text=text, **kwargs)
        self.pressed_color = th.shade(bg, 0.86)
        if callback is not None:
            self.bind(on_release=lambda *_: callback())


class CardGrid(ScrollView):
    """
    卡片网格，列数跟着宽度自动算。
    剧集列表、视频列表都用它，对应原版 QListWidget 的 IconMode。

    卡片宽度由 GridLayout 平分（每张 size_hint_x=1），
    高度在这里根据实际列宽算出来，保证缩略图区域接近正方形。
    """

    def __init__(self, tile_size=None, on_card_tap=None, **kwargs):
        kwargs.setdefault('do_scroll_x', False)
        kwargs.setdefault('bar_width', dp(8))
        super(CardGrid, self).__init__(**kwargs)
        self.tile_size = tile_size or dp(160)
        self.on_card_tap = on_card_tap
        self._cards = []
        self.grid = GridLayout(cols=4, spacing=dp(12), padding=dp(10),
                               size_hint_y=None)
        self.grid.bind(minimum_height=self.grid.setter('height'),
                       width=self._on_grid_width)
        self.add_widget(self.grid)
        self.bind(width=self._relayout, height=self._relayout)

    def clear(self):
        self.grid.clear_widgets()
        self._cards = []

    def set_cards(self, entries):
        """
        entries: [{'title':..., 'path':..., 'index':..., 'tone':...}, ...]
        """
        self.clear()
        for entry in entries:
            card = TileCard(
                title=entry['title'],
                fallback_color=entry.get('tone', COLORS['blue']),
                tile_size=self.tile_size,
            )
            card._payload = entry
            card.bind(on_release=self._make_tap(entry))
            self.grid.add_widget(card)
            self._cards.append((card, entry['path']))
        self._relayout()

    def _make_tap(self, entry):
        def _tap(_card):
            if self.on_card_tap:
                self.on_card_tap(entry)
        return _tap

    def _vis_width(self):
        """
        真正能摆卡片的宽度。

        ScrollView 的竖向滚动条是**盖在内容上面**的，不占布局宽度：
        grid 的宽度等于整个 ScrollView 的宽度，最右边那张卡片右边那一小条
        就被滚动条压住了，看起来像被切掉。列宽按「减掉滚动条」来算。
        """
        return max(dp(90), self.width - self.bar_width)

    def _cols(self):
        avail = self._vis_width() - dp(20)
        step = self.tile_size + dp(12)
        return max(1, int(avail // step)) if avail > 0 else 1

    def _col_width(self, cols):
        # 注意：GridLayout 在卡片没填满一整行的时候，实际用的列数会少于
        # self.grid.cols（实测 2 张卡片摆 4 列，最后还是 2 列平分整行，
        # 卡片实际 582 宽，而这里算出来 288）。
        # 所以这个值只能当「大致列宽」用 —— 它只影响缩略图的长宽比，
        # 缩略图是 cover 裁切的，宽一点窄一点都不会变形。
        # 高度不跟着它走，只受下面「可视区高度」约束，所以没关系。
        avail = min(self.grid.width, self._vis_width()) - dp(20) - (cols - 1) * dp(12)
        return max(dp(90), avail / float(cols))

    def _relayout(self, *_):
        cols = self._cols()
        if self.grid.cols != cols:
            self.grid.cols = cols
        self._on_grid_width()

    def _on_grid_width(self, *_):
        """列宽定了之后，把卡片高度按列宽重算一遍。"""
        if not self._cards:
            return
        cols = max(1, self.grid.cols)
        col_w = self._col_width(cols)
        want = col_w + dp(78)
        # 卡片不能比可视区还高。
        # 横屏手机上可视区很矮（实测 246dp），如果缩略图非要摆成正方形，
        # 一张卡片就比可视区高 —— 卡片下半截的剧集名正好被底部返回栏切掉，
        # 老人得先往下滑一点才看得见名字，很容易以为坏了。
        # 宁可把缩略图压扁一点，也要让一张卡片整整齐齐地露出来。
        if self.height > 0:
            room = self.height - dp(20)          # grid 上下各 10dp 的内边距
            # 这里**不能**再给「最小 150dp」这种下限。之前写过
            # max(dp(150), room)，结果横屏手机可视区只有 138 时，下限自己
            # 就把卡片顶到 225（实测），比可视区还高 —— 等于没夹住。
            # 可视区矮就只能矮，宁可缩略图小一点，也不能让卡片露不全。
            if room > 0:
                want = min(want, room)
        for card, _path in self._cards:
            card.height = want


class SimpleList(ScrollView):
    """设置页用的竖向列表。"""

    def __init__(self, **kwargs):
        kwargs.setdefault('do_scroll_x', False)
        super(SimpleList, self).__init__(**kwargs)
        self.box = BoxLayout(orientation='vertical', spacing=dp(10),
                             padding=dp(10), size_hint_y=None)
        self.box.bind(minimum_height=self.box.setter('height'))
        self.add_widget(self.box)

    def clear(self):
        self.box.clear_widgets()

    def add(self, widget):
        self.box.add_widget(widget)


# ============================================================================
#  各个界面
# ============================================================================
def folder_tree_text():
    """
    目录结构示例。设置页的「关于」和引导页共用这一份。

    缩进用**全角空格**（U+3000），每一层固定 3 个：
    等宽，换行之后竖着看还是齐的。
    不用「│」画竖线 —— 这个字体里树线字符的宽度不统一，画出来是歪的。
    """
    def line(depth, branch, name):
        return '　' * (1 + 3 * (depth - 1)) + branch + ' ' + name

    rows = [
        '老人视频（或您自定义的主文件夹）/',
        line(1, '├──', '戏曲/'),
        line(2, '├──', '女驸马/'),
        line(3, '├──', '女驸马01.mp4'),
        line(3, '└──', '女驸马02.mp4'),
        line(2, '└──', '天仙配/'),
        line(3, '└──', '天仙配.mp4'),
        line(1, '└──', '电视剧/'),
        line(2, '└──', '西游记/'),
        line(3, '├──', '01.mp4'),
        line(3, '└──', '02.mp4'),
    ]
    return '\n'.join(rows)


class BaseScreen(Screen):
    """所有界面的基类：统一的渐变背景 + 尺寸变化时重建界面。"""

    def __init__(self, **kwargs):
        super(BaseScreen, self).__init__(**kwargs)
        self.root_layout = FloatLayout()
        self.add_widget(self.root_layout)
        self.bg = GradientBackground()
        self.root_layout.add_widget(self.bg)
        self.content = BoxLayout(orientation='vertical',
                                 padding=[dp(14), dp(14), dp(14), dp(14)],
                                 spacing=dp(10),
                                 size_hint=(1, 1))
        self.root_layout.add_widget(self.content)
        theme.bind_change(self._on_theme_change)

    def _on_theme_change(self):
        Clock.schedule_once(lambda dt: self.rebuild(), 0)

    def rebuild(self):
        """子类覆盖：清空并重新构建 content 里的东西。"""
        pass

    def app(self):
        return App.get_running_app()


def build_topbar(title_text, buttons):
    """
    顶部栏：左边标题，右边若干按钮。返回 (boxlayout, title_label)
    """
    bar = BoxLayout(orientation='horizontal', size_hint_y=None,
                    height=max(dp(58), theme.ds('pill_h') + dp(6)),
                    spacing=dp(10))
    label = Label(text=title_text, font_name=CJK_FONT,
                  font_size=theme.fs('title'), bold=True,
                  color=list(COLORS['text_title']),
                  halign='left', valign='middle')
    label.bind(size=lambda w, *_: setattr(w, 'text_size', (w.width, w.height)))
    bar.add_widget(label)
    for btn in buttons:
        bar.add_widget(btn)
    return bar, label


def build_bottombar(*buttons):
    """
    底部按钮栏。

    返回键统一放在屏幕最下面：老人单手拿手机时拇指够得到上面，
    但够不到顶栏 —— 顶栏在横屏时尤其远。
    """
    bar = BoxLayout(orientation='horizontal', size_hint_y=None,
                    height=theme.ds('pill_h'), spacing=dp(10))
    for btn in buttons:
        bar.add_widget(btn)
    return bar


class HomeScreen(BaseScreen):
    """主页：一个标题 + 三个大入口。横屏一排三个，竖屏一列三行。"""

    def __init__(self, **kwargs):
        super(HomeScreen, self).__init__(**kwargs)
        self._was_landscape = None
        # 手机上方向是锁死的，但分屏、桌面调试时窗口尺寸会变，
        # 这时候横/竖的排法也得跟着换
        Window.bind(size=self._on_window_resize)

    def on_pre_enter(self, *args):
        self.rebuild()

    @staticmethod
    def _is_landscape():
        return Window.width > Window.height

    def _on_window_resize(self, *_):
        now = self._is_landscape()
        if self._was_landscape is None:
            self._was_landscape = now
            return
        if now != self._was_landscape:
            self._was_landscape = now
            Clock.schedule_once(lambda dt: self.rebuild(), 0)

    def rebuild(self):
        self._was_landscape = self._is_landscape()
        self.content.clear_widgets()
        app = self.app()

        refresh_btn = FlatButton('⇄ 刷新', bg=COLORS['green'],
                                 callback=app.action_refresh,
                                 size_hint_x=None, width=dp(104))
        settings_btn = FlatButton('≡ 设置', bg=COLORS['gray'],
                                  callback=app.show_settings,
                                  size_hint_x=None, width=dp(104))
        bar, title = build_topbar('▶ ' + core.APP_NAME,
                                  [refresh_btn, settings_btn])
        self.content.add_widget(bar)

        entries = ((CAT_HUANGMEI, COLORS['cat_huangmei']),
                   (CAT_TV, COLORS['cat_tv']),
                   (CAT_HISTORY, COLORS['cat_history']))

        if self._is_landscape():
            self._build_row(app, entries)
        else:
            self._build_column(app, entries)

        path_tip = Label(
            text='视频文件夹：%s' % app.store.root_path,
            font_name=CJK_FONT, font_size=sp(13),
            color=list(COLORS['text_dim']),
            halign='center', valign='middle',
            size_hint_y=None, height=dp(34),
            shorten=True, shorten_from='center')
        path_tip.bind(size=lambda w, *_: setattr(w, 'text_size', (w.width, w.height)))
        self.content.add_widget(path_tip)

    def _build_column(self, app, entries):
        """竖屏：整行长卡片竖着摆三行，右边跟一句说明。"""
        self.content.add_widget(Widget(size_hint_y=None, height=dp(8)))

        for name, color in entries:
            count = app.count_history() if name == CAT_HISTORY \
                else app.count_for_category(name)

            btn = BigEntryButton(title=name, accent=color)
            btn.icon_texture = self._icon_texture(name)
            btn.size_hint_y = None
            btn.height = theme.ds('cat_h')
            btn.bind(on_release=lambda _b, n=name: app.open_category(n))
            self.content.add_widget(btn)

            tip = Label(text=app.describe_count(name, count),
                        font_name=CJK_FONT, font_size=theme.fs('card'),
                        color=list(COLORS['text_dim']),
                        halign='left', valign='middle',
                        size_hint_y=None, height=dp(30))
            tip.bind(size=lambda w, *_: setattr(w, 'text_size', (w.width, w.height)))
            self.content.add_widget(tip)

        self.content.add_widget(Widget())

    def _build_row(self, app, entries):
        """
        横屏：三个方块并排，占满中间这块。

        横屏的高度只有 300dp 出头，三个 136dp 的整行卡片叠起来根本放不下，
        所以换成「上图下字」的方块 —— 视觉上更像桌面版，一屏点完不用滚动。
        """
        self.content.add_widget(Widget(size_hint_y=None, height=dp(6)))

        row = BoxLayout(orientation='horizontal', spacing=dp(12),
                        size_hint_y=1)
        for name, color in entries:
            count = app.count_history() if name == CAT_HISTORY \
                else app.count_for_category(name)
            tile = BigEntryTile(title=name,
                                subtitle=app.describe_count_short(name, count),
                                accent=color,
                                icon_texture=self._icon_texture(name))
            tile.bind(on_release=lambda _b, n=name: app.open_category(n))
            row.add_widget(tile)
        self.content.add_widget(row)

        self.content.add_widget(Widget(size_hint_y=None, height=dp(4)))

    # ---- 自定义图标 ----
    @staticmethod
    def _icon_key(name):
        for key, category in core.HOME_ICON_KEYS:
            if category == name:
                return key
        return None

    def _icon_texture(self, name):
        key = self._icon_key(name)
        if not key:
            return None
        path = self.app().store.home_icon(key)
        return load_icon_texture(path) if path else None


class SeriesScreen(BaseScreen):
    """某个分类下的剧集列表。"""

    def on_pre_enter(self, *args):
        self.rebuild()

    def rebuild(self):
        self.content.clear_widgets()
        app = self.app()
        category = app.current_category

        bar, title = build_topbar(category or '剧集', [])
        self.content.add_widget(bar)

        series = app.categories.get(category, [])
        if not series:
            self.content.add_widget(self._empty_tip('这个分类下还没有视频。'))
        else:
            grid = CardGrid(tile_size=theme.ds('tile_min'),
                            on_card_tap=app.open_series)
            entries = []
            for path in series:
                # 分类文件夹自己出现时（说明它下面直接堆了视频），换个说法
                label = '未分集的视频' if path.name == category else path.name
                entries.append({
                    'kind': 'series',
                    'title': label,
                    'path': path,
                    'tone': COLORS['cat_huangmei'] if category == CAT_HUANGMEI
                    else COLORS['cat_tv'],
                })
            grid.set_cards(entries)
            self.content.add_widget(grid)
            app.attach_thumbnails(grid)
            Clock.schedule_once(lambda dt: grid._relayout(), 0)

        self.content.add_widget(build_bottombar(
            BackButton('⬅ 返回主界面', callback=app.go_home)))

    @staticmethod
    def _empty_tip(text):
        return Label(text=text, font_name=CJK_FONT, font_size=theme.fs('card'),
                     color=list(COLORS['text_dim']),
                     halign='center', valign='middle')


class VideoScreen(BaseScreen):
    """某个剧集下的视频列表。"""

    def on_pre_enter(self, *args):
        self.rebuild()

    def rebuild(self):
        self.content.clear_widgets()
        app = self.app()

        title = app.current_series.name if app.current_series else '视频'
        bar, _ = build_topbar(title, [])
        self.content.add_widget(bar)

        # current_videos 在 open_series 里已经过滤过了，这里直接用
        videos = list(app.current_videos)
        if not videos:
            if app.current_series_videos_all:
                self.content.add_widget(
                    self._unsupported_tip(app.current_series_videos_all))
            else:
                self.content.add_widget(self._plain_tip('这个剧集文件夹里没有视频。'))
        else:
            grid = CardGrid(tile_size=theme.ds('tile_min'),
                            on_card_tap=app.open_video)
            entries = []
            for idx, path in enumerate(videos):
                entries.append({
                    'kind': 'video',
                    'title': path.name,
                    'path': path,
                    'index': idx,
                    'tone': COLORS['cat_tv'],
                })
            grid.set_cards(entries)
            self.content.add_widget(grid)
            app.attach_thumbnails(grid)
            Clock.schedule_once(lambda dt: grid._relayout(), 0)

        self.content.add_widget(build_bottombar(
            BackButton('⬅ 返回剧集', callback=app.go_series)))

    @staticmethod
    def _plain_tip(text):
        return Label(text=text, font_name=CJK_FONT, font_size=theme.fs('card'),
                     color=list(COLORS['text_dim']),
                     halign='center', valign='middle')

    @staticmethod
    def _unsupported_tip(all_videos):
        exts = sorted(set(p.suffix.lower() for p in all_videos))
        text = ('这里的视频格式手机放不了：%s\n\n'
                '安卓只认 MP4（H.264 编码）这类常见格式。\n'
                '需要先在电脑上把视频转成 MP4 再拷进来。\n\n'
                '如果确实想试试，可以在「设置」里打开'
                '「显示可能无法播放的格式」。') % '、'.join(exts)
        return Label(text=text, font_name=CJK_FONT, font_size=theme.fs('base'),
                     color=list(COLORS['text']),
                     halign='center', valign='middle')


class HistoryScreen(BaseScreen):
    """历史播放。"""

    def on_pre_enter(self, *args):
        self.rebuild()

    def rebuild(self):
        self.content.clear_widgets()
        app = self.app()

        bar, _ = build_topbar('◎ 历史播放', [])
        self.content.add_widget(bar)

        records = app.store.valid_records()
        if not records:
            tip = ('还没有历史记录。\n\n'
                   '一个视频连续看满 3 分钟之后，\n'
                   '下次就能从这里接着看。')
            self.content.add_widget(Label(
                text=tip, font_name=CJK_FONT, font_size=theme.fs('base'),
                color=list(COLORS['text_dim']),
                halign='center', valign='middle'))
        else:
            grid = CardGrid(tile_size=theme.ds('tile_min'),
                            on_card_tap=app.resume_record)
            entries = []
            for rec, series_path, video_path, videos in records:
                entries.append({
                    'kind': 'record',
                    'title': '%s\n%s' % (series_path.name, video_path.stem),
                    'path': video_path,
                    'record': rec,
                    'series_path': series_path,
                    'video_files': videos,
                    'tone': COLORS['cat_history'],
                })
            grid.set_cards(entries)
            self.content.add_widget(grid)
            app.attach_thumbnails(grid)
            Clock.schedule_once(lambda dt: grid._relayout(), 0)

        self.content.add_widget(build_bottombar(
            BackButton('⬅ 返回主界面', callback=app.go_home)))


class PlayerScreen(BaseScreen):
    """播放界面。"""

    def __init__(self, **kwargs):
        super(PlayerScreen, self).__init__(**kwargs)
        self.content.padding = [0, 0, 0, 0]
        self.content.spacing = 0
        self.video = None            # 当前用的播放器（VlcVideo 或 Kivy 的 Video）
        self._tick = None
        self._prepared = False
        self._eos_fired = False
        self._pending_seek = None
        self._was_playing = False
        self._last_save_pos = 0
        self._manual_stop = False
        self._drag_value = None
        self._attach_check = None    # 用来确认 libVLC 的画面有没有真的挂上去
        self._build()

    # ---- 界面 ----
    def _build(self):
        self.content.clear_widgets()

        # 顶部：正在播的是哪一集
        self.now_playing = Label(
            text='', font_name=CJK_FONT, font_size=theme.fs('base'),
            bold=True, color=list(COLORS['text_title']),
            halign='center', valign='middle',
            size_hint_y=None, height=dp(38),
            shorten=True, shorten_from='center',
            padding=[dp(10), 0])
        self.now_playing.bind(
            size=lambda w, *_: setattr(w, 'text_size', (w.width, w.height)))
        self.content.add_widget(self.now_playing)

        # 视频区。里面装什么由 _setup_backend 决定：
        # 可能是给 libVLC 用的占位控件，也可能是 Kivy 自带的 Video 控件。
        self.video_box = BoxLayout()
        self.content.add_widget(self.video_box)

        panel = Card(orientation='vertical', radius=dp(22),
                     padding=[dp(14), dp(8), dp(14), dp(10)],
                     spacing=dp(4), size_hint_y=None, shadow=False)
        panel.bg_color = [1, 1, 1, 0.92]
        # 只剩「进度」和「按钮」两行，高度跟着按钮档位走
        panel.height = dp(8) + dp(46) + dp(4) + theme.ds('pill_h') + dp(10)
        self.panel = panel

        # 时间 + 进度
        progress_row = BoxLayout(orientation='horizontal', size_hint_y=None,
                                 height=dp(46), spacing=dp(10))
        self.time_label = Label(text='00:00 / 00:00', font_name=CJK_FONT,
                                font_size=theme.fs('base'), bold=True,
                                color=list(COLORS['text']),
                                size_hint_x=None, width=dp(150),
                                halign='center', valign='middle')
        self.time_label.bind(
            size=lambda w, *_: setattr(w, 'text_size', (w.width, w.height)))
        self.seek = SeekBar(on_seek=self._on_seek)
        progress_row.add_widget(self.time_label)
        progress_row.add_widget(self.seek)
        panel.add_widget(progress_row)

        # 控制按钮：返回 / 播放 --（空开一段）-- 音量组
        #
        # 快进快退 30 秒已经按需求去掉了：老人用不上，还占地方。
        # 音量从「一根滑条」改成「减 / 喇叭 / 加」，按钮比滑条好按得多。
        #
        # 返回、播放是一组（都在左边，拇指够得到），音量是另一组，
        # 中间特意留一段空当，两组不会挤成一条糊在一起。
        controls = BoxLayout(orientation='horizontal', size_hint_y=None,
                             height=theme.ds('pill_h'), spacing=dp(10))
        self.btn_back = BackButton('⬅ 返回', callback=self.app_go_back)
        self.btn_back.size_hint_x = 0.26
        self.btn_play = FlatButton('‖ 暂停', bg=COLORS['play_pause'],
                                   fg=COLORS['text'], callback=self.toggle_play)
        self.btn_play.size_hint_x = 0.26

        # 这段空当是固定的，不参与 size_hint 分配
        gap = Widget(size_hint=(None, 1), width=dp(34))

        vol_box = BoxLayout(orientation='horizontal', spacing=dp(8),
                            size_hint_x=0.48)
        self.btn_vol_down = FlatButton('－', bg=COLORS['gray'],
                                       callback=lambda: self.change_volume(-1))
        self.vol_icon = SpeakerIcon(color=list(COLORS['text']),
                                    width=dp(56),
                                    height=theme.ds('pill_h'))
        self.btn_vol_up = FlatButton('＋', bg=COLORS['gray'],
                                     callback=lambda: self.change_volume(1))
        # 顺序照用户说的：音量减 - 喇叭 - 音量加
        vol_box.add_widget(self.btn_vol_down)
        vol_box.add_widget(self.vol_icon)
        vol_box.add_widget(self.btn_vol_up)

        controls.add_widget(self.btn_back)
        controls.add_widget(self.btn_play)
        controls.add_widget(gap)
        controls.add_widget(vol_box)
        panel.add_widget(controls)

        self.content.add_widget(panel)

    # ---- 播放内核的选择与降级 ----
    def _setup_backend(self):
        """
        决定这次用哪个播放内核，并把视频区布置好。

        优先用 libVLC（能放 RMVB / WMV / AVI / FLV 这些老格式）；
        只要它有一点不对，就退回 Kivy 自带的系统播放器。
        """
        app = self.app()
        self._release_video()
        self.video_box.clear_widgets()

        if app.use_vlc_backend():
            holder = Widget()          # libVLC 的画面会盖在这块区域上
            self.video_box.add_widget(holder)
            try:
                self.video = VlcVideo(holder=holder)
                return
            except Exception as exc:
                app.note_vlc_failed(str(exc))
                self.video_box.clear_widgets()

        video = Video(state='stop')
        video.allow_stretch = True
        video.keep_ratio = True
        try:
            video.fit_mode = 'contain'
        except Exception:
            pass
        self.video_box.add_widget(video)
        self.video = video

    def _release_video(self):
        """把上一个播放器停掉、放掉。"""
        old = self.video
        self.video = None
        if old is None:
            return
        if isinstance(old, VlcVideo):
            try:
                old.release()
            except Exception:
                pass
        else:
            try:
                old.state = 'stop'
            except Exception:
                pass

    def _verify_backend(self, _dt=None):
        """
        确认 libVLC 的画面真的挂到屏幕上了。

        挂 SurfaceView 是在安卓 UI 线程异步做的，万一失败（拿不到容器视图、
        设备不兼容之类），用户看到的就是一块黑。这里过一会儿检查一下，
        没挂上就换成系统播放器重来，别让老人对着黑屏干等。
        """
        self._attach_check = None
        if not isinstance(self.video, VlcVideo):
            return
        if self.video.is_attached():
            return
        self.app().note_vlc_failed('画面没能显示出来')
        self._switch_backend_keeping_position()

    def _switch_backend_keeping_position(self):
        """换内核，但尽量接着刚才的位置往下播。"""
        app = self.app()
        if (not app.current_videos or app.current_index < 0
                or app.current_index >= len(app.current_videos)):
            return
        position = 0
        try:
            if self.video is not None:
                position = int(float(self.video.position or 0))
        except Exception:
            position = 0
        try:
            if self.video is not None:
                self.video.state = 'stop'
        except Exception:
            pass
        self._setup_backend()
        self._start_file(app.current_videos[app.current_index], position)
        app.toast('这个视频改用系统播放器打开', 2.6)

    # ---- 生命周期 ----
    def on_pre_enter(self, *args):
        app = self.app()
        # 兜底：万一没选视频就进了播放界面（历史记录失效之类），
        # 直接回主页，别在这里抛 IndexError 把整个程序带崩
        if (not app.current_videos
                or app.current_index < 0
                or app.current_index >= len(app.current_videos)):
            Clock.schedule_once(lambda dt: self._bail_no_video(), 0)
            return
        app.set_playback_ui(True)
        AndroidBridge.keep_screen_on(True)
        # 跟着「设置 → 打开时的屏幕方向」走，不再强行横屏
        AndroidBridge.apply_default_orientation()
        self._eos_fired = False
        self._prepared = False
        self._manual_stop = False
        self._was_playing = False
        self._last_save_pos = 0

        # 每次进播放页都重新决定用哪个内核：设置可能被改过，
        # libVLC 也可能在上一次会话里失败过
        self._setup_backend()
        if isinstance(self.video, VlcVideo):
            self.video.attach_view()
            # 稍后确认画面有没有真的挂上屏幕，没挂上就自动换回系统播放器
            self._attach_check = Clock.schedule_once(self._verify_backend, 2.0)

        self._start_file(app.current_videos[app.current_index],
                         app.pending_start_pos)

    def _bail_no_video(self):
        app = self.app()
        app.toast('没有选到视频，回到主页。')
        app.go_home()

    def on_leave(self, *args):
        if self._attach_check is not None:
            self._attach_check.cancel()
            self._attach_check = None
        self.save_progress(force=True)
        self._stop_ticker()
        video = self.video
        if video is not None:
            try:
                video.state = 'stop'
            except Exception:
                pass
            # libVLC 的画面是挂在安卓视图上的，离开时必须摘掉，
            # 不然它会一直盖在别的界面上
            if isinstance(video, VlcVideo):
                try:
                    video.detach_view()
                except Exception:
                    pass
        AndroidBridge.keep_screen_on(False)
        # 别硬切成竖屏：默认就是横屏，切一下等于让整个界面原地转一圈
        AndroidBridge.apply_default_orientation()
        self.app().set_playback_ui(False)

    def _stop_ticker(self):
        if self._tick is not None:
            self._tick.cancel()
            self._tick = None

    # ---- 播放 ----
    def _start_file(self, path, start_pos=0):
        self._prepared = False
        self._eos_fired = False
        self._pending_seek = start_pos if start_pos and start_pos > 0 else None
        self.seek.value = 0
        self.time_label.text = '00:00 / 00:00'
        self.btn_play.text = '‖ 暂停'
        self.now_playing.text = '%s  ·  第 %d / %d 集' % (
            Path(path).stem,
            self.app().current_index + 1,
            max(1, len(self.app().current_videos)))
        try:
            self.video.source = str(path)
            self.video.state = 'play'
            self._was_playing = True
        except Exception as exc:
            self._show_error(str(exc))
        self._stop_ticker()
        self._tick = Clock.schedule_interval(self._on_tick, 0.4)

    def _on_tick(self, _dt):
        app = self.app()
        try:
            duration = float(self.video.duration or 0)
            position = float(self.video.position or 0)
            state = self.video.state
        except Exception:
            return

        # 等播放器把文件准备好，再去跳进度（Kivy 没有 prepared 回调，只能轮询）
        if not self._prepared:
            if duration > 0:
                self._prepared = True
                if self._pending_seek:
                    self._do_seek_ms(self._pending_seek)
                    self._pending_seek = None
            else:
                return

        if duration > 0:
            if self._drag_value is None:
                self.seek.value = max(0.0, min(1.0, position / duration))
            self.time_label.text = '%s / %s' % (format_time(position),
                                                format_time(duration))
        else:
            self.time_label.text = '%s / --:--' % format_time(position)

        if state == 'play':
            self._was_playing = True
            if self.btn_play.text != '‖ 暂停':
                self.btn_play.text = '‖ 暂停'
        elif self.btn_play.text != '▶ 播放' and not self._manual_stop:
            self.btn_play.text = '▶ 播放'

        # 自动保存进度（原版是每次位置变化都存，手机上太耗电，改成每 5 秒）
        if position - self._last_save_pos > 5000:
            self._last_save_pos = position
            self.save_progress(position=position)

        # 播完自动下一集
        if (not self._eos_fired and duration > 0 and position > 0
                and position >= duration - 1500):
            self._eos_fired = True
            Clock.schedule_once(lambda dt: self._on_finished(), 0.3)

    def _on_finished(self):
        app = self.app()
        next_index = app.current_index + 1
        if next_index < len(app.current_videos):
            app.current_index = next_index
            self._start_file(app.current_videos[next_index], 0)
        else:
            self._manual_stop = True
            try:
                self.video.state = 'stop'
            except Exception:
                pass
            self.btn_play.text = '▶ 播放'
            app.toast('这一集全部播放完了')

    def _do_seek_ms(self, ms):
        try:
            duration = float(self.video.duration or 0)
        except Exception:
            duration = 0
        if duration <= 0:
            return
        ratio = max(0.0, min(1.0, ms / duration))
        try:
            self.video.seek(ratio)
        except Exception:
            pass

    def _on_seek(self, ratio, final=False):
        if final:
            self._drag_value = None
            try:
                self.video.seek(ratio)
            except Exception:
                pass
            # 手动拖到最后会被当成播完，这里把标志收回来
            self._eos_fired = False
        else:
            self._drag_value = ratio
            self.seek.value = ratio
            try:
                duration = float(self.video.duration or 0)
                self.time_label.text = '%s / %s' % (
                    format_time(ratio * duration), format_time(duration))
            except Exception:
                pass

    def toggle_play(self):
        app = self.app()
        try:
            if self.video.state == 'play':
                self.video.state = 'pause'
                self.btn_play.text = '▶ 播放'
            else:
                self._manual_stop = False
                self.video.state = 'play'
                self.btn_play.text = '‖ 暂停'
        except Exception as exc:
            self._show_error(str(exc))

    def app_go_back(self):
        self.app().go_back_from_play()

    # ---- 音量 ----
    def change_volume(self, direction):
        """
        音量加一格 / 减一格。

        优先调**系统媒体音量** —— 老人不用再去找手机侧键，按这里就行。
        桌面调试时没有这套接口，退化成调播放器自己的音量。
        """
        lo, hi = AndroidBridge.volume_range()
        if hi > lo:
            target = max(lo, min(hi, AndroidBridge.volume_current()
                                   + int(direction)))
            if AndroidBridge.set_volume(target):
                self._sync_volume_icon(0 if target <= lo else 1)
                self.app().toast('音量 %d / %d' % (target, hi), 1.2)
                return
        try:
            now = float(self.video.volume or 0.0)
        except Exception:
            now = 0.0
        try:
            self.video.volume = max(0.0, min(1.0, now + 0.1 * direction))
            self._sync_volume_icon(self.video.volume)
            self.app().toast('音量 %d%%' % int(round(self.video.volume * 100)),
                             1.2)
        except Exception:
            pass

    def _sync_volume_icon(self, level):
        """音量为 0 的时候把喇叭变灰，一眼能看出「现在是静音」。"""
        try:
            self.vol_icon.muted = float(level or 0) <= 0.0
        except Exception:
            pass

    # ---- 进度保存 ----
    def save_progress(self, position=None, force=False):
        app = self.app()
        try:
            if position is None:
                position = float(self.video.position or 0)
        except Exception:
            return
        if position < MIN_RESUME_DURATION:
            return
        if not app.current_series:
            return
        app.store.upsert_record(app.current_series,
                                app.current_index,
                                int(position))

    def _show_error(self, message):
        self.app().toast('播放出错：%s' % message)

    def _on_back(self):
        self.save_progress(force=True)
        return False


class SettingsScreen(BaseScreen):
    """
    设置：字号、按钮大小、缩略图、主文件夹、权限。

    排版上的两个要点（都是排版体检逼出来的）：
      1. 标题必须独占一行。原来标题和 -/+ 按钮挤在一行，
         标题只剩 111px，而「显示可能无法播放的格式」要 363px，直接被裁掉。
      2. 说明文字必须设 text_size 让它可以换行，并且按估算的行数留够高度。
         不设 text_size 的 Label 不会被布局约束，多出来的字是硬裁掉的。
    """

    def on_pre_enter(self, *args):
        self.rebuild()

    # ---------------------------------------------------------- 尺寸计算
    @staticmethod
    def _hint_font():
        """说明文字的字号：基准字号的 0.62 倍，跟着档位一起放大。"""
        return theme.fs('base') * 0.62

    def _title_h(self):
        return theme.fs('card') * 1.5

    def _hint_h(self, lines=2):
        return self._hint_font() * 1.5 * max(1, int(lines))

    def _row_height(self, hint_lines=2):
        # padding 上下 + 标题 + 控制行 + 说明，中间两处 spacing
        return (dp(12) * 2 + self._title_h() + theme.ds('pill_h')
                + dp(8) * 2 + self._hint_h(hint_lines))

    @staticmethod
    def _avail_dp():
        """卡片里一行大概有多少逻辑像素宽。"""
        try:
            density = dp(1) or 1.0
            window_w = Window.width if Window is not None else 400 * density
            return max(120.0, window_w / density - 56)
        except Exception:
            return 300.0

    def _wrapped_lines(self, text, font_size, extra_width_dp=0):
        """粗略估算这段文字换行之后占几行。"""
        per_line = max(6, int((self._avail_dp() - extra_width_dp)
                              / max(1.0, font_size)))
        total = 0
        for part in str(text).split('\n'):
            total += max(1, int(math.ceil(len(part) / float(per_line))))
        return total

    def _text_label(self, text, font_size, color, bold=False, height=None,
                    halign='left'):
        """会按给定高度自动换行的文字标签。"""
        if height is None:
            height = font_size * 1.5
        label = Label(text=text, font_name=CJK_FONT, font_size=font_size,
                      bold=bold, color=list(color),
                      halign=halign, valign='middle',
                      size_hint_y=None, height=height)
        label.bind(size=lambda w, *_: setattr(w, 'text_size', (w.width, w.height)))
        return label

    def _hint_label(self, text):
        lines = self._wrapped_lines(text, self._hint_font())
        return self._text_label(text, self._hint_font(), COLORS['text_dim'],
                                height=self._hint_h(lines))

    # ---------------------------------------------------------- 构建
    def rebuild(self):
        self.content.clear_widgets()
        app = self.app()

        bar, _ = build_topbar('≡ 设置', [])
        self.content.add_widget(bar)

        lst = SimpleList()
        self.content.add_widget(lst)

        lst.add(self._stepper_row(
            '文字大小', app.theme.font_level, app.bump_font,
            '现在看着正好就保持，觉得小就点「变大」。'))
        lst.add(self._stepper_row(
            '按钮大小', app.theme.btn_level, app.bump_button,
            '按钮越大越好按，手抖也能点中。'))
        lst.add(self._orientation_row())
        lst.add(self._toggle_row(
            '显示视频缩略图', bool(app.store.get_setting('thumbnails', True)),
            app.toggle_thumbnails,
            '关掉的话列表里只显示彩色方块，翻页会更快。'))
        if app.use_vlc_backend():
            unplayable_hint = ('现在用的是增强播放器，这些老格式也能放了，'
                               '这一项基本用不上。')
        else:
            unplayable_hint = ('RMVB / FLV / WMV / AVI 这些老格式系统播放器'
                               '放不了，默认藏起来不显示。')
        lst.add(self._toggle_row(
            '显示可能无法播放的格式',
            bool(app.store.get_setting('show_unplayable', False)),
            app.toggle_unplayable,
            unplayable_hint))

        status_text, _active = app.vlc_status()
        lst.add(self._toggle_row(
            '增强播放器（支持更多格式）',
            bool(app.store.get_setting('use_vlc', True)),
            app.toggle_enhanced_player,
            status_text))

        lst.add(self._icons_card())
        lst.add(self._folder_card())
        if IS_ANDROID:
            lst.add(self._permission_card())
        lst.add(self._about_card())

        # 返回键放最下面
        self.content.add_widget(build_bottombar(
            BackButton('⬅ 返回主界面', callback=app.go_home)))

    # ---------------------------------------------------------- 各种卡片
    def _stepper_row(self, title, level, on_change, hint=''):
        row = Card(orientation='vertical', size_hint_y=None,
                   padding=[dp(14), dp(12), dp(14), dp(12)], spacing=dp(8),
                   height=self._row_height())
        row.add_widget(self._text_label(title, theme.fs('card'),
                                        COLORS['text'], bold=True,
                                        height=self._title_h()))

        ctrl = BoxLayout(orientation='horizontal', size_hint_y=None,
                         height=theme.ds('pill_h'), spacing=dp(8))
        ctrl.add_widget(FlatButton('变小 －', bg=COLORS['gray'],
                                   callback=lambda: on_change(-1)))
        middle = Card(orientation='vertical', shadow=False,
                      bg_color=list(COLORS['card_press']),
                      padding=[dp(4), 0, dp(4), 0])
        middle.add_widget(Label(text='%d / 5' % level, font_name=CJK_FONT,
                                font_size=theme.fs('pill'), bold=True,
                                color=list(COLORS['blue_dark']),
                                halign='center', valign='middle'))
        ctrl.add_widget(middle)
        ctrl.add_widget(FlatButton('变大 ＋', bg=COLORS['blue'],
                                   callback=lambda: on_change(1)))
        row.add_widget(ctrl)

        row.add_widget(self._hint_label(hint))
        return row

    def _toggle_row(self, title, value, on_toggle, hint=''):
        row = Card(orientation='vertical', size_hint_y=None,
                   padding=[dp(14), dp(12), dp(14), dp(12)], spacing=dp(8),
                   height=self._row_height())
        row.add_widget(self._text_label(title, theme.fs('card'),
                                        COLORS['text'], bold=True,
                                        height=self._title_h()))
        row.add_widget(FlatButton(
            '已打开 ✓' if value else '已关闭 ×',
            bg=COLORS['green'] if value else COLORS['gray'],
            callback=lambda: on_toggle(not value)))
        row.add_widget(self._hint_label(hint))
        return row

    def _orientation_row(self):
        app = self.app()
        landscape = app.store.default_landscape()

        row = Card(orientation='vertical', size_hint_y=None,
                   padding=[dp(14), dp(12), dp(14), dp(12)], spacing=dp(8),
                   height=self._row_height())
        row.add_widget(self._text_label('打开时的屏幕方向', theme.fs('card'),
                                        COLORS['text'], bold=True,
                                        height=self._title_h()))

        ctrl = BoxLayout(orientation='horizontal', size_hint_y=None,
                         height=theme.ds('pill_h'), spacing=dp(8))
        ctrl.add_widget(FlatButton(
            '横屏', bg=COLORS['green'] if landscape else COLORS['gray'],
            callback=lambda: app.set_default_orientation(True)))
        ctrl.add_widget(FlatButton(
            '竖屏', bg=COLORS['gray'] if landscape else COLORS['green'],
            callback=lambda: app.set_default_orientation(False)))
        row.add_widget(ctrl)

        row.add_widget(self._hint_label(
            '绿色的那个是当前选择，点一下马上就转过去。'))
        return row

    def _icon_row(self, key, name, color):
        app = self.app()
        side = theme.ds('pill_h')

        row = BoxLayout(orientation='horizontal', spacing=dp(8),
                        size_hint_y=None, height=side)

        preview = ThumbArea(fallback_text=name[:1],
                            fallback_color=list(color),
                            size_hint=(None, 1), width=side)
        icon = app.store.home_icon(key)
        if icon is not None:
            tex = load_icon_texture(icon)
            if tex is not None:
                preview.texture = tex
        row.add_widget(preview)

        row.add_widget(self._text_label(name, theme.fs('base'),
                                        COLORS['text'], bold=True,
                                        height=side))

        row.add_widget(FlatButton('上传', bg=COLORS['blue'],
                                  size_hint_x=None, width=dp(88),
                                  callback=lambda: app.pick_home_icon(key, name)))
        row.add_widget(FlatButton('默认', bg=COLORS['gray'],
                                  size_hint_x=None, width=dp(88),
                                  callback=lambda: app.reset_home_icon(key)))
        return row

    def _icons_card(self):
        colors = {'xiqu': COLORS['cat_huangmei'],
                  'tv': COLORS['cat_tv'],
                  'history': COLORS['cat_history']}
        hint = ('可以把图标换成老人熟悉的照片，比如戏台、全家福。'
                '图片会先缩到 512 像素再存，不占地方。')

        card = Card(orientation='vertical', size_hint_y=None,
                    padding=[dp(14), dp(12), dp(14), dp(12)], spacing=dp(8))
        card.add_widget(SectionLabel('❖ 主页图标'))
        for key, name in core.HOME_ICON_KEYS:
            card.add_widget(self._icon_row(key, name, colors.get(
                key, COLORS['blue'])))
        card.add_widget(self._hint_label(hint))

        # 高度显式算：BoxLayout 的 minimum_height 是布局时才更新的，
        # 在这里读永远是 0，靠不住
        rows = len(core.HOME_ICON_KEYS)
        card.height = (dp(12) * 2 + dp(34) + theme.ds('pill_h') * rows
                       + self._hint_h(self._wrapped_lines(hint,
                                                          self._hint_font()))
                       + dp(8) * (rows + 1))
        return card

    def _folder_card(self):
        app = self.app()
        info = scan_root_report(app.store.root_path)
        path_lines = self._wrapped_lines(str(info['path']),
                                         theme.fs('base') * 0.85)
        status_lines = self._wrapped_lines(info['message'] or ' ',
                                           theme.fs('base') * 0.75)
        height = (dp(12) * 2 + self._title_h()
                  + theme.fs('base') * 0.85 * 1.5 * path_lines
                  + theme.fs('base') * 0.75 * 1.5 * status_lines
                  + theme.ds('pill_h') + dp(8) * 4)

        card = Card(orientation='vertical', size_hint_y=None,
                    padding=[dp(14), dp(12), dp(14), dp(12)], spacing=dp(8),
                    height=height)
        card.add_widget(SectionLabel('▣ 视频主文件夹'))
        card.add_widget(self._text_label(
            str(info['path']), theme.fs('base') * 0.85, COLORS['text'],
            height=theme.fs('base') * 0.85 * 1.5 * path_lines))
        card.add_widget(self._text_label(
            info['message'] or '', theme.fs('base') * 0.75,
            COLORS['green_dark'] if info['category_count'] else COLORS['warn'],
            height=theme.fs('base') * 0.75 * 1.5 * status_lines))

        btn_row = BoxLayout(orientation='horizontal', size_hint_y=None,
                            height=theme.ds('pill_h'), spacing=dp(10))
        btn_row.add_widget(FlatButton('▣ 选文件夹', bg=COLORS['blue'],
                                      callback=app.show_folder_picker))
        btn_row.add_widget(FlatButton('⇄ 重新扫描', bg=COLORS['green'],
                                      callback=app.action_refresh))
        card.add_widget(btn_row)
        return card

    def _permission_card(self):
        app = self.app()
        granted = AndroidBridge.has_all_files_access()
        if granted:
            text = ('已获得「所有文件访问权限」，一切正常。\n'
                    '应用可以自己读取视频、创建文件夹。')
        else:
            text = ('还没获得「所有文件访问权限」。\n'
                    '没有它，应用没法自己建文件夹，\n'
                    '也读不到 RMVB 这类没有被系统媒体库收录的文件。\n'
                    '点下面的按钮，在系统页面把开关打开，再回来。')
        text_lines = self._wrapped_lines(text, self._hint_font())
        height = (dp(12) * 2 + self._title_h() + self._hint_h(text_lines)
                  + theme.ds('pill_h') + dp(8) * 3)

        card = Card(orientation='vertical', size_hint_y=None,
                    padding=[dp(14), dp(12), dp(14), dp(12)], spacing=dp(8),
                    height=height)
        card.add_widget(SectionLabel('❖ 存储权限'))
        card.add_widget(self._text_label(text, self._hint_font(),
                                         COLORS['text'],
                                         height=self._hint_h(text_lines)))
        card.add_widget(FlatButton(
            '✓ 已完成，重新扫描' if granted else '去授权',
            bg=COLORS['green'] if granted else COLORS['cat_history'],
            callback=app.request_all_files_access))
        return card

    def _about_card(self):
        text = (
            '文件夹结构示例：\n'
            + folder_tree_text() + '\n'
            '\n'
            '• 主文件夹下的一级子文件夹名，对应主页的按钮\n'
            '• 再下一层是剧集文件夹（如「女驸马」），里面放视频文件\n'
            '• 点剧集进入视频列表，再点视频开始播放\n'
            '• 「历史播放」会显示播放超过 3 分钟的视频记录\n'
            '\n'
            '连续看满 3 分钟会自动记住进度，下次接着看。')

        font_size = theme.fs('base') * 0.72
        lines = self._wrapped_lines(text, font_size)
        height = dp(12) * 2 + dp(34) + font_size * 1.55 * lines + dp(8) * 2

        card = Card(orientation='vertical', size_hint_y=None,
                    padding=[dp(14), dp(12), dp(14), dp(12)], spacing=dp(8),
                    height=height)
        card.add_widget(SectionLabel('❖ 关于'))
        card.add_widget(self._text_label(text, font_size,
                                         COLORS['text'],
                                         height=font_size * 1.55 * lines))
        # 同上：高度显式算，别指望 minimum_height
        card.height = (dp(12) * 2 + dp(34) + font_size * 1.55 * lines
                       + dp(8) * 2)
        return card


class HelpScreen(BaseScreen):
    """没有权限 / 文件夹不存在时的引导页。"""

    def on_pre_enter(self, *args):
        self.rebuild()

    def rebuild(self):
        self.content.clear_widgets()
        app = self.app()
        info = scan_root_report(app.store.root_path)

        self.content.add_widget(Label(
            text='还差最后一步', font_name=CJK_FONT,
            font_size=theme.fs('title'), bold=True,
            color=list(COLORS['text_title']),
            size_hint_y=None, height=dp(56),
            halign='center', valign='middle'))

        lines = [
            '这个播放器要读手机里的视频，现在还没读到。',
            '',
            '要用的文件夹是：',
            info['path'],
            '',
            '情况：' + (info['message'] or '未知'),
            '',
            '请按下面的顺序试：',
            '1. 点「去授权」把开关打开，回到这里点「重新扫描」',
            '2. 点「帮我建文件夹」（有了权限才能建）',
            '3. 用手机自带的「文件管理」，把视频拷进这个文件夹',
            '   目录结构长这样：',
            '',
        ]
        lines += ['   ' + row for row in folder_tree_text().split('\n')]
        lines += [
            '',
            '   • 一级子文件夹名对应主页的按钮',
            '   • 再下一层是剧集文件夹，里面放视频文件',
            '',
            '4. 回到这里点「重新扫描」',
        ]
        scroll = ScrollView(do_scroll_x=False)
        body = Label(
            text='\n'.join(lines), font_name=CJK_FONT,
            font_size=theme.fs('base'), color=list(COLORS['text']),
            halign='left', valign='top', size_hint_y=None,
            padding=[dp(8), dp(4)])
        body.bind(width=lambda w, v: setattr(w, 'text_size', (v, None)),
                  texture_size=lambda w, v: setattr(w, 'height', v[1]))
        scroll.add_widget(body)
        self.content.add_widget(scroll)

        buttons = [
            ('去授权', COLORS['cat_history'], app.request_all_files_access),
            ('帮我建文件夹', COLORS['blue'], app.create_root_folder),
            ('重新扫描', COLORS['green'], app.action_refresh),
            ('选别的文件夹', COLORS['gray'], app.show_folder_picker),
        ]
        for text, color, cb in buttons:
            self.content.add_widget(FlatButton(text, bg=color, callback=cb))


# ============================================================================
#  文件夹选择弹窗
# ============================================================================
class FolderPicker(Popup):

    def __init__(self, start_path, on_pick, **kwargs):
        kwargs.setdefault('title', '选择视频主文件夹')
        # Popup 标题默认用 Roboto，字体里没有中文，不改就是一排方框
        kwargs.setdefault('title_font', CJK_FONT)
        kwargs.setdefault('title_size', theme.fs('card'))
        kwargs.setdefault('size_hint', (0.96, 0.92))
        super(FolderPicker, self).__init__(**kwargs)
        self.on_pick = on_pick
        self.current = Path(start_path) if start_path else Path('/')
        self._build()

    def _build(self):
        # Popup 自带的是深灰底，我们的字是深色的，看不清。
        # 里面套一张白卡片，跟 App 其他界面统一。
        root = Card(orientation='vertical', spacing=dp(8), padding=dp(10),
                    radius=dp(16), shadow=False)

        self.path_label = Label(text='', font_name=CJK_FONT,
                                font_size=dp(15), bold=True,
                                color=list(COLORS['text']),
                                size_hint_y=None, height=dp(52),
                                halign='left', valign='middle',
                                shorten=True, shorten_from='center')
        self.path_label.bind(
            size=lambda w, *_: setattr(w, 'text_size', (w.width, w.height)))
        root.add_widget(self.path_label)

        self.list_view = SimpleList()
        root.add_widget(self.list_view)

        btn_row = BoxLayout(orientation='horizontal', size_hint_y=None,
                            height=dp(58), spacing=dp(8))
        btn_row.add_widget(FlatButton('⬆ 上一级', bg=COLORS['gray'],
                                      callback=self.go_up))
        btn_row.add_widget(FlatButton('✓ 就用这个', bg=COLORS['green'],
                                      callback=self.pick_current))
        btn_row.add_widget(FlatButton('取消', bg=COLORS['gray'],
                                      callback=lambda: self.dismiss()))
        root.add_widget(btn_row)

        self.content = root
        self.refresh()

    def refresh(self):
        self.path_label.text = str(self.current)
        self.list_view.clear()

        try:
            entries = sorted([p for p in self.current.iterdir() if p.is_dir()],
                             key=lambda p: natural_key(p.name))
        except PermissionError:
            self.list_view.add(self._tip('没有权限打开这个文件夹。'))
            return
        except OSError as exc:
            self.list_view.add(self._tip('打不开：%s' % exc))
            return

        visible = [p for p in entries if not p.name.startswith('.')]
        if not visible:
            self.list_view.add(self._tip('这里面没有子文件夹。'))
        for path in visible[:200]:
            self.list_view.add(FlatButton(
                '▣ ' + path.name, bg=COLORS['card'],
                fg=COLORS['text'],
                callback=lambda p=path: self.enter(p)))

    def _tip(self, text):
        return Label(text=text, font_name=CJK_FONT, font_size=dp(15),
                     color=list(COLORS['text_dim']),
                     size_hint_y=None, height=dp(50),
                     halign='left', valign='middle')

    def enter(self, path):
        self.current = path
        self.refresh()

    def go_up(self):
        parent = self.current.parent
        if parent != self.current:
            self.current = parent
            self.refresh()

    def pick_current(self):
        try:
            self.on_pick(self.current)
        finally:
            self.dismiss()


# ============================================================================
#  图片选择弹窗（换主页图标用）
# ============================================================================
IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.gif')
MAX_PHOTOS = 80        # 一个文件夹最多列这么多张，太多了翻起来找不着


class PickerScreen(BaseScreen):
    """
    选一张图片当主页图标 —— 整屏的，不是弹窗。

    为什么改成整屏：横屏手机上弹窗只剩 130dp 高，一行缩略图都放不下，
    挑照片得一直滑。整屏能放下「文件夹信息 + 快捷入口 + 一整排缩略图 +
    底部返回」，看得见才选得准。

    三件事是专门为「看不懂英文的老人」做的：
      1. 系统目录显示中文名（DCIM -> 相机相册（DCIM））；
      2. 一排快捷入口（相机相册 / 图片 / 下载 / 手机存储），
         不用一层层翻英文目录名；
      3. 图片是**缩略图网格**，不是一串文件名 —— 看一眼就知道是哪张。

    缩略图是分帧加载的：先把格子摆出来，每帧补两张图，
    几十张照片也不会把界面卡住。
    """

    def __init__(self, **kwargs):
        super(PickerScreen, self).__init__(**kwargs)
        self.current = Path.home()
        self.on_pick = None
        self._pending = []          # 还没加载缩略图的 (卡片, 路径)
        self._thumb_clock = None

    def on_pre_enter(self, *args):
        self.rebuild()

    def show(self, start_path, on_pick):
        """打开这一页：从哪儿开始找、选完回调给谁。"""
        try:
            self.current = Path(start_path) if start_path else Path.home()
        except Exception:
            self.current = Path.home()
        self.on_pick = on_pick
        self.app().sm.current = 'picker'

    # ---- 界面 ----
    def rebuild(self):
        self._stop_thumbs()
        self._pending = []
        self.content.clear_widgets()

        # 第一行：当前文件夹（中文名，大字）—— 兼当标题
        self.where_label = Label(text='', font_name=CJK_FONT,
                                 font_size=theme.fs('title'), bold=True,
                                 color=list(COLORS['text_title']),
                                 size_hint_y=None, height=dp(38),
                                 halign='left', valign='middle',
                                 shorten=True, shorten_from='center')
        self.where_label.bind(
            size=lambda w, *_: setattr(w, 'text_size', (w.width, w.height)))
        self.content.add_widget(self.where_label)

        # 第二行：完整路径（小字，方便跟文件管理器对号）
        self.path_label = Label(text='', font_name=CJK_FONT,
                                font_size=max(dp(11), theme.fs('base') * 0.72),
                                color=list(COLORS['text_dim']),
                                size_hint_y=None, height=dp(22),
                                halign='left', valign='middle',
                                shorten=True, shorten_from='center')
        self.path_label.bind(
            size=lambda w, *_: setattr(w, 'text_size', (w.width, w.height)))
        self.content.add_widget(self.path_label)

        # 快捷入口：老人不用认识那些英文目录名
        quick_row = BoxLayout(orientation='horizontal', size_hint_y=None,
                              height=theme.ds('pill_h'), spacing=dp(8))
        for label, path in core.photo_quick_dirs():
            quick_row.add_widget(FlatButton(
                label, bg=COLORS['blue'],
                callback=lambda p=path: self.enter(p)))
        if not quick_row.children:
            quick_row.height = 0
        self.content.add_widget(quick_row)

        # 缩略图网格：文件夹和图片混在一起，文件夹排前面
        self.grid = CardGrid(tile_size=dp(120), on_card_tap=self._on_tap)
        self.content.add_widget(self.grid)

        # 空文件夹时用它提醒，平时不挂在界面上
        self._tip = Label(text='', font_name=CJK_FONT,
                          font_size=theme.fs('base'),
                          color=list(COLORS['text_dim']),
                          halign='center', valign='middle')
        self._body = self.content

        self.content.add_widget(build_bottombar(
            FlatButton('⬆ 上一级', bg=COLORS['gray'], callback=self.go_up),
            BackButton('← 返回设置', callback=self.app_go_back)))

        self.refresh()

    def app_go_back(self):
        """回设置页 —— 选完图标正好能马上看到效果。"""
        self._stop_thumbs()
        self.on_pick = None
        self.app()._goto('settings')

    # ---- 列目录 ----
    def refresh(self):
        self.path_label.text = str(self.current)
        self.where_label.text = core.folder_label(self.current)
        self._stop_thumbs()
        self._pending = []

        try:
            entries = sorted(list(self.current.iterdir()),
                             key=lambda p: (not p.is_dir(), natural_key(p.name)))
        except PermissionError:
            self._show_tip('没有权限打开这个文件夹，换一个试试。')
            return
        except OSError as exc:
            self._show_tip('打不开：%s' % exc)
            return

        dirs = [p for p in entries
                if p.is_dir() and not p.name.startswith('.')]
        images = [p for p in entries
                  if p.is_file() and p.suffix.lower() in IMAGE_EXT]

        if not dirs and not images:
            self._show_tip('这里没有图片，点上面的蓝色按钮换个地方看看。')
            return

        self._hide_tip()
        cards = []
        for path in dirs[:40]:
            # 认识的系统目录显示中文名，不认识的照原样显示
            cards.append({'kind': 'dir', 'title': core.folder_label(path),
                          'path': path, 'tone': COLORS['card_line']})
        for path in images[:MAX_PHOTOS]:
            cards.append({'kind': 'img', 'title': path.name,
                          'path': path, 'tone': COLORS['cat_tv']})
        self.grid.set_cards(cards)

        # 文件夹格子不显示首字，换成方块符号，一眼区分「这是文件夹」
        #
        # 横屏手机整个窗口只有 340dp 高，去掉标题、路径、快捷按钮和底栏，
        # 留给网格的就 90dp 左右。这时候照片格子再写一行文件名，
        # 缩略图只剩下一条缝 —— 干脆把名字收掉，地方全给图。
        # 判断用 Window.height（整窗高度，布局之前就是准的），
        # 不用 grid.height（这一帧还没排完，读出来是 0）。
        compact = Window.height < dp(380)
        for card, _path in self.grid._cards:
            if getattr(card, '_payload', {}).get('kind') == 'dir':
                card.thumb.fallback_text = '▣'
                card.thumb.fallback_color = list(COLORS['card_line'])
            else:
                if compact:
                    card.hide_title()
                self._pending.append((card, _path))

        if len(images) > MAX_PHOTOS:
            self._show_tip('这个文件夹里还有 %d 张没列出来，'
                           '进到更具体的文件夹里再找。'
                           % (len(images) - MAX_PHOTOS), keep_grid=True)

        if self._pending:
            self._thumb_clock = Clock.schedule_interval(self._load_thumbs, 0)

    def _load_thumbs(self, _dt):
        for _ in range(2):
            if not self._pending:
                self._thumb_clock = None
                return False
            card, path = self._pending.pop(0)
            tex = load_photo_texture(path)
            if tex is not None:
                try:
                    card.texture = tex
                except Exception:
                    pass
        return True

    def _stop_thumbs(self):
        if self._thumb_clock is not None:
            try:
                self._thumb_clock.cancel()
            except Exception:
                pass
            self._thumb_clock = None

    # ---- 空文件夹的提示 ----
    def _show_tip(self, text, keep_grid=False):
        self._tip.text = text
        if not keep_grid and self.grid.parent is not None:
            self._body.remove_widget(self.grid)
        if self._tip.parent is None:
            # BoxLayout 的 children 是倒序的：index=1 就是「底栏上面」
            self._body.add_widget(self._tip, index=1)

    def _hide_tip(self):
        if self._tip.parent is not None:
            self._body.remove_widget(self._tip)
        if self.grid.parent is None:
            self._body.add_widget(self.grid, index=1)

    # ---- 导航 ----
    def _on_tap(self, entry):
        if entry.get('kind') == 'dir':
            self.enter(entry['path'])
        else:
            self.choose(entry['path'])

    def enter(self, path):
        self.current = path
        self.refresh()

    def go_up(self):
        parent = self.current.parent
        if parent != self.current:
            self.current = parent
            self.refresh()

    def choose(self, path):
        callback = self.on_pick
        self.on_pick = None
        self._stop_thumbs()
        self.app()._goto('settings')
        if callback is not None:
            callback(path)


# ============================================================================
#  应用主体
# ============================================================================
class ElderPlayerApp(App):

    title = core.APP_NAME

    @property
    def theme(self):
        """全局尺寸档位。界面都从这里取字号和尺寸。"""
        return th.theme

    def __init__(self, **kwargs):
        super(ElderPlayerApp, self).__init__(**kwargs)
        self.store = None
        self.thumbs = None
        self.categories = {}
        self.current_category = None
        self.current_series = None
        self.current_videos = []
        self.current_series_videos_all = []
        self.current_index = 0
        self.pending_start_pos = 0
        self.previous_screen = 'home'
        self._toast_popup = None
        # 播放内核状态：None = 还没试过，True = libVLC 可用，False = 用系统播放器
        # 记住它是有意义的 —— libVLC 初始化有成本，而且一旦失败就没必要反复试
        self._vlc_ok = None
        self._vlc_note = ''

    # ------------------------------------------------ 播放内核（libVLC）
    def use_vlc_backend(self):
        """
        这次播放要不要用增强内核（libVLC）。

        只要 libVLC 有任何跑不起来的迹象（没打进 APK、初始化失败、
        画面挂不上去），就返回 False，播放界面会自动改用 Kivy 自带的
        系统播放器 —— 应用不会因为 libVLC 出问题就不可用。
        """
        if not IS_ANDROID:
            return False
        if not self.store.get_setting('use_vlc', True):
            return False
        if self._vlc_ok is None:
            self._vlc_ok = bool(VlcVideo.probe())
            if not self._vlc_ok:
                self._vlc_note = VlcVideo.why_not()
        return bool(self._vlc_ok)

    def note_vlc_failed(self, reason):
        """libVLC 中途出问题了（比如画面挂不上去），这次会话不再用它。"""
        self._vlc_ok = False
        self._vlc_note = reason

    def vlc_status(self):
        """给设置页显示用，返回 (说明文字, 现在是不是正在用它)。"""
        if not IS_ANDROID:
            return ('电脑上运行，用的是 Kivy 自带的播放器', False)
        if self._vlc_ok is False:
            return ('增强播放器不可用（%s）' % (self._vlc_note or '原因未知'), False)
        if not self.store.get_setting('use_vlc', True):
            return ('已关闭，现在用系统播放器，老格式放不了', False)
        if self._vlc_ok is None:
            return ('进播放页时会自动启用', True)
        return ('已启用，RMVB / WMV / AVI 等老格式都能放', True)

    # ------------------------------------------------------------ 启动
    def build(self):
        th.register_fonts()
        # 桌面端理论上一定有窗口；安卓端也一定有。
        # 但万一拿不到（比如某些无头环境），后面的代码不该因此崩掉
        if Window is not None:
            Window.clearcolor = COLORS['bg_bottom']

        self.store = Store()
        # 直接赋值并把非法档位纠正回来（此时还不通知界面，界面还没建）
        try:
            font_level = int(self.store.get_setting('font_level',
                                                    th.DEFAULT_FONT_LEVEL))
        except (TypeError, ValueError):
            font_level = th.DEFAULT_FONT_LEVEL
        try:
            btn_level = int(self.store.get_setting('btn_level',
                                                   th.DEFAULT_BTN_LEVEL))
        except (TypeError, ValueError):
            btn_level = th.DEFAULT_BTN_LEVEL
        theme.font_level = th.Theme._clamp(font_level, th.FONT_LEVELS)
        theme.btn_level = th.Theme._clamp(btn_level, th.BTN_LEVELS)

        self.thumbs = ThumbnailLoader(self.store, size=256)
        self.thumbs.set_enabled(bool(self.store.get_setting('thumbnails', True)))

        self.sm = ScreenManager(transition=NoTransition())
        for screen in (HomeScreen(name='home'), SeriesScreen(name='series'),
                       VideoScreen(name='videos'), PlayerScreen(name='player'),
                       HistoryScreen(name='history'),
                       SettingsScreen(name='settings'), HelpScreen(name='help'),
                       PickerScreen(name='picker')):
            self.sm.add_widget(screen)
        self.sm.current = 'home'
        if Window is not None:
            Window.bind(on_keyboard=self._on_key)
        return self.sm

    def on_start(self):
        # manifest 里的 orientation 决定启动那一帧是横的；这里再钉一次，
        # 兜住那些忽略 manifest 的机型（多窗口模式下尤其常见）
        AndroidBridge.apply_default_orientation()
        Clock.schedule_once(self._boot, 0.25)

    def _boot(self, _dt=None):
        if IS_ANDROID:
            AndroidBridge.request_storage_permissions(self._after_permission)
        else:
            self._after_permission(True)

    def _after_permission(self, granted):
        self.scan()
        info = scan_root_report(self.store.root_path)
        # 文件夹不存在、没权限、或者里面一个视频都没有，就把引导页顶上来
        if not info['exists'] or not info['readable'] or not info['category_count']:
            self.previous_screen = 'home'
            self.sm.current = 'help'
        else:
            self.sm.current = 'home'
        self.refresh_current_screen()

    # ------------------------------------------------------------ 数据
    def scan(self):
        try:
            self.store.root_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.categories = scan_categories(self.store.root_path)

    def count_for_category(self, name):
        return len(self.categories.get(name, []))

    def count_history(self):
        try:
            return len(self.store.valid_records())
        except Exception:
            return 0

    def describe_count(self, name, count):
        if name == CAT_HISTORY:
            return '上次看到哪，从这里接着看' if count else '还没有记录'
        if count:
            return '共 %d 个剧集' % count
        return '这个分类下还没有视频'

    def describe_count_short(self, name, count):
        """
        横屏方块下面那行字，得比 describe_count 短一些。

        方块宽度只有 250dp 上下，'上次看到哪，从这里接着看' 这种长句要折三行，
        所以这里统一收成一句话说清数量。
        """
        if not count:
            return '还没有记录' if name == CAT_HISTORY else '还没有视频'
        if name == CAT_HISTORY:
            return '共 %d 条记录' % count
        return '共 %d 个剧集' % count

    def filter_videos(self, videos):
        """
        按设置过滤掉手机放不了的格式。

        注意 can_play 要显式把平台传进去。它自己有一份默认值，
        不传的话就会绕过这里已经判断过的 IS_ANDROID。

        另外，如果 libVLC 内核可用，几乎什么格式都能放，
        这个过滤实际上不会筛掉任何东西 —— 那正是加它的意义。
        """
        if not IS_ANDROID:
            # 电脑上解码器齐全，什么都放得动，不用过滤
            return list(videos)
        if self.store.get_setting('show_unplayable', False):
            return list(videos)
        with_vlc = self.use_vlc_backend()
        return [v for v in videos
                if can_play(v, on_android=True, with_vlc=with_vlc)]

    # ------------------------------------------------------------ 导航
    def _goto(self, name):
        if self.sm.current != name:
            self.previous_screen = self.sm.current
            self.sm.current = name

    def go_home(self):
        self.scan()
        self._goto('home')

    def go_series(self):
        self._goto('series')

    def go_back_from_play(self):
        target = self.previous_screen
        if target not in ('series', 'videos', 'history'):
            target = 'videos'
        self._goto(target)

    def open_category(self, name):
        if name == CAT_HISTORY:
            self._goto('history')
            return
        self.scan()
        if not self.categories.get(name):
            self.toast('「%s」里还没有视频。\n请把视频拷到 %s 下面。'
                       % (name, self.store.root_path))
            return
        self.current_category = name
        self._goto('series')

    def open_series(self, entry):
        path = entry.get('path')
        all_videos = list_videos(path)
        if not all_videos:
            self.toast('这个剧集文件夹里没有视频。')
            return
        self.current_series = path
        self.current_series_videos_all = all_videos
        # 关键：播放列表在进入时就过滤一次。
        # 这样「播完自动下一集」不会走到 Rmvb 这种放不了的文件上，
        # 也就不会播到最后一集突然报错。
        self.current_videos = self.filter_videos(all_videos)
        self.current_index = 0
        self._goto('videos')

    def open_video(self, entry):
        index = int(entry.get('index', 0))
        if index < 0 or index >= len(self.current_videos):
            return
        self.current_index = index
        self.pending_start_pos = 0
        self.previous_screen = 'videos'
        self.sm.current = 'player'

    def resume_record(self, entry):
        rec = entry.get('record') or {}
        series_path = entry.get('series_path')
        all_videos = entry.get('video_files') or []
        if not series_path or not all_videos:
            self.toast('这条记录已经失效了。')
            return

        # 记录里的 video_index 是相对「全部视频」的序号，
        # 过滤之后序号会变，所以要先把目标文件找出来再定位
        try:
            raw_index = int(rec.get('video_index', 0))
        except (TypeError, ValueError):
            raw_index = 0
        raw_index = max(0, min(raw_index, len(all_videos) - 1))
        target = all_videos[raw_index]

        filtered = self.filter_videos(all_videos)
        if target not in filtered:
            # 目标文件正好是被过滤掉的格式，那就回退到完整列表
            filtered = list(all_videos)
        self.current_series = series_path
        self.current_series_videos_all = list(all_videos)
        self.current_videos = filtered
        self.current_index = filtered.index(target)

        position = int(rec.get('position', 0) or 0)
        self.pending_start_pos = max(0, position - RESUME_OFFSET)
        self.previous_screen = 'history'
        self.sm.current = 'player'

    def refresh_current_screen(self):
        screen = self.sm.current_screen
        if hasattr(screen, 'rebuild'):
            screen.rebuild()

    # ------------------------------------------------------------ 缩略图
    def attach_thumbnails(self, grid):
        """给网格里的卡片挂上异步缩略图。"""
        if not self.thumbs or not self.thumbs.enabled:
            return

        def _apply(card, tex):
            try:
                card.texture = tex
            except Exception:
                pass

        for card, path in getattr(grid, '_cards', []):
            try:
                self.thumbs.request(path, lambda tex, c=card: _apply(c, tex))
            except Exception:
                pass

    # ------------------------------------------------------------ 设置动作
    def bump_font(self, delta):
        new_level = max(1, min(5, self.store.get_setting('font_level',
                                                         theme.font_level) + delta))
        theme.set_font_level(new_level)
        self.store.set_setting('font_level', theme.font_level)
        self.thumbs.clear()
        self.refresh_current_screen()

    def bump_button(self, delta):
        new_level = max(1, min(5, self.store.get_setting('btn_level',
                                                         theme.btn_level) + delta))
        theme.set_btn_level(new_level)
        self.store.set_setting('btn_level', theme.btn_level)
        self.refresh_current_screen()

    def toggle_thumbnails(self, value):
        self.store.set_setting('thumbnails', bool(value))
        self.thumbs.set_enabled(bool(value))
        self.refresh_current_screen()

    def _recompute_current_playlist(self):
        """
        过滤规则或播放内核变了之后，当前剧集的播放列表要重算一遍。

        不重算的话，自动连播用的还是旧的列表 —— 比如刚把增强播放器
        打开，下一集还是会被当成「放不了」而跳过。
        """
        if not self.current_series_videos_all:
            return
        current = None
        if 0 <= self.current_index < len(self.current_videos):
            current = self.current_videos[self.current_index]
        self.current_videos = self.filter_videos(self.current_series_videos_all)
        if current is not None and current in self.current_videos:
            self.current_index = self.current_videos.index(current)
        else:
            self.current_index = 0

    def toggle_unplayable(self, value):
        self.store.set_setting('show_unplayable', bool(value))
        self._recompute_current_playlist()
        self.refresh_current_screen()

    def toggle_enhanced_player(self, value):
        """
        开关增强播放器（libVLC）。

        关掉之后只用系统播放器，RMVB / WMV / AVI 这些会被重新藏起来。
        留这个开关是因为我没办法在所有手机上验证 libVLC —— 万一它在
        某台机器上表现不好（卡顿、黑屏），用户能自己切回去，不至于没法看。
        """
        self.store.set_setting('use_vlc', bool(value))
        self._recompute_current_playlist()
        self.refresh_current_screen()

    def show_settings(self):
        self._goto('settings')

    # ---------------------------------------------------------- 屏幕方向
    def set_default_orientation(self, landscape):
        """记住「打开 App 时用哪个方向」，并立刻转过去。"""
        self.store.set_default_landscape(bool(landscape))
        AndroidBridge.apply_default_orientation(bool(landscape))
        self._refresh_home_and_settings()
        self.toast('已设为%s' % ('横屏' if landscape else '竖屏'))

    # ---------------------------------------------------------- 主页图标
    def _image_start_dir(self):
        """
        图片选择器从哪儿开始找。

        顺序：快捷入口里第一个**确实有图片**的目录 -> 相册/图片目录 -> 主文件夹。
        只判断「目录存在」是不够的：Pictures 常常是空的，
        老人一打开看到「这里没有图片」就不知道怎么办了。
        """
        try:
            quick = core.photo_quick_dirs()
        except Exception:
            quick = []
        candidates = [str(p) for _label, p in quick]
        if IS_ANDROID:
            candidates += ['/storage/emulated/0/Pictures',
                           '/storage/emulated/0/DCIM',
                           '/storage/emulated/0/DCIM/Camera',
                           '/storage/emulated/0/Download']
        else:
            candidates += [str(Path.home() / 'Pictures'),
                           str(Path.home() / 'Desktop'),
                           str(Path.home())]
        candidates.append(str(self.store.root_path))

        def has_image(folder):
            try:
                for i, entry in enumerate(folder.iterdir()):
                    if i > 40:
                        break
                    if entry.is_file() and entry.suffix.lower() in IMAGE_EXT:
                        return True
            except Exception:
                return False
            return False

        folder_paths = []
        for c in candidates:
            try:
                p = Path(c)
                if c and p.is_dir():
                    folder_paths.append(p)
            except Exception:
                continue
        for p in folder_paths:
            if has_image(p):
                return str(p)
        if folder_paths:
            return str(folder_paths[0])
        return str(self.store.root_path)

    def pick_home_icon(self, key, name):
        """点「上传」：进选图页，挑完就换上。"""
        screen = self.sm.get_screen('picker')
        screen.show(self._image_start_dir(),
                    lambda path: self.apply_home_icon(key, path, name=name))

    def apply_home_icon(self, key, path, name=''):
        for old in self.store.icon_dir.glob(key + '.*'):
            forget_icon_texture(old)
        saved = self.store.save_home_icon(key, path)
        if not saved:
            self.toast('这张图打不开，换一张试试')
            return
        forget_icon_texture(saved)
        self._refresh_home_and_settings()
        self.toast('%s的图标已换好' % (name or ''))

    def reset_home_icon(self, key, name=''):
        for old in self.store.icon_dir.glob(key + '.*'):
            forget_icon_texture(old)
        if self.store.clear_home_icon(key):
            self._refresh_home_and_settings()
            self.toast('%s的图标已恢复默认' % (name or ''))
        else:
            self.toast('本来就是默认图标')

    def _refresh_home_and_settings(self):
        for screen_name in ('home', 'settings'):
            screen = self.sm.get_screen(screen_name)
            if screen is not None:
                screen.rebuild()

    def action_refresh(self):
        self.scan()
        self.refresh_current_screen()
        self.toast('已重新扫描')

    def create_root_folder(self):
        if self.store.try_create_root():
            self.scan()
            self.toast('文件夹建好了：\n%s' % self.store.root_path)
        else:
            self.toast('建不了。需要先去系统里打开\n'
                       '「所有文件访问权限」，再回来试。')
        self.refresh_current_screen()

    def request_all_files_access(self):
        if not IS_ANDROID:
            self.toast('桌面端不需要这个权限。')
            return
        if AndroidBridge.has_all_files_access():
            self.scan()
            self.toast('权限正常，已经重新扫描。')
            self.refresh_current_screen()
            return
        AndroidBridge.open_all_files_access_settings()
        self.toast('请在打开的页面里打开开关，\n然后回到这里点「重新扫描」。')

    def show_folder_picker(self):
        FolderPicker(self.store.root_path, self._on_folder_picked).open()

    def _on_folder_picked(self, path):
        if path == self.store.root_path:
            return
        self.store.set_root_path(path)
        self.scan()
        self.refresh_current_screen()
        self.toast('主文件夹已改为：\n%s' % path)

    # ------------------------------------------------------------ 播放状态
    def set_playback_ui(self, playing):
        """
        进/出播放界面时的钩子。

        以前这里负责切屏幕方向（菜单竖屏、播放横屏），现在整个 App 都锁横屏了，
        方向不用再切，所以是个空实现。留着这个钩子是因为「播放中」这个状态
        将来可能还要管别的事（比如隐藏状态栏），调用点已经在那儿了。
        """
        pass

    # ------------------------------------------------------------ 提示条
    def toast(self, message, seconds=2.4):
        try:
            if self._toast_popup is not None:
                self._toast_popup.dismiss()
        except Exception:
            pass
        label = Label(text=message, font_name=CJK_FONT,
                      font_size=theme.fs('base'),
                      color=list(COLORS['text']),
                      halign='center', valign='middle', padding=[dp(14), dp(10)])
        label.bind(size=lambda w, *_: setattr(w, 'text_size', (w.width, None)))
        popup = Popup(title='', content=label, size_hint=(0.86, None),
                      height=dp(150), separator_height=0,
                      background_color=[1, 1, 1, 0.96],
                      auto_dismiss=True)
        self._toast_popup = popup
        popup.open()
        Clock.schedule_once(lambda dt: self._dismiss_toast(popup), seconds)

    def _dismiss_toast(self, popup):
        try:
            popup.dismiss()
        except Exception:
            pass
        if self._toast_popup is popup:
            self._toast_popup = None

    # ------------------------------------------------------------ 生命周期
    def _on_key(self, _window, key, _scancode, _codepoint, _modifiers):
        """安卓返回键 / 电脑 ESC。"""
        if key not in (27, 1001):
            return False
        current = self.sm.current
        if current == 'player':
            self.sm.current_screen.save_progress(force=True)
            self.go_back_from_play()
            return True
        if current in ('series', 'videos', 'history', 'settings'):
            if current == 'settings':
                self.refresh_current_screen()
            if current == 'videos':
                self.go_series()
            else:
                self.go_home()
            return True
        if current == 'picker':
            # 选图页按返回 = 放弃选图，回设置
            self.sm.get_screen('picker').app_go_back()
            return True
        if current == 'help':
            self.go_home()
            return True
        return False

    def on_pause(self):
        """切到后台：暂停播放，尽量别被系统回收。"""
        try:
            screen = self.sm.get_screen('player')
            video = screen.video
            if video is not None:
                if video.state == 'play':
                    video.state = 'pause'
                # libVLC 的画面挂在安卓原生视图上。切后台时系统会拆掉
                # SurfaceView，这里先主动摘掉，免得回来变成「有声音没画面」
                if isinstance(video, VlcVideo):
                    video.detach_view()
            screen.save_progress(force=True)
        except Exception:
            pass
        return True

    def on_resume(self):
        """从后台回来：把 libVLC 的画面重新挂上。"""
        try:
            if self.sm.current != 'player':
                return
            video = self.sm.get_screen('player').video
            if isinstance(video, VlcVideo):
                video.attach_view()
        except Exception:
            pass

    def on_stop(self):
        try:
            screen = self.sm.get_screen('player')
            screen.save_progress(force=True)
            # 退出前一定要把画面摘掉、播放器放掉，
            # 不然会留下一个还占着屏幕的原生 SurfaceView
            screen._release_video()
        except Exception:
            pass


def main():
    ElderPlayerApp().run()


if __name__ == '__main__':
    main()
