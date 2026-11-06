# -*- coding: utf-8 -*-
"""
smoke_test.py —— 在电脑上把整个程序跑一遍，检查逻辑和界面构建有没有问题

能验到的：
    * 目录扫描、自然排序、播放记录读写
    * 六个界面能不能正常构建出来
    * 界面之间的跳转（home -> series -> videos -> player -> 返回）
    * 播放进度条、快进快退的取值计算
    * 设置里的字号/按钮档位切换
    * 返回键的处理

验不到的：
    * 真正的视频解码播放。桌面 PyQt5/Kivy 环境里没有安卓的 MediaCodec，
      这里用一个假的 Video 控件替过去，只测它周边的逻辑。
      真正的播放要在手机上装了 APK 才能确认。

运行：python tools/smoke_test.py
"""

import os
import sys
import shutil
import tempfile
from pathlib import Path

# 让 Kivy 安静点。这里用真实窗口跑（本机有显卡），能顺带验证界面布局不出错。
os.environ.setdefault('KIVY_NO_ARGS', '1')
os.environ.setdefault('KIVY_LOG_LEVEL', 'warning')
# 别让 Kivy 去删旧日志：这台机器的安全策略会把删除重定向到回收站，
# 回收站偶尔失败就直接抛异常，import 阶段就死了
os.environ.setdefault('KIVY_NO_FILELOG', '1')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kivy.clock import Clock
from kivy.properties import (BooleanProperty, NumericProperty, StringProperty)
from kivy.uix.widget import Widget

import elder_core as core
import elder_theme as th
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


def find_types(widget, wanted, found=None):
    """在控件树里找指定类型的控件（主页横竖两套排法靠它区分）。"""
    if found is None:
        found = []
    if isinstance(widget, wanted):
        found.append(widget)
    for child in widget.children:
        find_types(child, wanted, found)
    return found


# ---------------------------------------------------------------- 假视频控件
class FakeVideo(Widget):
    """顶替 kivy.uix.video.Video，接口跟真实的一致。"""

    source = StringProperty('')
    state = StringProperty('stop')
    duration = NumericProperty(0)
    position = NumericProperty(0)
    volume = NumericProperty(1.0)
    allow_stretch = BooleanProperty(True)
    keep_ratio = BooleanProperty(True)

    def seek(self, ratio):
        self.position = max(0.0, min(1.0, float(ratio))) * self.duration


def build_fake_library(root):
    """
    造一个跟说明里一样的目录结构。

    戏曲那个文件夹**故意还用旧名字「黄梅戏」** —— 老用户的手机里就是这个名字，
    改了按钮名之后照样得能读出来（见 core.CAT_FOLDER_ALIASES）。
    """
    huangmei = root / '黄梅戏'
    tv = root / '电视剧'

    # 黄梅戏：两个剧集
    for series, count in (('女驸马', 3), ('天仙配', 2)):
        d = huangmei / series
        d.mkdir(parents=True, exist_ok=True)
        for i in range(1, count + 1):
            (d / ('%02d.mp4' % i)).write_bytes(b'\x00' * 64)

    # 电视剧：一个剧集，故意用 第2集/第10集 来验自然排序
    xi = tv / '西游记'
    xi.mkdir(parents=True, exist_ok=True)
    for name in ('第2集.mp4', '第10集.mp4', '第1集.mp4'):
        (xi / name).write_bytes(b'\x00' * 64)

    # 分类下直接放视频（没有剧集子文件夹），原版会漏掉这种情况
    (tv / '单本剧.mp4').write_bytes(b'\x00' * 64)

    # 手机放不了的格式
    (xi / '老片.rmvb').write_bytes(b'\x00' * 64)

    return root


