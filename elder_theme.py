# -*- coding: utf-8 -*-
"""
elder_theme.py —— 界面主题层

职责：
  1. 解决 Kivy 在安卓上不显示中文的问题（内置思源黑体 / Noto Sans SC）
  2. 还原原版的视觉风格：浅蓝渐变背景、白色圆角卡片、大字号、大按钮
  3. 提供全局可调的「界面尺寸档位」，对应原版设置里的那堆字号滑块

原版用 QSS 画界面（border-radius / box-shadow / qlineargradient），
Kivy 没有 CSS，这些效果全部改用 canvas 指令手绘：
  * 渐变  -> 2x2 纹理 + 双线性插值放大（Kivy 里最省事的画法）
  * 圆角  -> RoundedRectangle
  * 阴影  -> 在卡片下方垫一层半透明圆角矩形
"""

import os
from pathlib import Path

from kivy.core.text import LabelBase
from kivy.graphics import (Canvas, Color, Line, Rectangle, RoundedRectangle,
                           StencilPush, StencilPop, StencilUse, StencilUnUse,
                           Triangle)
from kivy.graphics.texture import Texture
from kivy.metrics import dp, sp
from kivy.properties import (BooleanProperty, ListProperty, NumericProperty,
                             ObjectProperty, StringProperty)
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.relativelayout import RelativeLayout
from kivy.uix.widget import Widget
from kivy.utils import platform

IS_ANDROID = (platform == 'android')

# ---------------------------------------------------------------- 配色
# 全部取自原版 QSS，色值一一对应，改主题只要改这里
COLORS = {
    'bg_top':        (0.909, 0.941, 0.996, 1),   # #e8f0fe
    'bg_bottom':     (0.831, 0.878, 0.941, 1),   # #d4e0f0
    'card':          (1.000, 1.000, 1.000, 1),   # #ffffff
    'card_press':    (0.882, 0.929, 1.000, 1),   # #e1edff
    'card_line':     (0.690, 0.769, 0.871, 1),   # #b0c4de
    'text':          (0.173, 0.243, 0.313, 1),   # #2c3e50
    'text_title':    (0.102, 0.227, 0.361, 1),   # #1a3a5c
    'text_dim':      (0.400, 0.463, 0.510, 1),
    'blue':          (0.357, 0.608, 0.835, 1),   # #5b9bd5
    'blue_dark':     (0.165, 0.420, 0.620, 1),   # #2a6b9e
    'green':         (0.180, 0.800, 0.443, 1),   # #2ecc71
    'green_dark':    (0.153, 0.682, 0.376, 1),   # #27ae60
    'gray':          (0.584, 0.647, 0.650, 1),   # #95a5a6
    'gray_dark':     (0.498, 0.549, 0.553, 1),   # #7f8c8d
    'white':         (1, 1, 1, 1),

    # 主页三个分类按钮
    'cat_huangmei':  (0.906, 0.298, 0.235, 1),   # #e74c3c
    'cat_tv':        (0.204, 0.596, 0.859, 1),   # #3498db
    'cat_history':   (0.902, 0.494, 0.133, 1),   # #e67e22

    # 播放界面控制条（原版特意指定过的两个颜色）
    'play_pause':    (0.808, 0.835, 0.573, 1),   # #CED592
    'back_btn':      (0.992, 0.804, 0.376, 1),   # #FDCD60
    'warn':          (0.906, 0.298, 0.235, 1),
}


def rgba(color, alpha):
    """取一个颜色并改透明度。"""
    return (color[0], color[1], color[2], alpha)


def shade(color, factor):
    """简单调亮/调暗：factor > 1 变暗，< 1 变亮（沿用原版 darker/lighter 的思路）。"""
    if factor <= 0:
        factor = 1.0
    return tuple(min(1.0, max(0.0, c * factor)) for c in color[:3]) + (color[3] if len(color) > 3 else 1,)


def lighten(color, amount=0.18):
    return tuple(min(1.0, c + (1.0 - c) * amount) for c in color[:3]) + \
        (color[3] if len(color) > 3 else 1,)


# ---------------------------------------------------------------- 字体
CJK_FONT = 'Roboto'          # 找不到中文字体时的兜底
_FONT_REGISTERED = False

_BUNDLED_REGULAR = 'NotoSansSC-Regular.otf'
_BUNDLED_BOLD = 'NotoSansSC-Bold.otf'

