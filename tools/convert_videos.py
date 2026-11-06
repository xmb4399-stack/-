#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert_videos.py —— 把手机放不了的老格式视频，批量转成 H.264 MP4

【解决什么问题】
.rmvb / .rm / .flv / .wmv / .avi / .mpg 这些老格式，手机的**系统播放器**解不了。
而 Kivy 在安卓上用的正是系统播放器，所以这些文件在 App 里点开会提示放不了。
转成 H.264 MP4 之后全部能正常播放，而且体积通常还更小。

【一个能省大量时间的优化】
很多 .flv 和一部分 .avi 里面装的**本来就是 H.264**，只是套了个老外壳。
这种文件不需要重新编码，只要换个封装容器就行 —— 几秒钟就好，画质零损失。
真正需要重编码的只有编码本身是老的（RMVB 的 RV40、WMV 的 WMV3/VC-1、
AVI 里的 DivX/Xvid 等）。脚本会自动分辨这两种情况。

【怎么用】
    # 先看一眼都有什么、要不要转（不写任何文件）
    python tools/convert_videos.py --src "D:/老人的视频" --dry-run

    # 正式转换（默认不删原文件）
    python tools/convert_videos.py --src "D:/老人的视频"

    # 转好的放在哪
    #   默认：源文件夹同级的「<源文件夹名>_手机版」
    #   也可以自己指定：--dst "E:/转好的"

【依赖】
需要 ffmpeg。按下面顺序自动找：
    1. 环境变量 FFMPEG_BINARY 指定的路径
    2. 系统 PATH 里的 ffmpeg
    3. Python 包 imageio-ffmpeg 自带的便携版
如果三个都没有，执行：pip install imageio-ffmpeg
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 输出被重定向到文件时不要刷动态进度行（会变成一堆回车符）
TTY = sys.stdout.isatty()

# ---------------------------------------------------------------- 格式清单
# 和 App（elder_core.py）里的判断保持一致，避免两边说法不一样

# 手机上一般放不了的老格式，默认只处理这些
OLD_EXT = {'.rmvb', '.rm', '.flv', '.wmv', '.avi', '.mpg', '.mpeg',
           '.vob', '.f4v', '.m2ts', '.mts', '.asf', '.divx', '.ogm'}

# 其余会被当成视频的后缀（--deep 时才一起检查）
OTHER_VIDEO_EXT = {'.mp4', '.m4v', '.mkv', '.mov', '.webm', '.3gp',
                   '.ts', '.mpv', '.m2v'}

ALL_VIDEO_EXT = OLD_EXT | OTHER_VIDEO_EXT

# 安卓系统播放器能稳定处理的编码。注意 HEVC 虽然新手机支持，
# 但老人的机器往往偏低端，统一转成 H.264 最保险。
GOOD_VIDEO_CODECS = {'h264'}
GOOD_AUDIO_CODECS = {'aac'}          # mp4 里唯一稳妥的音频编码
COPYABLE_AUDIO = {'aac'}             # 能原样搬进 mp4 的音频

# 安卓硬解要的像素格式。这一个条件经常被忽略：
# H.264 也可能是 yuv444p / yuv422p / yuv420p10le（10bit），
# 这些在电脑上能放，在手机的硬解器上直接不出画面。
# 只有 8bit 的 4:2:0 才是所有安卓设备都认的。
SAFE_PIX_FMT = {'yuv420p', 'yuvj420p'}

TARGET_FPS = 30                      # 超过这个帧率的往下压，老人机不吃高帧率


# ---------------------------------------------------------------- 工具函数
def human_size(num):
    """把字节数变成好读的大小。"""
    num = float(num)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if num < 1024 or unit == 'GB':
            return '%.1f %s' % (num, unit)
        num /= 1024


