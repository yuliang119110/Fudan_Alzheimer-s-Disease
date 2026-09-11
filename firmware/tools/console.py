#!/usr/bin/env python3
"""
串口控制台 —— 复位板子, 等启动横幅, 可选倒计时后发一个按键, 然后把固件
输出同时打到屏幕和 UTF-8 文本文件。

为什么要写文件: Windows 控制台是 GBK, 直接看原始字节会乱码。这里先在
Python 里按 UTF-8 正确解码成字符串, 再交给控制台按它自己的编码输出,
中文就正常了。文件那份永远是干净的 UTF-8, 方便事后翻。

用法:
    python console.py [串口] [按键] [观察秒数] [发送前倒计时秒数] [日志文件]
    python console.py COM10 m 22 5
    python console.py COM10 "" 8            # 不发按键, 只复位看启动
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM10"
KEY = sys.argv[2] if len(sys.argv) > 2 else ""
SECS = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
WAIT = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
LOGFILE = sys.argv[5] if len(sys.argv) > 5 else r"v:\pyproj\aiwatch\console.log"
BAUD = 921600

# 控制台是 GBK, 遇到它编不出来的字符就替换掉而不是崩
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

log = open(LOGFILE, "w", encoding="utf-8")


def out(raw):
    s = raw.decode("utf-8", "replace")
    sys.stdout.write(s)
    sys.stdout.flush()
    log.write(s)
    log.flush()


ser = serial.Serial(PORT, BAUD, timeout=0.2)
print(f"打开 {PORT} @ {BAUD}", flush=True)

# 用 DTR/RTS 复位, 这样能完整抓到启动横幅。
# DTR=False -> GPIO0 保持高 -> 正常启动 (不是下载模式)
# RTS 脉冲   -> EN 拉低再拉高 -> 复位
ser.dtr = False
ser.rts = True
time.sleep(0.15)
ser.rts = False
time.sleep(0.1)

t0 = time.time()
while time.time() - t0 < 3.0:
    c = ser.read(4096)
    if c:
        out(c)

if not KEY:
    print(f"\n>>> 未发送按键, 继续观察 {SECS:.0f} 秒\n", flush=True)
else:
    if WAIT > 0:
        print(f"\n>>> {WAIT:.0f} 秒后发送 {KEY!r}, 请准备\n", flush=True)
        for i in range(int(WAIT), 0, -1):
            print(f"  {i} ...", flush=True)
            time.sleep(1)
    ser.write(KEY.encode())
    ser.flush()
    print(f"\n>>> 已发送 {KEY!r}, 观察 {SECS:.0f} 秒\n", flush=True)

t0 = time.time()
while time.time() - t0 < SECS:
    c = ser.read(4096)
    if c:
        out(c)

ser.close()
log.close()
print(f"\n\n>>> 日志已写入 {LOGFILE}", flush=True)