# 实在没带字体时的系统兜底（安卓各家 ROM 的中文字体路径）
_SYSTEM_CJK_FONTS = (
    # 安卓
    '/system/fonts/NotoSansCJK-Regular.ttc',
    '/system/fonts/NotoSansCJKsc-Regular.otf',
    '/system/fonts/NotoSansSC-Regular.otf',
    '/system/fonts/DroidSansFallback.ttf',
    '/system/fonts/DroidSansChinese.ttf',
    '/system/fonts/HarmonyOS_Sans_SC_Regular.ttf',
    '/system/fonts/HarmonyOS_SansSC_Regular.ttf',
    '/system/fonts/MiSans-Regular.ttf',
    '/system/fonts/MiSansLatin-Regular.ttf',
    '/system/fonts/OPPOSans-R.ttf',
    '/system/fonts/OnePlusSans-Regular.ttf',
    '/system/fonts/SourceHanSansCN-Regular.otf',
    # Windows / macOS（本地调试用）
    'C:/Windows/Fonts/msyh.ttc',
    'C:/Windows/Fonts/msyh.ttf',
    'C:/Windows/Fonts/simhei.ttf',
    '/System/Library/Fonts/PingFang.ttc',
)

_SYSTEM_CJK_BOLD = (
    '/system/fonts/NotoSansCJK-Bold.ttc',
    '/system/fonts/NotoSansSC-Bold.otf',
    '/system/fonts/HarmonyOS_Sans_SC_Bold.ttf',
    '/system/fonts/MiSans-Bold.ttf',
    'C:/Windows/Fonts/msyhbd.ttc',
    'C:/Windows/Fonts/simhei.ttf',
)


def _asset_dir():
    return Path(__file__).resolve().parent / 'assets'


def _first_existing(paths):
    for p in paths:
        try:
            if os.path.exists(p):
                return p
        except Exception:
            continue
    return None


def register_fonts():
    """
    注册中文字体。

    关键点：Kivy 自带的 Roboto 字体里一个汉字都没有，不换字体的话
    整个界面会变成一排「豆腐块」。这里直接把 Roboto 这个默认名字
    指向中文字体，这样连 Kivy 内部控件（TextInput 等）也会跟着用上，
    不用每个控件单独指定 font_name。
    """
    global CJK_FONT, _FONT_REGISTERED
    if _FONT_REGISTERED:
        return CJK_FONT

    regular = _first_existing([str(_asset_dir() / _BUNDLED_REGULAR)]
                              + list(_SYSTEM_CJK_FONTS))
    if regular is None:
        # 连系统字体都没找到，只能用 Kivy 自带的 Roboto（中文会显示为方块）
        CJK_FONT = 'Roboto'
        _FONT_REGISTERED = True
        return CJK_FONT

    bundled_bold = _asset_dir() / _BUNDLED_BOLD
    if bundled_bold.exists():
        bold = str(bundled_bold)
    else:
        bold = _first_existing(_SYSTEM_CJK_BOLD) or regular

    try:
        LabelBase.register(name='CJK', fn_regular=regular, fn_bold=bold)
    except Exception:
        pass

    for alias in ('Roboto', 'RobotoBold'):
        try:
            LabelBase.register(name=alias, fn_regular=regular, fn_bold=bold)
        except Exception:
            pass

    CJK_FONT = 'CJK'
    _FONT_REGISTERED = True
    return CJK_FONT


# ---------------------------------------------------------------- 尺寸档位
# 原版设置里有 7 个字号 + 2 个尺寸滑块，手机上没必要那么细。
# 这里收成两档：文字大小、按钮大小，各 5 级，老人自己也能调。
FONT_LEVELS = {
    1: {'base': 15, 'card': 16, 'title': 20, 'pill': 17},
    2: {'base': 18, 'card': 19, 'title': 24, 'pill': 20},
    3: {'base': 21, 'card': 22, 'title': 28, 'pill': 23},   # 默认
    4: {'base': 24, 'card': 26, 'title': 32, 'pill': 27},
    5: {'base': 28, 'card': 30, 'title': 36, 'pill': 31},
}

BTN_LEVELS = {
    1: {'cat_h': 96, 'tile_min': 132, 'pill_h': 48, 'radius': 18},
    2: {'cat_h': 116, 'tile_min': 152, 'pill_h': 54, 'radius': 20},
    3: {'cat_h': 136, 'tile_min': 172, 'pill_h': 60, 'radius': 22},   # 默认
    4: {'cat_h': 160, 'tile_min': 196, 'pill_h': 70, 'radius': 26},
    5: {'cat_h': 186, 'tile_min': 222, 'pill_h': 80, 'radius': 30},
}

DEFAULT_FONT_LEVEL = 3
DEFAULT_BTN_LEVEL = 3