def human_time(sec):
    """把秒数变成 1小时23分 这种形式。"""
    sec = int(sec)
    if sec < 60:
        return '%d 秒' % sec
    if sec < 3600:
        return '%d 分 %d 秒' % (sec // 60, sec % 60)
    return '%d 小时 %d 分' % (sec // 3600, (sec % 3600) // 60)


def find_ffmpeg():
    """
    按三条路找 ffmpeg，找到就返回可执行文件的路径。

    返回值: (路径, 说明) —— 说明是给用户看的，告诉他这个 ffmpeg 从哪来的
    """
    # 1. 用户自己指定
    env_path = os.environ.get('FFMPEG_BINARY')
    if env_path and Path(env_path).exists():
        return env_path, '环境变量 FFMPEG_BINARY'

    # 2. 系统 PATH
    found = shutil.which('ffmpeg')
    if found:
        return found, '系统 PATH'

    # 3. imageio-ffmpeg 自带的便携版
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe, 'imageio-ffmpeg 便携版'
    except Exception:
        pass

    return None, None


# ---------------------------------------------------------------- 探测
_DUR_RE = re.compile(r'Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)')
_VCODEC_RE = re.compile(r'Stream #\d+:\d+.*?: Video:\s*([A-Za-z0-9_\-]+)')
_ACODEC_RE = re.compile(r'Stream #\d+:\d+.*?: Audio:\s*([A-Za-z0-9_\-]+)')
_VDIM_RE = re.compile(r'Video:.*?,\s*([A-Za-z0-9_]+)(?:\([^)]*\))?,\s*(\d{2,5})x(\d{2,5})')


def probe(ffmpeg, path):
    """
    看一眼这个文件里到底是什么编码、多长、多大。

    用 ffmpeg 自己来探（不依赖 ffprobe，因为便携版通常只带 ffmpeg）。
    """
    info = {'ok': False, 'duration': 0.0, 'vcodec': '', 'acodec': '',
            'pix_fmt': '', 'width': 0, 'height': 0}
    try:
        proc = subprocess.run(
            [ffmpeg, '-hide_banner', '-nostdin', '-i', str(path)],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=60)
    except Exception:
        return info

    # ffmpeg 把文件信息打在 stderr 上，然后因为没有指定输出而报错退出，
    # 这是正常行为，我们只要它打印的那段信息
    text = (proc.stderr or '') + (proc.stdout or '')

    m = _DUR_RE.search(text)
    if m:
        info['duration'] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                            + float(m.group(3)))

    m = _VCODEC_RE.search(text)
    if m:
        info['vcodec'] = m.group(1).lower()

    m = _ACODEC_RE.search(text)
    if m:
        info['acodec'] = m.group(1).lower()

    m = _VDIM_RE.search(text)
    if m:
        info['pix_fmt'] = m.group(1).lower()
        info['width'] = int(m.group(2))
        info['height'] = int(m.group(3))

    # 有时长说明文件能读到，算探测成功
    info['ok'] = bool(info['duration'] > 0)
    return info


def decide(info, ext):
    """
    决定这个文件该怎么处理。

    返回 (动作, 说明)：
        'skip'   已经能播，不用动
        'remux'  编码没问题，只要换个封装，几秒钟
        'encode' 编码是老的，必须重新编码
        'bad'    文件读不出来，跳过
    """
    if not info['ok']:
        return 'bad', '读不出时长，文件可能损坏'

    vcodec = info['vcodec']
    acodec = info['acodec']

    if not vcodec:
        return 'bad', '没有视频轨'

    ext_safe = ext not in OLD_EXT

    if vcodec in GOOD_VIDEO_CODECS:
        # 先看像素格式。H.264 也可能是 yuv444p / 10bit，
        # 电脑能放但手机硬解器不出画面，这种必须重编码，不能图省事直接搬
        pix = info.get('pix_fmt') or ''
        if pix and pix not in SAFE_PIX_FMT:
            return 'encode', ('视频虽是 H.264，但像素格式是 %s，'
                              '手机硬解不支持，需要重新编码' % pix)

        audio_ok = acodec in COPYABLE_AUDIO
        if ext_safe and audio_ok:
            return 'skip', '已经是 H.264 + AAC（%s）' % ext
        if audio_ok:
            return 'remux', '视频是 H.264，只需换成 mp4 外壳'
        if not acodec:
            return 'remux', '视频是 H.264，无音轨，只需换成 mp4 外壳'
        return 'remux', '视频是 H.264，音频 %s 换成 AAC' % acodec

    # 剩下的都得重编码
    return 'encode', '视频编码是 %s，需要重新编码' % (vcodec or '未知')


# ---------------------------------------------------------------- 转码
_TIME_RE = re.compile(r'^out_time=(\d+):(\d\d):(\d\d(?:\.\d+)?)')
_SPEED_RE = re.compile(r'^speed=\s*([\d.]+|N/A)')


def build_cmd(ffmpeg, src, dst, action, acodec, max_height=0):
    """拼出这条 ffmpeg 命令。"""
    cmd = [ffmpeg, '-y', '-hide_banner', '-nostdin', '-loglevel', 'error',
           '-progress', 'pipe:1', '-nostats', '-i', str(src)]

    # 只保留第一条视频轨和第一条音轨，丢掉字幕、章节、数据流。
    # 老格式里常有乱七八糟的附加流，直接塞进 mp4 会失败
    cmd += ['-map', '0:v:0']
    if acodec:
        cmd += ['-map', '0:a:0?']
    cmd += ['-sn', '-dn']

    if action == 'remux':
        cmd += ['-c:v', 'copy']
    else:
        cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-level', '4.0',
                '-r', str(TARGET_FPS)]

    if max_height and action != 'remux':
        # 只缩不放，避免把小文件拉大
        cmd += ['-vf', 'scale=-2:min(%d\\,ih)' % max_height]

    if not acodec:
        cmd += ['-an']
    elif acodec in COPYABLE_AUDIO and action == 'remux':
        cmd += ['-c:a', 'copy']
    else:
        cmd += ['-c:a', 'aac', '-b:a', '128k', '-ac', '2']

    # faststart 把索引挪到文件开头，手机拖动进度条才快
    cmd += ['-movflags', '+faststart', str(dst)]
    return cmd


