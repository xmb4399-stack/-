[app]
# ---------------------------------------------------------------------------
#  老年视频播放器 —— buildozer 打包配置
#
#  用途：把这个目录打包成一个安卓 APK。
#  本机（Windows）没法直接编译，需要用 Linux 环境。最省事的办法是
#  推送到 GitHub，让 .github/workflows/build-apk.yml 在云端编译。
# ---------------------------------------------------------------------------

title = 老年视频播放器
package.name = eldervideo
package.domain = org.elder

source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,atlas,otf,ttf,json,txt
source.exclude_dirs = userdata,.github,bin,.buildozer,__pycache__,tools
source.exclude_patterns = *.pyc,*.pyo,*.zip,README.md,.gitignore

version = 1.0.0

# kivy 是界面框架，pyjnius 让 Python 能调安卓 Java API，
# android 提供权限申请、屏幕旋转这些桥接能力。
# pillow 用来把用户上传的主页图标缩小（手机照片动辄 4000x3000，
# 不缩直接当纹理用要占几十 MB 显存）。
requirements = python3,kivy,pyjnius,android,pillow

# 界面尺寸按屏幕自适应，不做缩放
android.entrypoint = org.kivy.android.PythonActivity

# 方向必须是 all —— 用户在「设置 → 打开时的屏幕方向」里可以选横屏还是竖屏，
# 这里要是写死 landscape，系统层面就锁死了，设置里的竖屏不会生效。
#
# 默认仍然是横屏，由 main.py 启动时按设置调用 setRequestedOrientation 钉住。
# 代价：启动那一帧的方向跟着手机当时的姿态走，随后再转到设置的默认值。
orientation = all
fullscreen = 0

# 播放时屏幕不熄灭（原版没有这个，手机上不加上视频看一半就黑屏了）
android.wakelock = True

# ---------------------------------------------------------------------------
#  权限说明
#    READ_MEDIA_VIDEO / READ_MEDIA_AUDIO  Android 13+ 读媒体文件的正规权限
#    READ_EXTERNAL_STORAGE                Android 12 及以下用这个
#    MANAGE_EXTERNAL_STORAGE              「所有文件访问权限」，Android 11+
#                                         靠它才能自己建文件夹、才能读到
#                                         RMVB 这类没被系统媒体库收录的文件。
#                                         这个权限需要用户在系统设置里手动打开，
#                                         应用内会引导（见 HelpScreen）。
# ---------------------------------------------------------------------------
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,READ_MEDIA_VIDEO,READ_MEDIA_AUDIO,MANAGE_EXTERNAL_STORAGE,WAKE_LOCK

# ---------------------------------------------------------------------------
#  libVLC 播放内核 —— 让 App 能放 RMVB / RM / WMV / AVI / FLV 这些老格式
#
#  安卓系统播放器解不了这些格式（这是系统限制），MX Player、VLC 能放是因为
#  它们自带了一套 FFmpeg 解码器。这里把 VLC 的内核接进来，原理一样。
#
#  代价就是体积：这一行会让 APK 从十几 MB 涨到 40～60 MB（全都是解码器）。
#  如果不想用，把下面这行删掉、重新编译即可 —— elder_vlc.py 探测不到
#  libVLC 时会自动回退到系统播放器，App 照常能用，只是老格式放不了了。
#
#  许可：libVLC 是 LGPLv2.1+，动态链接使用是允许的，不要求本应用开源。
# ---------------------------------------------------------------------------
android.gradle_dependencies = org.videolan.android:libvlc-all:3.6.0

# libvlc-all 这一个包里塞了 4 种 CPU 架构的解码库，实测解压后大小：
#     x86_64        49.5 MB        arm64-v8a     42.2 MB
#     x86           47.3 MB        armeabi-v7a   38.7 MB
# 每个压缩后都还有 19MB 左右。全塞进 APK 的话，安装包会从十几 MB
# 直接涨到 90 MB 以上 —— 而其中两个（x86 系列）只有电脑上的安卓模拟器
# 用得上，真机永远跑不到。
#
# 下面这行只保留 arm64-v8a 的解码库，对应上面的 android.archs 设置。
# x86 系列手机用不上；armeabi-v7a 是 32 位老机器的，archs 里既然没编
# 这个架构，留着也是白占体积。
#
# ⚠️ 三个必须遵守的写法（2026-11-09 实测踩过，改之前先读 tools/trim_libvlc_abis.py 开头）：
#   1. 值里**不能带外层双引号**。buildozer 的 getlist() 只按逗号切、不去引号，
#      带引号的话插进 build.gradle 的是一句裸字符串，Gradle 静默忽略 → 一个 so 都排不掉。
#   2. 必须写 `jniLibs { excludes += ... }`。p4a 用的 AGP 8.11 里，
#      老的 `exclude 'lib/x86/*.so'` 只当资源排除，对 jniLibs 无效。
#   3. **不能有逗号**（getlist 按逗号切，一个逗号断成两条），所以一条只排一个架构。
#
# ⚠️ 改了 android.archs 就要同步改这一行，否则应用会因为缺少对应架构
#    的解码库而打不开。用 GitHub Actions 编译的话不用手动改 ——
#    工作流会先跑 tools/trim_libvlc_abis.py 按 archs 自动生成这一行。
android.add_packaging_options = jniLibs { excludes += 'lib/armeabi-v7a/**' }, jniLibs { excludes += 'lib/x86/**' }, jniLibs { excludes += 'lib/x86_64/**' }

# targetSdk 33 才有 READ_MEDIA_VIDEO；minapi 24 覆盖 Android 7.0 以上
android.api = 33
android.minapi = 24
android.ndk_api = 24

# 默认只打 arm64-v8a：2016 年之后的手机基本都是这个架构，编译也快，
# 而且 libVLC 的解码库很大，少一个架构能省几十 MB。
# 如果老人的手机比较老（32 位），改成：arm64-v8a,armeabi-v7a
android.archs = arm64-v8a

# 图标和启动图
icon.filename = %(source.dir)s/assets/icon.png
presplash.filename = %(source.dir)s/assets/presplash.png
android.presplash_color = #e8f0fe
android.presplash_l10n = False

# 允许把应用装到 SD 卡
android.allow_backup = True

# 调试用：把 print 输出写进 logcat
android.logcat_filters = *:S python:D

[buildozer]
log_level = 2
warn_on_root = 1