class Theme(object):
    """
    全局尺寸状态。
    界面元素都从这里取值，设置页改完调 bump() 触发界面重排。
    """

    def __init__(self, font_level=DEFAULT_FONT_LEVEL, btn_level=DEFAULT_BTN_LEVEL):
        self.font_level = self._clamp(font_level, FONT_LEVELS)
        self.btn_level = self._clamp(btn_level, BTN_LEVELS)
        self._listeners = []

    @staticmethod
    def _clamp(level, table):
        try:
            level = int(level)
        except (TypeError, ValueError):
            level = 3
        if level not in table:
            level = min(table, key=lambda k: abs(k - level))
        return level

    def set_font_level(self, level):
        level = self._clamp(level, FONT_LEVELS)
        if level != self.font_level:
            self.font_level = level
            self._notify()

    def set_btn_level(self, level):
        level = self._clamp(level, BTN_LEVELS)
        if level != self.btn_level:
            self.btn_level = level
            self._notify()

    def bind_change(self, callback):
        """注册一个「尺寸变了」的回调，界面据此重新排版。"""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def _notify(self):
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                pass

    # ---- 取值 ----
    @property
    def font(self):
        return FONT_LEVELS[self.font_level]

    @property
    def btn(self):
        return BTN_LEVELS[self.btn_level]

    def fs(self, key='base'):
        """某个用途的字号，返回 sp 值。"""
        return sp(self.font[key])

    def ds(self, key):
        """某个用途的尺寸，返回 dp 值。"""
        return dp(self.btn[key])

    @property
    def radius(self):
        return dp(self.btn['radius'])


# 全局实例，main.py 启动时会替换成从设置里读出来的档位
theme = Theme()


# ---------------------------------------------------------------- 渐变背景
def make_vertical_gradient(top, bottom):
    """
    生成一条竖直渐变纹理。
    1x2 的纹理 + 线性过滤，放大后就是平滑渐变，比写 shader 省事得多。
    注意 blit_buffer 的数据是从「最下面一行」开始填的。
    """
    tex = Texture.create(size=(1, 2), colorfmt='rgba')
    data = bytearray()
    for rgba_ in (bottom, top):
        for c in rgba_[:4]:
            data.append(int(round(max(0.0, min(1.0, c)) * 255)))
    tex.blit_buffer(bytes(data), colorfmt='rgba', bufferfmt='ubyte')
    tex.mag_filter = 'linear'
    tex.min_filter = 'linear'
    return tex


class GradientBackground(Widget):
    """铺满整个界面的渐变底板。"""

    def __init__(self, top=None, bottom=None, **kwargs):
        super(GradientBackground, self).__init__(**kwargs)
        self._tex = make_vertical_gradient(
            top or COLORS['bg_top'], bottom or COLORS['bg_bottom'])
        with self.canvas:
            self._rect = Rectangle(texture=self._tex, pos=self.pos, size=self.size)
        self.bind(pos=self._sync, size=self._sync)
        self._sync()

    def _sync(self, *_):
        self._rect.pos = self.pos
        self._rect.size = self.size


# ---------------------------------------------------------------- 基础容器
class Card(BoxLayout):
    """
    圆角卡片。原版 QListWidget::item 的白色圆角 + 阴影效果就靠它还原。
    """

    bg_color = ListProperty(list(COLORS['card']))
    radius = NumericProperty(0)
    shadow = BooleanProperty(True)
    shadow_alpha = NumericProperty(0.08)

    def __init__(self, **kwargs):
        self._radius_px = 0
        super(Card, self).__init__(**kwargs)
        with self.canvas.before:
            self._shadow_color = Color(0, 0, 0, self.shadow_alpha if self.shadow else 0)
            self._shadow_rect = RoundedRectangle(pos=self.pos, size=self.size)
            self._bg_color_instr = Color(*self.bg_color)
            self._bg_rect = RoundedRectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._sync, size=self._sync,
                  bg_color=self._sync_bg)
        # 半径没显式给的话，用主题里的默认值
        if self.radius:
            self._radius_px = self.radius
        else:
            self._radius_px = theme.radius
        self._sync()

    def _sync_bg(self, *_):
        self._bg_color_instr.rgba = self.bg_color

    def _sync(self, *_):
        r = [max(0, self._radius_px)]
        w, h = self.width, self.height
        if w > 0 and h > 0:
            r = [min(self._radius_px, w / 2.0, h / 2.0)]
        self._bg_rect.radius = r
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size
        # 阴影：往下偏移一点，宽度收窄一点，看起来就像浮起来
        self._shadow_rect.radius = r
        self._shadow_rect.pos = (self.x + dp(2), self.y - dp(4))
        self._shadow_rect.size = (max(0, self.width - dp(4)), self.height)
        self._shadow_color.a = self.shadow_alpha if self.shadow else 0