def run_ffmpeg(cmd, total_sec, on_progress=None):
    """
    跑 ffmpeg 并回报进度。

    返回 (是否成功, 错误信息)
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, encoding='utf-8',
        errors='replace', bufsize=1)

    err_lines = []

    def _drain():
        # 必须有人一直读 stderr，否则缓冲区满了 ffmpeg 会卡死
        try:
            for line in proc.stderr:
                err_lines.append(line)
        except Exception:
            pass

    pump = threading.Thread(target=_drain, daemon=True)
    pump.start()

    try:
        for line in proc.stdout:
            line = line.strip()
            m = _TIME_RE.match(line)
            if m:
                done = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                        + float(m.group(3)))
                if on_progress and total_sec > 0:
                    on_progress(min(1.0, done / total_sec))
                continue
            if line.startswith('speed='):
                m2 = _SPEED_RE.match(line)
                if m2 and on_progress and m2.group(1) != 'N/A':
                    on_progress(None, float(m2.group(1)))
    except Exception:
        pass

    proc.wait()
    pump.join(timeout=2)

    if proc.returncode != 0:
        tail = ''.join(err_lines[-12:]).strip()
        return False, tail or ('ffmpeg 退出码 %d' % proc.returncode)
    return True, ''


# ---------------------------------------------------------------- 主流程
class Task(object):
    def __init__(self, src, dst_rel, action, reason, info):
        self.src = src
        self.dst_rel = dst_rel
        self.action = action
        self.reason = reason
        self.info = info
        self.status = 'pending'
        self.note = ''


def collect(src_root, deep, dst_root):
    """扫描源文件夹，给每个视频文件定好处理方案。"""
    tasks = []
    exts = ALL_VIDEO_EXT if deep else OLD_EXT

    for path in sorted(src_root.rglob('*')):
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue
        # 别把上次转出来的东西又扫进来
        try:
            path.relative_to(dst_root)
            continue
        except ValueError:
            pass
        try:
            rel = path.relative_to(src_root)
        except ValueError:
            continue
        tasks.append(Task(path, rel.with_suffix('.mp4'), '', '', None))
    return tasks


def convert_one(ffmpeg, task, dst_root, args):
    """处理一个文件，返回 (状态, 说明)。"""
    dst = dst_root / task.dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 断点续跑：目标已经转好了就跳过
    if dst.exists() and dst.stat().st_size > 0:
        done_info = probe(ffmpeg, dst)
        if done_info['ok']:
            task.status = 'done'
            return 'done', '上次已经转好，跳过'

    # 先转成临时文件，成功了再改名。
    # 这样中途中断不会留下一个半成品被下次误当成「已完成」
    tmp = dst.with_name(dst.name + '.part.mp4')

    action = task.action
    cmd = build_cmd(ffmpeg, task.src, tmp, action,
                    task.info['acodec'], args.max_height)

    total = task.info['duration']
    started = time.time()
    last_print = [0.0]

    def on_progress(frac, speed=None):
        if args.quiet or args.jobs > 1 or not TTY:
            return
        # 最多每 0.4 秒刷一次，别把终端刷爆
        now = time.time()
        if frac is not None and now - last_print[0] < 0.4 and frac < 1.0:
            return
        last_print[0] = now
        if frac is None:
            return
        bar_len = 24
        filled = int(bar_len * frac)
        bar = '#' * filled + '-' * (bar_len - filled)
        extra = '  %5.1fx' % speed if speed else ''
        sys.stdout.write('\r      [%s] %5.1f%%%s' % (bar, frac * 100, extra))
        sys.stdout.flush()

    ok, err = run_ffmpeg(cmd, total, on_progress)
    elapsed = time.time() - started

    if TTY and not args.quiet and args.jobs <= 1:
        sys.stdout.write('\r' + ' ' * 60 + '\r')

    if not ok:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        task.status = 'failed'
        return 'failed', err

    # 校验产物。不管刚才走的是换封装还是重编码，出来的东西必须真的
    # 是手机上能播的：H.264 + yuv420p + 有音轨的话是 AAC。
    # 换封装那条路万一漏了源文件里的怪东西，也会在这里被拦住，
    # 不会留下一个「看着转成功了、装到手机上却黑屏」的文件。
    out_info = probe(ffmpeg, tmp)
    if not out_info['ok']:
        try:
            tmp.unlink()
        except Exception:
            pass
        task.status = 'failed'
        return 'failed', '转出来的文件读不出时长，已删除'

    problems = []
    if out_info['vcodec'] not in GOOD_VIDEO_CODECS:
        problems.append('视频编码是 %s' % (out_info['vcodec'] or '未知'))
    out_pix = out_info.get('pix_fmt') or ''
    if out_pix and out_pix not in SAFE_PIX_FMT:
        problems.append('像素格式是 %s' % out_pix)
    if out_info['acodec'] and out_info['acodec'] not in GOOD_AUDIO_CODECS:
        problems.append('音频是 %s' % out_info['acodec'])

    if problems:
        try:
            tmp.unlink()
        except Exception:
            pass
        task.status = 'failed'
        return 'failed', '转出来不对劲（%s），已删除' % '、'.join(problems)

    try:
        if dst.exists():
            dst.unlink()
        tmp.rename(dst)
    except Exception as exc:
        task.status = 'failed'
        return 'failed', '改名失败：%s' % exc

    task.status = 'done'
    src_size = task.src.stat().st_size
    dst_size = dst.stat().st_size
    ratio = (dst_size / src_size * 100) if src_size else 0
    return 'done', '用 %s，体积 %s → %s（%.0f%%）' % (
        human_time(elapsed), human_size(src_size), human_size(dst_size), ratio)


def main():
    ap = argparse.ArgumentParser(
        description='把手机放不了的老格式视频，批量转成 H.264 MP4',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('【依赖】')[0])
    ap.add_argument('--src', required=True, help='放视频的文件夹（原文件不动）')
    ap.add_argument('--dst', default=None,
                    help='转好的放哪里。默认：源文件夹同级的「<源文件夹名>_手机版」')
    ap.add_argument('--deep', action='store_true',
                    help='连 .mp4/.mkv 也一起检查（适合怀疑有些 mp4 里装的是老编码）')
    ap.add_argument('--dry-run', action='store_true',
                    help='只列出计划和判断依据，不真的转换')
    ap.add_argument('--max-height', type=int, default=0,
                    help='限制最高分辨率，例如 720。默认不限制')
    ap.add_argument('--jobs', type=int, default=1,
                    help='同时转几个。默认 1 个（进度看得清，也不抢电脑）')
    ap.add_argument('--quiet', action='store_true', help='少打点字')
    args = ap.parse_args()

    src_root = Path(args.src).expanduser().resolve()
    if not src_root.is_dir():
        print('找不到这个文件夹：%s' % src_root)
        return 2

    if args.dst:
        dst_root = Path(args.dst).expanduser().resolve()
    else:
        dst_root = src_root.parent / (src_root.name + '_手机版')

    print('=' * 66)
    print('源文件夹  %s' % src_root)
    print('输出到    %s' % dst_root)
    print('=' * 66)

    ffmpeg, origin = find_ffmpeg()
    if not ffmpeg:
        print()
        print('没找到 ffmpeg。三个办法任选一个：')
        print('  1. pip install imageio-ffmpeg    （最省事，推荐）')
        print('  2. 装一个 ffmpeg 并加进系统 PATH')
        print('  3. 设环境变量 FFMPEG_BINARY 指向 ffmpeg.exe')
        return 2

    print('ffmpeg    %s' % ffmpeg)
    print('来源      %s' % origin)
    print()

    print('正在扫描…')
    tasks = collect(src_root, args.deep, dst_root)
    if not tasks:
        print('没找到需要处理的视频文件。')
        print('（默认只看老格式。如果怀疑 .mp4 里也装了老编码，加 --deep）')
        return 0

    print('找到 %d 个视频文件，正在看里面是什么编码…' % len(tasks))
    print()

    # 探测阶段：一个个看编码，决定怎么处理
    for i, task in enumerate(tasks, 1):
        if TTY:
            sys.stdout.write('\r  探测 %d/%d  %s'
                             % (i, len(tasks), task.src.name[:40]))
            sys.stdout.flush()
        task.info = probe(ffmpeg, task.src)
        task.action, task.reason = decide(task.info, task.src.suffix.lower())

        # 上次已经转好的就别再走一遍了。这里只看文件在不在，
        # 内容对不对留到转换阶段再确认一次
        if task.action in ('remux', 'encode'):
            candidate = dst_root / task.dst_rel
            try:
                if candidate.exists() and candidate.stat().st_size > 0:
                    task.action = 'done'
                    task.reason = '上次已经转好，跳过'
            except OSError:
                pass
    if TTY:
        sys.stdout.write('\r' + ' ' * 70 + '\r')

    groups = {'skip': [], 'done': [], 'remux': [], 'encode': [], 'bad': []}
    for task in tasks:
        groups[task.action].append(task)

    for key, title in (('skip', '不用动（本来就能播）'),
                       ('done', '上次已经转好（跳过）'),
                       ('remux', '只需换封装（几秒钟，画质不变）'),
                       ('encode', '需要重新编码（比较慢）'),
                       ('bad', '读不了（跳过）')):
        items = groups[key]
        if not items:
            continue
        print('【%s】共 %d 个' % (title, len(items)))
        for task in items[:40]:
            info = task.info or {}
            dim = '%dx%d' % (info.get('width', 0), info.get('height', 0)) \
                if info.get('width') else ''
            print('   %-42s %-10s %s' % (str(task.src.name)[:42],
                                         info.get('vcodec', ''), dim))
            print('      %s' % task.reason)
        if len(items) > 40:
            print('   … 还有 %d 个，就不全列了' % (len(items) - 40))
        print()

    todo = groups['remux'] + groups['encode']
    if groups['bad']:
        print('有 %d 个文件读不了，这些帮不上忙，建议你在电脑上先试着播一下，'
              '确认文件本身没坏。' % len(groups['bad']))
        print()

    if not todo:
        if groups['done']:
            print('上面这些上次都已经转好了，不用重来。')
        else:
            print('没有需要处理的文件，收工。')
        return 0

    if args.dry_run:
        print('（这是 --dry-run，什么都没写）')
        print('去掉 --dry-run 就会开始转换。')
        return 0

    # 估算耗时：换封装大约 10 秒，重编码大约是时长的 0.5~1.5 倍
    est = len(groups['remux']) * 10 + int(
        sum(t.info['duration'] for t in groups['encode']) * 1.0)
    print('准备处理 %d 个文件（%d 个换封装 + %d 个重编码），'
          '大概要 %s。' % (len(todo), len(groups['remux']),
                          len(groups['encode']), human_time(est)))
    print('可以随时按 Ctrl+C 中断，下次再跑会自动接着来。')
    print()

    started = time.time()
    done = failed = 0

    try:
        if args.jobs > 1:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                futures = {}
                for task in todo:
                    print('开始 %s' % task.src.name)
                    futures[pool.submit(convert_one, ffmpeg, task,
                                        dst_root, args)] = task
                for fut in as_completed(futures):
                    task = futures[fut]
                    try:
                        status, note = fut.result()
                    except Exception as exc:
                        status, note = 'failed', str(exc)
                    if status == 'done':
                        done += 1
                        print('[好] %s  %s' % (task.src.name, note))
                    else:
                        failed += 1
                        print('[失败] %s' % task.src.name)
                        print('       %s' % note.splitlines()[-1] if note else '')
        else:
            for i, task in enumerate(todo, 1):
                kind = '换封装' if task.action == 'remux' else '重编码'
                print('[%d/%d] %s（%s）' % (i, len(todo), task.src.name, kind))
                status, note = convert_one(ffmpeg, task, dst_root, args)
                if status == 'done':
                    done += 1
                    print('       好：%s' % note)
                else:
                    failed += 1
                    print('       失败：%s' % (note.splitlines()[-1]
                                              if note else '未知错误'))
    except KeyboardInterrupt:
        print()
        print('中断了。已经转好的都在，下次跑同一个命令会自动接着来。')

    elapsed = time.time() - started
    print()
    print('=' * 66)
    print('转换完成：成功 %d 个，失败 %d 个，用时 %s' % (done, failed,
                                                       human_time(elapsed)))
    print('''
接下来：

  1. 打开  %s
  2. 把它里面的文件夹（黄梅戏 / 电视剧 …）整个拷到手机的
     存储/Movies/老人视频/ 下面
  3. 原文件不用管，留着也没事，确认能播之后再删

如果嫌视频太大，可以加 --max-height 720 重跑一次，
转换会把画面压到 720p，手机上看着一样清楚，体积小很多。''' % dst_root)
    print('=' * 66)

    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
