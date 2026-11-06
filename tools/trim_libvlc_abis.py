#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trim_libvlc_abis.py —— 按目标 CPU 架构，把 libVLC 里用不到的解码库剔出去

【为什么需要这一步】
libvlc-all 这一个 AAR 里塞了 4 种 CPU 架构的解码库，实测解压后大小：

    x86_64        49.5 MB        arm64-v8a     42.2 MB
    x86           47.3 MB        armeabi-v7a   38.7 MB

每个压缩后仍然有 19MB 左右。全打进 APK 的话，安装包会从十几 MB
直接涨到 90 MB 以上 —— 而其中两个（x86 系列）只有电脑上的安卓模拟器
用得上，真机永远跑不到。

【它做什么】
读 buildozer.spec 里的 android.archs，把不在里面的架构全部写进
android.add_packaging_options 的排除列表。这样 APK 里只会留下
目标架构的解码库。

举个例子，android.archs = arm64-v8a 时，它会写成：

    android.add_packaging_options = "exclude 'lib/x86/*.so'", \
"exclude 'lib/x86_64/*.so'", "exclude 'lib/armeabi-v7a/*.so'"

【什么时候跑】
GitHub Actions 在编译前会自动跑一次（见 .github/workflows/build-apk.yml）。
用 Actions 的话不用管它。

本地手动编译的话，buildozer.spec 里已经按默认的 arm64-v8a 配好了；
要是你改了 android.archs，先跑一下这个脚本再编译。

运行：python tools/trim_libvlc_abis.py
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE.parent / 'buildozer.spec'

# p4a / buildozer 认得的所有安卓 CPU 架构
ALL_ABIS = ['arm64-v8a', 'armeabi-v7a', 'x86', 'x86_64']

# 每个架构的解码库压缩后大概多大，只用来打印提示
APPROX_MB = {'arm64-v8a': 18.9, 'armeabi-v7a': 18.7,
             'x86': 19.9, 'x86_64': 20.6}

LINE_RE = re.compile(r'^(?:#\s*)?android\.add_packaging_options\s*=.*$', re.M)
ARCH_RE = re.compile(r'^android\.archs\s*=\s*(.+)$', re.M)


def main():
    if not SPEC.exists():
        print('找不到 buildozer.spec：%s' % SPEC)
        return 2

    text = SPEC.read_text(encoding='utf-8')

    m = ARCH_RE.search(text)
    if not m:
        print('buildozer.spec 里没有找到 android.archs')
        return 2

    archs = [a.strip() for a in m.group(1).split(',') if a.strip()]
    unknown = [a for a in archs if a not in ALL_ABIS]
    if unknown:
        print('android.archs 里有不认识的架构：%s' % unknown)
        print('认得的只有：%s' % ALL_ABIS)
        return 2

    drop = [a for a in ALL_ABIS if a not in archs]
    saved = sum(APPROX_MB.get(a, 0) for a in drop)

    print('目标架构      %s' % ', '.join(archs))
    if drop:
        print('要排除的架构  %s（大约省下 %.0f MB）'
              % (', '.join(drop), saved))
    else:
        print('要排除的架构  没有 —— 四种架构都要，APK 会很大')

    if drop:
        items = ', '.join('"exclude \'lib/%s/*.so\'"' % a for a in drop)
        line = 'android.add_packaging_options = %s' % items
    else:
        line = '# android.add_packaging_options = （所有架构都要，不需要排除）'

    if LINE_RE.search(text):
        text = LINE_RE.sub(lambda _m: line, text, count=1)
    else:
        text = text.rstrip('\n') + '\n' + line + '\n'

    SPEC.write_text(text, encoding='utf-8')

    print()
    print('已写入 buildozer.spec：')
    print('  %s' % line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