class TapCard(ButtonBehavior, Card):
    """可以点的卡片。按下时底色变浅蓝，给一点触觉反馈。"""

    pressed_color = ListProperty(list(COLORS['card_press']))

    def __init__(self, **kwargs):
        self._normal_color = list(kwargs.get('bg_color') or COLORS['card'])
        super(TapCard, self).__init__(**kwargs)
        self.bind(state=self._on_state)

    def _on_state(self, _widget, value):
        if value == 'down':
            self.bg_color = self.pressed_color
        else:
            self.bg_color = self._normal_color


class PillButton(ButtonBehavior, Card):
    """
    胶囊按钮。字体、高度都跟着主题档位走，老人手指粗也点得中。
    """

    text = StringProperty('')
    font_size = NumericProperty(0)
    bold = BooleanProperty(True)
    text_color = ListProperty(list(COLORS['white']))
    pressed_color = ListProperty(list(COLORS['blue_dark']))

    def __init__(self, text='', bg_color=None, **kwargs):
        bg = list(bg_color or COLORS['blue'])
        self._normal_color = list(bg)
        kwargs.setdefault('bg_color', bg)
        kwargs.setdefault('size_hint_y', None)
        kwargs.setdefault('height', theme.ds('pill_h'))
        kwargs.setdefault('padding', [dp(16), 0, dp(16), 0])
        super(PillButton, self).__init__(**kwargs)
        self.text = text
        self.bold = kwargs.get('bold', True)
        if not self.font_size:
            self.font_size = theme.fs('pill')
        self._label = Label(
            text=self.text,
            font_name=CJK_FONT,
            font_size=self.font_size,
            bold=self.bold,
            color=self.text_color,
            halign='center',
            valign='middle',
            shorten=True,
            shorten_from='right',
        )
        self.add_widget(self._label)
        self.bind(text=self._on_text, font_size=self._on_fs,
                  text_color=self._on_text_color, size=self._on_size)
        self._on_size()

    def _on_text(self, *_):
        self._label.text = self.text

    def _on_fs(self, *_):
        self._label.font_size = self.font_size

    def _on_text_color(self, *_):
        self._label.color = self.text_color

    def _on_size(self, *_):
        self._label.text_size = (max(0, self.width - dp(20)), None)
        self._label.size = self.size
        self._label.pos = self.pos

    def _on_state(self, _widget, value):
        if value == 'down':
            self.bg_color = self.pressed_color
        else:
            self.bg_color = self._normal_color


# ---------------------------------------------------------------- 缩略图色块
_ICON_CACHE = {}


def load_icon_texture(path):
    """
    从图片文件读一个纹理，读不出来（文件没了、格式不支持）就返回 None，
    界面会自动退回彩色方块。

    Kivy 的纹理不会自己保住底层的图片对象，所以这里缓存一份 CoreImage，
    否则过一会儿纹理可能变成空白。
    """
    if not path:
        return None
    key = str(path)
    try:
        core_img = _ICON_CACHE.get(key)
        if core_img is None:
            from kivy.core.image import Image as CoreImage
            core_img = CoreImage(key)
            _ICON_CACHE[key] = core_img
        return core_img.texture
    except Exception:
        return None


def forget_icon_texture(path):
    """换图 / 恢复默认之后调，不然缓存里还是旧那张。"""
    _ICON_CACHE.pop(str(path), None)


def cover_tex_coords(tex_size, widget_size):
    """
    算出「等比铺满并居中裁掉多余部分」的纹理坐标。
    对应 CSS 的 object-fit: cover。
    """
    tw, th = tex_size
    ww, wh = widget_size
    if tw <= 0 or th <= 0 or ww <= 0 or wh <= 0:
        return (0, 0, 1, 0, 1, 1, 0, 1)
    scale = max(float(ww) / tw, float(wh) / th)
    used_w = ww / scale
    used_h = wh / scale
    u0 = (tw - used_w) / 2.0 / tw
    v0 = (th - used_h) / 2.0 / th
    u1 = 1.0 - u0
    v1 = 1.0 - v0
    return (u0, v0, u1, v0, u1, v1, u0, v1)