def test_core(tmp_root):
    print('\n--- 核心逻辑 ---')

    # 自然排序
    names = ['第10集.mp4', '第2集.mp4', '第1集.mp4']
    ordered = sorted(names, key=core.natural_key)
    check('自然排序：第2集 排在 第10集 前面',
          ordered == ['第1集.mp4', '第2集.mp4', '第10集.mp4'],
          str(ordered))

    # 时间格式
    check('时间格式 0 -> 00:00', core.format_time(0) == '00:00')
    check('时间格式 65s -> 01:05', core.format_time(65000) == '01:05')
    check('时间格式 1h -> 1:00:00', core.format_time(3600000) == '1:00:00')

    # 扫描
    cats = core.scan_categories(tmp_root)
    check('扫到 2 个分类', len(cats) == 2, str(sorted(cats.keys())))
    check('旧的「黄梅戏」文件夹被归到「戏曲」分类下',
          len(cats.get('戏曲', [])) == 2, str(sorted(cats.keys())))
    check('不会另外冒出一个叫「黄梅戏」的分类',
          '黄梅戏' not in cats, str(sorted(cats.keys())))
    tv_series = cats.get('电视剧', [])
    check('电视剧下识别出「单本剧」这种直接放视频的分类',
          any(s.name == '电视剧' for s in tv_series),
          str([s.name for s in tv_series]))

    # 视频列表 + 排序
    xiyouji = tmp_root / '电视剧' / '西游记'
    videos = core.list_videos(xiyouji)
    check('西游记里扫到 4 个视频（含 1 个 rmvb）', len(videos) == 4,
          str([v.name for v in videos]))
    check('视频按自然顺序排列',
          [v.name for v in videos][0] == '第1集.mp4',
          str([v.name for v in videos]))
    check('rmvb 被判定为手机放不了',
          core.can_play(xiyouji / '老片.rmvb', on_android=True) is False)
    check('mp4 被判定为能放',
          core.can_play(xiyouji / '第1集.mp4', on_android=True) is True)

    # 诊断信息
    info = core.scan_root_report(tmp_root)
    check('scan_root_report 能读出分类数', info['category_count'] == 2,
          str(info))

    # 设置 / 记录读写
    store = core.Store(base_dir=tmp_root / '_cfg')
    store.set_setting('font_level', 4)
    store2 = core.Store(base_dir=tmp_root / '_cfg')
    check('设置能存能读', store2.get_setting('font_level') == 4)

    store.upsert_record(xiyouji, 0, 300000)
    store.upsert_record(tmp_root / '黄梅戏' / '女驸马', 1, 400000)
    store3 = core.Store(base_dir=tmp_root / '_cfg')
    check('播放记录能存能读', len(store3.reload_records()) == 2,
          str(store3.records))

    valid = store3.valid_records()
    check('有效记录能补出视频文件', len(valid) == 2 and valid[0][2].exists(),
          str([v[2].name for v in valid]))

    # 同剧集只留一条
    store3.upsert_record(xiyouji, 2, 500000)
    same = [r for r in store3.records if r['series_path'] == str(xiyouji)]
    check('同一剧集只保留一条记录', len(same) == 1, str(same))

    # 缩略图缓存路径稳定
    p1 = core.thumb_cache_path(store, xiyouji / '第1集.mp4', 256)
    p2 = core.thumb_cache_path(store, xiyouji / '第1集.mp4', 256)
    check('缩略图缓存文件名稳定', p1 == p2 and p1.suffix == '.jpg')

    # 系统目录的中文名：老人看不懂 DCIM / Pictures 这些
    check('DCIM 显示成相机相册',
          core.folder_label(Path('/storage/emulated/0/DCIM'))
          == '相机相册（DCIM）',
          core.folder_label(Path('/storage/emulated/0/DCIM')))
    check('Pictures 显示成图片',
          core.folder_label(Path('/storage/emulated/0/Pictures'))
          == '图片（Pictures）')
    check('/storage/emulated/0 显示成手机存储',
          core.folder_label(Path('/storage/emulated/0')) == '手机存储',
          core.folder_label(Path('/storage/emulated/0')))
    check('不认识的文件夹照原名显示',
          core.folder_label(Path('/x/女驸马照片')) == '女驸马照片')
    quick = core.photo_quick_dirs()
    check('快捷入口只给真实存在的目录',
          all(p.is_dir() for _label, p in quick), str(quick))


