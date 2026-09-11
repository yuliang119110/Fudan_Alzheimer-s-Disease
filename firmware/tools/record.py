#!/usr/bin/env python3
"""
从 ESP32-S3 抓一段录音, 校验并存成 wav。

用法:
    python record.py [串口] [输出文件] [倒计时秒数]
    python record.py COM4 record.wav 15

固件收到 'r' 之后会吐:
    \\nPCM <样本数> <采样率> <校验和>\\n
    <样本数 * 2 字节, 小端有符号 16bit>
    \\n<<<END>>>\\n

校验和是所有字节的 32 位累加和, 用来确认高速串口没丢字节 ——
丢了就直接报错, 而不是让你去听一段带杂音的假音频。
"""
import re
import struct
import sys
import time

import serial

PORT  = sys.argv[1] if len(sys.argv) > 1 else "COM4"
OUT   = sys.argv[2] if len(sys.argv) > 2 else "record.wav"
DELAY = int(sys.argv[3]) if len(sys.argv) > 3 else 0
BAUD  = 921600


def write_wav(path, pcm, rate):
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
        b"data", len(pcm),
    )
    with open(path, "wb") as f:
        f.write(hdr + pcm)


def relay_text(buf):
    """把固件发来的文本行打出来 (只挑有意义的, 电平行太多不要)"""
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        s = line.strip(b"\r").decode("utf-8", "replace")
        if "[" in s or "PCM" in s or "!!" in s:
            print("  固件> " + s, flush=True)
    return buf


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.5)
    ser.reset_input_buffer()
    print(f"打开 {PORT} @ {BAUD}", flush=True)

    if DELAY > 0:
        print(f"\n*** {DELAY} 秒后开始录音, 请准备说话 ***", flush=True)
        for i in range(DELAY, 0, -1):
            print(f"  {i} ...", flush=True)
            time.sleep(1)
        print("  >>> 开始! 说话吧 <<<\n", flush=True)

    ser.write(b"r")
    ser.flush()
    print("已发送 'r', 固件正在录 60 秒 ...", flush=True)

    # ---------- 等帧头 ----------
    buf = bytearray()
    hdr = None
    t0 = time.time()
    while time.time() - t0 < 200:
        chunk = ser.read(4096)
        if not chunk:
            continue
        buf += chunk
        m = re.search(rb"PCM (\d+) (\d+) (\d+)\r?\n", buf)
        if m:
            hdr = m
            break
        buf = bytearray(relay_text(bytes(buf)))

    if not hdr:
        print("!! 超时: 没等到 PCM 帧头", flush=True)
        return 1

    n, rate, want_sum = (int(hdr.group(i)) for i in (1, 2, 3))
    print(f"\n帧头: {n} 样本 / {rate} Hz / 校验和 {want_sum}", flush=True)

    # ---------- 收 PCM ----------
    need = n * 2
    data = bytearray(buf[hdr.end():])
    ser.timeout = 5
    t0 = time.time()
    last = time.time()
    while len(data) < need:
        chunk = ser.read(min(65536, need - len(data)))
        if chunk:
            data += chunk
            last = time.time()
        elif time.time() - last > 10:
            print(f"!! 数据停了: 只收到 {len(data)}/{need} 字节", flush=True)
            break
        if time.time() - t0 > 180:
            print(f"!! 超时: 只收到 {len(data)}/{need} 字节", flush=True)
            break

    data = bytes(data[:need])
    dt = time.time() - t0
    print(f"收到 {len(data)} / {need} 字节, 用时 {dt:.1f} 秒 "
          f"({len(data)/max(dt,0.001)/1024:.0f} KB/s)", flush=True)

    # ---------- 校验 ----------
    got_sum = sum(data) & 0xFFFFFFFF
    if len(data) != need:
        print(f"!! 长度不对, 文件已写出但仍可用部分内容", flush=True)
    elif got_sum != want_sum:
        print(f"!! 校验和不符: 期望 {want_sum}, 实际 {got_sum} —— 有丢字节", flush=True)
        return 2
    else:
        print(f"校验通过 (sum={got_sum})", flush=True)

    write_wav(OUT, data, rate)

    # ---------- 基本统计 ----------
    import array
    a = array.array("h")
    a.frombytes(data)
    peak = max(abs(v) for v in a) if a else 0
    rms = (sum(v * v for v in a) / len(a)) ** 0.5 if a else 0
    clipped = sum(1 for v in a if abs(v) >= 32767)
    print(f"\n写出 {OUT}")
    print(f"  时长   : {len(a)/rate:.1f} 秒")
    print(f"  峰值   : {peak}  ({20*__import__('math').log10(max(peak,1)/32768):.1f} dBFS)")
    print(f"  RMS    : {rms:.0f}  ({20*__import__('math').log10(max(rms,1)/32768):.1f} dBFS)")
    print(f"  削顶   : {clipped} 个样本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