class ThumbArea(Widget):
    """
    卡片上半部分的图块。
    有缩略图就画缩略图，没有就画一个彩色圆角块 + 首字，
    这正是原版 get_thumbnail 返回 None 时的那套兜底方案。
    """

    texture = ObjectProperty(None, allownone=True)
    fallback_text = StringProperty('')
    fallback_color = ListProperty(list(COLORS['blue']))
    radius = NumericProperty(0)
    cap_ratio = NumericProperty(1.0)

    def __init__(self, **kwargs):
        super(ThumbArea, self).__init__(**kwargs)
        self._radius_px = self.radius or theme.radius

        with self.canvas:
            # 底色（也是没有缩略图时的兜底）
            self._bg_color = Color(*self.fallback_color)
            self._bg_rect = RoundedRectangle(pos=self.pos, size=self.size)
            # 缩略图：用模板缓冲裁成圆角
            StencilPush()
            self._mask = RoundedRectangle(pos=self.pos, size=self.size)
            StencilUse()
            self._img_color = Color(1, 1, 1, 0)
            self._img_rect = Rectangle(pos=self.pos, size=self.size)
            StencilUnUse()
            self._mask2 = RoundedRectangle(pos=self.pos, size=self.size)
            StencilPop()

        self._label = Label(
            text=self.fallback_text,
            font_name=CJK_FONT,
            font_size=theme.fs('title'),
            bold=True,
            color=(1, 1, 1, 1),
            halign='center',
            valign='middle',
        )
        self.add_widget(self._label)

        self.bind(pos=self._sync, size=self._sync,
                  texture=self._sync_texture,
                  fallback_text=self._on_fallback_text,
                  fallback_color=self._on_fallback_color)
        self._sync()
        self._on_fallback_text()
        self._on_fallback_color()

    def _on_fallback_text(self, *_):
        self._label.text = self.fallback_text

    def _on_fallback_color(self, *_):
        self._bg_color.rgba = self.fallback_color

    def _sync_texture(self, *_):
        tex = self.texture
        if tex is None:
            self._img_color.a = 0
            self._label.opacity = 1
        else:
            self._img_rect.texture = tex
            self._img_color.a = 1
            self._label.opacity = 0
        self._sync()

    def _sync(self, *_):
        r = [min(self._radius_px, max(0, self.width / 2.0), max(0, self.height / 2.0))]
        for rect in (self._bg_rect, self._mask, self._mask2):
            rect.radius = r
            rect.pos = self.pos
            rect.size = self.size
        self._img_rect.pos = self.pos
        self._img_rect.size = self.size
        if self.texture is not None:
            try:
                self._img_rect.tex_coords = cover_tex_coords(
                    self.texture.size, (self.width, self.height))
            except Exception:
                pass
        self._label.size = self.size
        self._label.pos = self.pos
        self._label.text_size = (self.width, self.height)


class TileCard(TapCard):
    """
    剧集 / 视频卡片：上面是图，下面是名字。
    对应原版 QListWidget 的 IconMode + setIconSize + setGridSize。
    """

    title = StringProperty('')
    subtitle = StringProperty('')
    texture = ObjectProperty(None, allownone=True)
    fallback_color = ListProperty(list(COLORS['blue']))
    title_color = ListProperty(list(COLORS['text']))
    warn = BooleanProperty(False)

    def __init__(self, title='', subtitle='', fallback_color=None,
                 texture=None, tile_size=None, **kwargs):
        tile = tile_size or dp(160)
        kwargs.setdefault('orientation', 'vertical')
        kwargs.setdefault('padding', [dp(8), dp(8), dp(8), dp(8)])
        kwargs.setdefault('spacing', dp(6))
        # 宽度必须交给 GridLayout 分配（size_hint_x=1）。
        # 如果写成 size_hint=(None, None) 再给个固定 width，
        # GridLayout 会认为这一列用默认列宽，而默认是 0 —— 整张卡片就消失了。
        kwargs.setdefault('size_hint', (1, None))
        kwargs.setdefault('height', tile + dp(72))
        kwargs.setdefault('bg_color', list(COLORS['card']))
        super(TileCard, self).__init__(**kwargs)

        self.title = title
        self.subtitle = subtitle
        self.fallback_color = list(fallback_color or COLORS['blue'])
        self.texture = texture

        self.thumb = ThumbArea(
            fallback_text=(title or '?')[:1],
            fallback_color=list(fallback_color or COLORS['blue']),
            texture=texture,
            size_hint_y=1,
        )
        self.add_widget(self.thumb)

        # 标题高度跟着字号走，不然最大字号档位下会被切掉
        title_h = max(dp(46), sp(self._card_font_size()) * 2.2)
        self._title_label = Label(
            text=self.title,
            font_name=CJK_FONT,
            font_size=theme.fs('card'),
            bold=True,
            color=list(COLORS['text']),
            halign='center',
            valign='middle',
            shorten=True,
            shorten_from='right',
            size_hint_y=None,
            height=title_h,
        )
        self.add_widget(self._title_label)

        self.bind(title=self._on_title, texture=self._on_texture,
                  fallback_color=self._on_color, size=self._on_size_width)
        self._on_size_width()
        self._on_title()

    @staticmethod
    def _card_font_size():
        return theme.font['card']

    def _on_title(self, *_):
        self._title_label.text = self.title
        self.thumb.fallback_text = (self.title or '?')[:1]

    def _on_texture(self, *_):
        self.thumb.texture = self.texture

    def _on_color(self, *_):
        self.thumb.fallback_color = self.fallback_color

    def _on_size_width(self, *_):
        self._title_label.text_size = (max(0, self.width - dp(12)), None)

    def hide_title(self):
        """
        把名字那一行收掉，地方全给缩略图。

        横屏手机的可视区只剩 90dp 上下，卡片里再站一行字，
        缩略图就只剩一条缝了。照片本来也不用看文件名 —— 老人看的是图。
        文件夹格子不能收（中文名是唯一能认出「这是哪一本」的东西）。
        """
        self._title_label.height = 0
        self._title_label.opacity = 0
        self.spacing = 0