def test_ui(tmp_root):
    print('\n--- 界面构建与跳转 ---')

    # 把数据目录指到临时目录，别污染项目
    core.app_private_dir = lambda: tmp_root / '_appdata'

    # 用假的 Video 顶替真的
    player_main.Video = FakeVideo

    app = player_main.ElderPlayerApp()

    results = {}

    def tour(_dt):
        try:
            store = app.store
            store.set_root_path(tmp_root)
            app.scan()

            check('扫描到 2 个分类', len(app.categories) == 2,
                  str(sorted(app.categories.keys())))
            check('戏曲入口显示 2 个剧集（文件夹还叫黄梅戏）',
                  app.count_for_category('戏曲') == 2)
            check('历史播放数量为 0', app.count_history() == 0)

            # 六个界面都构建一遍
            for name in ('home', 'series', 'videos', 'player',
                         'history', 'settings', 'help'):
                try:
                    app.sm.current = name
                    screen = app.sm.get_screen(name)
                    screen.rebuild() if hasattr(screen, 'rebuild') else None
                    results[name] = True
                except Exception as exc:
                    results[name] = '%s: %s' % (type(exc).__name__, exc)

            for name, ok in results.items():
                check('界面 %s 能构建' % name, ok is True, '' if ok is True else str(ok))

            # 主页有两套排法：横屏一排三个方块，竖屏一列三行整行卡片。
            # App 默认横屏，但分屏/桌面调试时窗口可能是竖的，两套都得能用。
            #
            # 注意：这里不能用「改 Window.size 再 rebuild」的办法来测 ——
            # Kivy 的 Window.size 赋值是异步的，赋值完立刻读还是旧尺寸
            # （实测 1200x900 设成 400x820，回来仍然是 1200x900）。
            # 直接替换布局判定的入口，才能确定性地测到两条分支。
            home = app.sm.get_screen('home')

            def home_layout(landscape):
                home._is_landscape = lambda: landscape
                home.rebuild()
                return (len(find_types(home.content, th.BigEntryTile)),
                        len(find_types(home.content, th.BigEntryButton)))

            try:
                tiles, rows = home_layout(True)
                check('横屏主页：三个入口用并排方块', tiles == 3, str(tiles))
                check('横屏主页：不混用整行长卡片', rows == 0, str(rows))

                titles = sorted(t.title for t in find_types(home.content, th.BigEntryTile))
                check('横屏主页：三个方块对应三个入口',
                      titles == sorted(['戏曲', '电视剧', '历史播放']), str(titles))

                tiles, rows = home_layout(False)
                check('竖屏主页：三个入口用整行卡片', rows == 3, str(rows))
                check('竖屏主页：不混用方块', tiles == 0, str(tiles))
            finally:
                try:
                    del home._is_landscape
                except AttributeError:
                    pass
                home.rebuild()

            # 方向现在是设置项，不再是写死的常量
            check('默认是横屏', app.store.default_landscape() is True)
            app.set_default_orientation(False)
            check('改成竖屏后记住设置', app.store.default_landscape() is False)
            app.set_default_orientation(True)
            check('改回横屏', app.store.default_landscape() is True)

            # 跳转：主页 -> 戏曲 -> 女驸马 -> 视频列表
            app.sm.current = 'home'
            app.open_category('戏曲')
            check('进入戏曲分类', app.sm.current == 'series',
                  app.sm.current)

            series_path = None
            for p in app.categories['戏曲']:
                if p.name == '女驸马':
                    series_path = p
            check('找到女驸马剧集', series_path is not None)
            app.open_series({'path': series_path})
            check('进入剧集', app.sm.current == 'videos', app.sm.current)
            check('剧集里 3 个视频', len(app.current_videos) == 3,
                  str([v.name for v in app.current_videos]))

            # 电视剧：验证 rmvb 被过滤 + 单本剧
            app.go_home()
            app.open_category('电视剧')
            check('进入电视剧分类', app.sm.current == 'series',
                  app.sm.current)
            xiyouji = None
            for p in app.categories['电视剧']:
                if p.name == '西游记':
                    xiyouji = p
            check('找到西游记剧集', xiyouji is not None)

            app.open_series({'path': xiyouji})
            check('桌面端不做格式过滤，4 个都在',
                  len(app.current_videos) == 4,
                  str([v.name for v in app.current_videos]))

            # 把 IS_ANDROID 打开，模拟手机上的行为：rmvb 应该被挡掉
            player_main.IS_ANDROID = True
            filtered = app.filter_videos(app.current_series_videos_all)
            player_main.IS_ANDROID = False
            check('模拟手机上：rmvb 被过滤，只剩 3 个 mp4',
                  len(filtered) == 3,
                  str([v.name for v in filtered]))

            # 设置里的开关能改变过滤结果
            app.toggle_unplayable(True)
            check('打开开关后能显示出 4 个',
                  len(app.filter_videos(app.current_series_videos_all)) == 4)
            app.toggle_unplayable(False)

            # 进播放界面（假视频控件）
            app.open_video({'index': 1})
            check('进入播放界面', app.sm.current == 'player', app.sm.current)
            ps = app.sm.get_screen('player')
            check('播放界面指向第 2 个视频',
                  app.current_index == 1)
            check('正在播放', ps.video.state == 'play', ps.video.state)

            # 快进快退 30 秒按需求去掉了，这里确认按钮确实不在
            check('播放页没有快进快退按钮',
                  not hasattr(ps, 'btn_rewind')
                  and not hasattr(ps, 'btn_forward'))
            check('音量是减/加两个按钮',
                  hasattr(ps, 'btn_vol_down') and hasattr(ps, 'btn_vol_up'))

            # 控制栏顺序：返回 - 播放 -（空当）- 音量
            controls = ps.btn_back.parent
            kids = list(reversed(controls.children))   # children 是反序的
            check('控制栏依次是 返回 / 播放 / 空当 / 音量',
                  len(kids) == 4 and kids[0] is ps.btn_back
                  and kids[1] is ps.btn_play
                  and ps.btn_vol_up in kids[3].children,
                  str([type(k).__name__ for k in kids]))

            # 音量加减：桌面没有系统音量接口，退化成调播放器自己的音量
            ps.video.volume = 0.5
            ps.change_volume(1)
            check('音量加一格', abs(ps.video.volume - 0.6) < 0.01,
                  str(ps.video.volume))
            ps.change_volume(-1)
            check('音量减一格', abs(ps.video.volume - 0.5) < 0.01,
                  str(ps.video.volume))

            # 拖动进度条
            ps.video.duration = 100000.0
            ps.video.position = 40000.0
            ps._on_seek(0.5, True)
            check('拖到一半 -> 50 秒',
                  abs(ps.video.position - 50000.0) < 1.0,
                  str(ps.video.position))

            # 播放/暂停切换
            ps.toggle_play()
            check('切到暂停', ps.video.state == 'pause', ps.video.state)
            ps.toggle_play()
            check('切回播放', ps.video.state == 'play', ps.video.state)

            # 播完自动下一集
            last = len(app.current_videos) - 1
            app.current_index = 0
            ps._on_finished()
            check('播完自动切下一集', app.current_index == 1,
                  str(app.current_index))

            # 最后一集播完不应该再往下切
            app.current_index = last
            ps._on_finished()
            check('最后一集播完不再往下切', app.current_index == last,
                  '%d (共 %d 集)' % (app.current_index, last + 1))

            # 保存进度：少于 3 分钟不记，超过才记（沿用原版规则）
            app.current_index = 0
            ps.video.position = 100000.0        # 1 分 40 秒
            ps.save_progress()
            check('不足 3 分钟不写记录',
                  len([r for r in app.store.records
                       if r['series_path'] == str(app.current_series)]) == 0)
            ps.video.position = 250000.0        # 4 分 10 秒
            ps.save_progress()
            stored = [r for r in app.store.records
                      if r['series_path'] == str(app.current_series)]
            check('超过 3 分钟写入记录', len(stored) == 1, str(stored))

            # 历史播放界面能读到
            app.store.reload_records()
            app.sm.current = 'history'
            app.sm.get_screen('history').rebuild()
            check('历史播放读到 1 条', len(app.store.valid_records()) == 1)

            # 字号档位
            before = th.theme.font_level
            app.bump_font(1)
            check('字号档位能加', th.theme.font_level == min(5, before + 1),
                  str(th.theme.font_level))
            app.bump_font(-1)
            check('字号档位能减', th.theme.font_level == before,
                  str(th.theme.font_level))

            # 返回键一律在底部：底栏是 content 里最后加进去的那个控件
            for name in ('series', 'videos', 'history', 'settings'):
                screen = app.sm.get_screen(name)
                screen.rebuild()
                bottom = screen.content.children[0]
                check('%s 的返回键在底部' % name,
                      len(find_types(bottom, player_main.BackButton)) == 1,
                      type(bottom).__name__)

            # 主页图标：上传 -> 按 EXIF 摆正 -> 缩到 512 以内 -> 恢复默认
            try:
                from PIL import Image as PILImage
                src = tmp_root / 'icon_test.png'
                PILImage.new('RGB', (1600, 1200), (200, 30, 30)).save(str(src))
                app.apply_home_icon('xiqu', src, name='戏曲')
                saved = app.store.home_icon('xiqu')
                check('上传后存下了图标',
                      saved is not None and saved.exists(), str(saved))
                if saved is not None:
                    size = PILImage.open(str(saved)).size
                    check('大图被缩到 512 以内',
                          max(size) <= 512, str(size))

                # 手机照片是「横着存 + EXIF 标记要转 90 度」。
                # 不按 EXIF 摆正的话，存下来就是倒的（用户实测反馈过）。
                rot = tmp_root / 'icon_rot.jpg'
                portrait = PILImage.new('RGB', (800, 600), (30, 60, 200))
                exif = portrait.getexif()
                exif[274] = 6                     # Orientation=6：顺时针转 90°
                portrait.save(str(rot), 'JPEG', exif=exif)
                app.apply_home_icon('tv', str(rot), name='电视剧')
                saved_rot = app.store.home_icon('tv')
                if saved_rot is not None:
                    w, h = PILImage.open(str(saved_rot)).size
                    check('按 EXIF 方向摆正了（竖图存完还是竖的）',
                          h > w, '%dx%d' % (w, h))
                else:
                    check('按 EXIF 方向摆正了（竖图存完还是竖的）', False,
                          '没存下来')
                app.reset_home_icon('tv', name='电视剧')

                app.reset_home_icon('xiqu', name='戏曲')
                check('恢复默认后图标文件被删掉',
                      app.store.home_icon('xiqu') is None)
            except ImportError:
                print('  [SKIP] 没装 Pillow，跳过图标上传测试')

            # 播放控制栏：返回 / 播放 ⟶ 空当 ⟶ ➖ 喇叭 ➕
            player = app.sm.get_screen('player')
            player.rebuild()
            controls = player.btn_back.parent
            kids = list(reversed(controls.children))
            check('控制栏第一个是返回', kids[0] is player.btn_back)
            check('控制栏第二个是播放', kids[1] is player.btn_play)
            gap_ok = (len(kids) > 2 and not hasattr(kids[2], 'on_release')
                      and kids[2].width >= 20)
            check('播放和音量之间有空开的距离', gap_ok,
                  '%.0f' % (kids[2].width if len(kids) > 2 else -1))
            vol_box = kids[-1]
            vol_kids = list(reversed(vol_box.children))
            check('音量组顺序是 ➖ 喇叭 ➕',
                  vol_kids[0] is player.btn_vol_down
                  and isinstance(vol_kids[1], th.SpeakerIcon)
                  and vol_kids[2] is player.btn_vol_up)

            # 喇叭图标是画出来的贴图（字体里没有 🔊，用文字只会是方框）
            tex = th.speaker_texture(84, th.COLORS['text'], False)
            check('喇叭图标能生成贴图', tex is not None and tex.size[0] > 40,
                  str(getattr(tex, 'size', None)))

            # 选图页：存在、中文目录名、返回回设置
            picker = app.sm.get_screen('picker')
            check('选图页注册好了', picker is not None)
            app.sm.current = 'settings'
            picker.show(str(tmp_root), lambda p: None)
            check('选图页能打开', app.sm.current == 'picker', app.sm.current)
            handled = app._on_key(None, 27, 0, '', [])
            check('选图页按返回回到设置',
                  handled is True and app.sm.current == 'settings',
                  app.sm.current)

            # 返回键
            app.sm.current = 'player'
            handled = app._on_key(None, 27, 0, '', [])
            check('播放界面按返回键不退出程序', handled is True)
            check('返回键回到了视频列表', app.sm.current == 'videos',
                  app.sm.current)

            app.sm.current = 'home'
            check('主页按返回键交还系统', app._on_key(None, 27, 0, '', []) is False)

        except Exception as exc:
            import traceback
            FAILED.append(('界面流程', traceback.format_exc()))
            print('  [FAIL] 界面流程异常：')
            traceback.print_exc()
        finally:
            app.stop()

    Clock.schedule_once(tour, 0.6)
    app.run()