# ---------------------------------------------------------------- 主页大按钮
class BigEntryButton(TapCard):
    """
    主页那三个大按钮。
    原版是横排三个大方块（图标在上、文字在下），手机上竖排更好点，
    所以这里做成「整行的大卡片」：左边一个彩色图标方块，右边是大字。
    """

    title = StringProperty('')
    accent = ListProperty(list(COLORS['cat_huangmei']))
    icon_texture = ObjectProperty(None, allownone=True)

    def __init__(self, title='', accent=None, **kwargs):
        accent = list(accent or COLORS['cat_huangmei'])
        kwargs.setdefault('orientation', 'horizontal')
        kwargs.setdefault('padding', [dp(14), dp(10), dp(20), dp(10)])
        kwargs.setdefault('spacing', dp(18))
        kwargs.setdefault('size_hint_y', None)
        kwargs.setdefault('height', theme.ds('cat_h'))
        kwargs.setdefault('bg_color', list(COLORS['card']))
        super(BigEntryButton, self).__init__(**kwargs)

        self.title = title
        self.accent = accent

        size = max(dp(56), self.height - dp(20)) if self.height else dp(90)
        self.icon_area = ThumbArea(
            fallback_text=(title or '?')[:1],
            fallback_color=list(accent),
            size_hint=(None, 1),
            width=size,
        )
        self.add_widget(self.icon_area)

        self._label = Label(
            text=self.title,
            font_name=CJK_FONT,
            font_size=theme.fs('title'),
            bold=True,
            color=list(COLORS['text_title']),
            halign='left',
            valign='middle',
        )
        self.add_widget(self._label)

        self.bind(title=self._on_title, accent=self._on_accent,
                  icon_texture=self._on_icon_texture, size=self._layout_children)
        self._layout_children()
        self._on_title()
        self._on_icon_texture()

    def _on_title(self, *_):
        self._label.text = self.title
        self.icon_area.fallback_text = (self.title or '?')[:1]

    def _on_accent(self, *_):
        self.icon_area.fallback_color = self.accent

    def _on_icon_texture(self, *_):
        self.icon_area.texture = self.icon_texture

    def _layout_children(self, *_):
        side = max(dp(56), self.height - dp(20))
        self.icon_area.width = side
        self._label.text_size = (
            max(0, self.width - side - dp(60)), None)


class BigEntryTile(TapCard):
    """
    横屏主页用的入口方块：上图下字，三个并排。

    为什么竖屏那套不能直接用：BigEntryButton 是「整行长卡片」，
    一张就有 136dp 高，三张加上说明文字要 500dp 上下。
    横屏手机高度只有 300dp 出头，摆不下三行，只能改成一排三个 ——
    这也正好是桌面原版（PyQt5）的排法。
    """

    title = StringProperty('')
    subtitle = StringProperty('')
    accent = ListProperty(list(COLORS['cat_huangmei']))
    icon_texture = ObjectProperty(None, allownone=True)

    def __init__(self, title='', subtitle='', accent=None, **kwargs):
        accent = list(accent or COLORS['cat_huangmei'])
        kwargs.setdefault('orientation', 'vertical')
        kwargs.setdefault('padding', [dp(12), dp(12), dp(12), dp(10)])
        kwargs.setdefault('spacing', dp(6))
        kwargs.setdefault('bg_color', list(COLORS['card']))
        super(BigEntryTile, self).__init__(**kwargs)

        self.title = title
        self.subtitle = subtitle
        self.accent = accent

        # 上半部分：彩色方块 + 首字（有缩略图时自动换成缩略图）
        self.icon_area = ThumbArea(
            fallback_text=(title or '?')[:1],
            fallback_color=list(accent),
            size_hint=(1, 1),
        )
        self.add_widget(self.icon_area)

        self._label = Label(
            text=self.title,
            font_name=CJK_FONT,
            font_size=theme.fs('title'),
            bold=True,
            color=list(COLORS['text_title']),
            halign='center',
            valign='middle',
            size_hint_y=None,
            height=dp(46),
            shorten=True,
            shorten_from='right',
        )
        self._label.bind(size=lambda w, *_: setattr(w, 'text_size', (w.width, None)))
        self.add_widget(self._label)

        self._sub = Label(
            text=self.subtitle,
            font_name=CJK_FONT,
            font_size=theme.fs('card'),
            color=list(COLORS['text_dim']),
            halign='center',
            valign='middle',
            size_hint_y=None,
            height=dp(56),
        )
        self._sub.bind(size=lambda w, *_: setattr(w, 'text_size', (w.width, None)))
        self.add_widget(self._sub)

        self.bind(title=self._on_title, subtitle=self._on_subtitle,
                  accent=self._on_accent,
                  icon_texture=self._on_icon_texture)
        self._on_title()
        self._on_subtitle()
        self._on_icon_texture()

    def _on_title(self, *_):
        self._label.text = self.title
        self.icon_area.fallback_text = (self.title or '?')[:1]

    def _on_subtitle(self, *_):
        self._sub.text = self.subtitle

    def _on_accent(self, *_):
        self.icon_area.fallback_color = self.accent

    def _on_icon_texture(self, *_):
        self.icon_area.texture = self.icon_texture


class SectionLabel(Label):
    """界面上的小标题。"""

    def __init__(self, text='', **kwargs):
        kwargs.setdefault('font_name', CJK_FONT)
        kwargs.setdefault('font_size', theme.fs('card'))
        kwargs.setdefault('bold', True)
        kwargs.setdefault('color', list(COLORS['text']))
        kwargs.setdefault('halign', 'left')
        kwargs.setdefault('valign', 'middle')
        kwargs.setdefault('size_hint_y', None)
        kwargs.setdefault('height', dp(34))
        super(SectionLabel, self).__init__(text=text, **kwargs)


# ---------------------------------------------------------------- 喇叭图标
_SPEAKER_CACHE = {}


def speaker_texture(size_px, color=None, muted=False):
    """
    生成一张喇叭图标贴图。

    为什么不用文字字形：内置思源黑体里没有「扬声器」这类 emoji（Kivy 不做字体回退，
    缺字形就画方框），而 ♪ 只是音符，老人看不出那是「音量」。

    为什么不用 canvas 一条条画：声波是弧线，canvas 画出来线宽是按 dp 走的，
    在小尺寸下会跟喇叭口粘成一坨。改成 Pillow 按**实际像素**画、
    4 倍超采样再缩小，边缘干净，声波和喇叭口之间也留得开。

    图形比例照着 Material Icons 的 volume_up 来（喇叭 + 两道声波）。
    """
    size_px = int(max(16, size_px))
    key = (size_px, tuple(color or COLORS['text']), bool(muted))
    if key in _SPEAKER_CACHE:
        return _SPEAKER_CACHE[key]
    tex = _render_speaker(size_px, key[1], bool(muted))
    if tex is not None:
        _SPEAKER_CACHE[key] = tex
    return tex