def test_font_glyphs():
    """
    界面上每一个字，内置字体里都得真有对应的字形。

    Kivy 不做字体回退：字体里没有的字会渲染成一个空心方框（豆腐块）。
    Noto Sans SC 是中文字体，**不带 emoji** —— 之前标题写成「🎬 老人视频播放器」，
    在电脑和手机上都显示成「☒ 老人视频播放器」，就是因为这个。

    做法是直接读字体的 cmap 表，把源码里出现的字符挨个查一遍。
    """
    print('【字体字形】界面上每个字都得有字形，不能出现方框')

    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print('  [SKIP] 没装 fontTools，跳过（pip install fonttools 后才会检查）')
        return

    root = Path(__file__).resolve().parent.parent
    font_path = root / 'assets' / 'NotoSansSC-Regular.otf'
    if not font_path.exists():
        check('内置字体文件存在', False, str(font_path))
        return

    cmap = TTFont(str(font_path)).getBestCmap()

    # 只查界面文案所在的文件；注释里的花哨符号不影响显示
    offenders = []
    for name in ('main.py', 'elder_theme.py'):
        path = root / name
        if not path.exists():
            continue
        for lineno, line in enumerate(
                path.read_text(encoding='utf-8').splitlines(), 1):
            bad = sorted({c for c in line
                          if ord(c) > 0x7F and ord(c) not in cmap})
            if bad:
                offenders.append('%s:%d  %s' % (
                    name, lineno, ' '.join('U+%04X' % ord(c) for c in bad)))

    check('界面文案没有字体里缺的字', not offenders,
          '；'.join(offenders[:5]))


def main_test():
    tmp = Path(tempfile.mkdtemp(prefix='elder_smoke_'))
    try:
        test_font_glyphs()
        root = build_fake_library(tmp / '老人视频')
        test_core(root)
        test_ui(root)
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    print('\n' + '=' * 56)
    print('通过 %d 项，失败 %d 项' % (len(PASSED), len(FAILED)))
    for name, detail in FAILED:
        print('  ✗ %s\n     %s' % (name, detail))
    print('=' * 56)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main_test())