def _render_speaker(size_px, color, muted):
    try:
        import io
        from PIL import Image, ImageDraw
        from kivy.core.image import Image as CoreImage
    except Exception:
        return None
    ss = 4                                  # 超采样倍数，缩回去就等于抗锯齿
    S = size_px * ss
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rgba = tuple(int(round(c * 255)) for c in list(color)[:3]) + (255,)

    def P(x, y):
        # 坐标按 0..100 的方格给，换算成实际像素
        return (x * S / 100.0, y * S / 100.0)

    # 喇叭屁股（方块）+ 喇叭口（梯形）
    draw.rectangle([P(6, 36), P(24, 64)], fill=rgba)
    draw.polygon([P(24, 36), P(46, 14), P(46, 86), P(24, 64)], fill=rgba)

    if not muted:
        # 两道声波。圆心放在喇叭口 (46, 50)，半径 12 和 22：
        # 内圈离口子有 12×cos(55°)≈7 个单位，不会叠上去。
        lw = max(2, int(S * 0.06))
        for radius in (12.0, 22.0):
            draw.arc([P(46 - radius, 50 - radius),
                      P(46 + radius, 50 + radius)],
                     -55, 55, fill=rgba, width=lw)

    box = img.getbbox()
    if not box:
        return None
    img = img.crop(box)                     # 去掉四周空白
    side = max(img.size)                    # 摆进正方形，保持长宽比
    square = Image.new('RGBA', (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    img = square.resize((size_px, size_px), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    ci = CoreImage(buf, ext='png')
    tex = ci.texture
    if tex is not None:
        _PHOTO_BUF.append(buf)              # 保住内存流，别让 GC 提前收掉
        del _PHOTO_BUF[:-120]
    return tex


class SpeakerIcon(Widget):
    """
    音量喇叭图标。

    muted=True 时喇叭变灰、而且不画声波，一眼能看出「现在是静音」。
    """

    color = ListProperty(list(COLORS['text']))
    muted = BooleanProperty(False)

    def __init__(self, **kwargs):
        kwargs.setdefault('size_hint', (None, None))
        super(SpeakerIcon, self).__init__(**kwargs)
        # 单独开一层 Canvas：直接 canvas.clear() 会把子控件的绘制也一起清掉
        self._cnv = Canvas()
        self.canvas.add(self._cnv)
        self.bind(pos=self._redraw, size=self._redraw,
                  color=self._redraw, muted=self._redraw)
        self._redraw()

    def _redraw(self, *_):
        self._cnv.clear()
        w, h = self.size
        s = int(min(w, h))
        if s < 12:
            return
        col = list(COLORS['text_dim']) if self.muted else list(self.color)
        with self._cnv:
            tex = speaker_texture(s, col, self.muted)
            if tex is not None:
                Color(1, 1, 1, 1)
                Rectangle(texture=tex,
                          pos=(self.center_x - s / 2.0,
                               self.center_y - s / 2.0),
                          size=(s, s))
                return
            # 没有 Pillow 的兜底：画个最简的喇叭，总比空白强
            Color(*col)
            Rectangle(pos=(self.x + w * 0.16, self.center_y - s * 0.12),
                      size=(s * 0.14, s * 0.24))
            Triangle(points=[self.x + w * 0.16 + s * 0.14,
                             self.center_y - s * 0.12,
                             self.x + w * 0.16 + s * 0.36,
                             self.center_y - s * 0.26,
                             self.x + w * 0.16 + s * 0.36,
                             self.center_y + s * 0.26])
            Triangle(points=[self.x + w * 0.16 + s * 0.14,
                             self.center_y - s * 0.12,
                             self.x + w * 0.16 + s * 0.36,
                             self.center_y + s * 0.26,
                             self.x + w * 0.16 + s * 0.14,
                             self.center_y + s * 0.12])


# ---------------------------------------------------------------- 照片缩略图
# CoreImage 从内存流读图时要保证流活着，这里存一份引用，别让 GC 回收掉
_PHOTO_BUF = []


def load_photo_texture(path, max_side=220):
    """
    把一张照片读成小纹理，给「选图片」列表当预览。

    两个要点：
      * **先按 EXIF 摆正**。手机照片常常是横着存、靠 EXIF 里的方向标记
        告诉看图软件怎么转，Pillow 不会自动转 —— 不摆正的话预览和存下来的
        图标都是倒的（用户实测反馈过）。
      * **先缩再交给 Kivy**。不要直接把原图丢给 Kivy 的 Image：
        4000x3000 解开就是 48MB 显存，列表里有几十张时老机器直接崩。
    """
    try:
        import io
        from PIL import Image, ImageOps
        from kivy.core.image import Image as CoreImage
    except Exception:
        return None
    try:
        img = Image.open(str(path))
        try:
            fixed = ImageOps.exif_transpose(img)
            if fixed is not None:
                img = fixed
        except Exception:
            pass
        # JPEG 可以按 1/2、1/4、1/8 粗解码，缩略图不需要全解，快好几倍
        try:
            img.draft('RGB', (max_side, max_side))
        except Exception:
            pass
        img.thumbnail((max_side, max_side), Image.BILINEAR)
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA')
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        buf.seek(0)
        ci = CoreImage(buf, ext='png')
        tex = ci.texture
    except Exception:
        return None
    if tex is None:
        return None
    _PHOTO_BUF.append(buf)
    # 只留最近的几十份，别无限涨
    del _PHOTO_BUF[:-80]
    return tex


__all__ = [
    'COLORS', 'rgba', 'shade', 'lighten', 'theme', 'Theme', 'CJK_FONT',
    'register_fonts', 'make_vertical_gradient', 'GradientBackground',
    'Card', 'TapCard', 'PillButton', 'ThumbArea', 'TileCard',
    'BigEntryButton', 'BigEntryTile', 'SectionLabel', 'cover_tex_coords',
    'SpeakerIcon', 'load_photo_texture',
    'load_icon_texture', 'forget_icon_texture',
    'dp', 'sp', 'IS_ANDROID',
]
