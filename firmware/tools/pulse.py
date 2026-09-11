#!/usr/bin/env python3
"""
MAX30102 实时脉搏观察器

固件连续 60 秒往串口吐 CSV:
    D,<red>,<ir>,<ac>,<bpm>,<finger>,<beat>,<temp_mC>
        bpm     = -1 表示还没测出来
        temp_mC = 毫摄氏度整数, -100000 表示 AS6221 不在线
    D,BEGIN / D,END                            起止标记

这个脚本一边读串口, 一边在本地起一个小 HTTP 服务。浏览器打开
    http://localhost:8765
就能看到实时波形、心率数字和手指状态 —— 不用装东西, 也不用刷新页面。

固件一轮只跑 60 秒 (MAX_SECONDS), 脚本会在每轮 D,END 之后自动再叫一轮,
所以页面会一直刷新 —— 不用掐着表试。Ctrl+C 退出。

用法:
    python pulse.py [串口] [总秒数]
    python pulse.py COM10          # 一直循环, 直到 Ctrl+C
    python pulse.py COM10 120      # 跑满 120 秒就停
"""
import collections
import http.server
import json
import os
import sys
import threading
import time

import serial

PORT      = sys.argv[1] if len(sys.argv) > 1 else "COM10"
SECS      = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0   # 0 = 一直循环
BAUD      = 921600
HTTP_PORT = 8765
HERE      = os.path.dirname(os.path.abspath(__file__))

LOOP = (SECS <= 0)

# 每项: (red, ir, ac, bpm, finger, beat, temp)   temp 为 None 表示读不到
samples = collections.deque(maxlen=2000)
lock    = threading.Lock()
state   = {"bpm": -1, "finger": 0, "red": 0, "ir": 0, "ac": 0,
           "temp": None, "count": 0, "done": False, "error": ""}

with open(os.path.join(HERE, "pulse.html"), "rb") as f:
    HTML = f.read()


def reader():
    """后台线程: 读串口, 往 samples 里塞"""
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.2)
    except Exception as e:
        with lock:
            state["error"] = f"打不开 {PORT}: {e}"
            state["done"] = True
        return

    # DTR/RTS 复位, 这样能抓到完整启动过程
    ser.dtr = False
    ser.rts = True
    time.sleep(0.15)
    ser.rts = False
    t = time.time()
    while time.time() - t < 3.0:
        ser.readline()

    ser.write(b"m")
    ser.flush()
    print("已发送 'm', 固件开始连续采集", flush=True)

    buf  = b""
    stop = (time.time() + SECS) if SECS > 0 else None

    while stop is None or time.time() < stop:
        try:
            buf += ser.read(4096)
        except Exception as e:
            with lock:
                state["error"] = f"串口读失败: {e}"
            break

        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("D,"):
                continue
            f = line.split(",")

            if f[1] == "BEGIN":
                with lock:
                    samples.clear()
                    state.update(bpm=-1, finger=0, red=0, ir=0, ac=0, count=0, done=False)
                continue

            if f[1] == "END":
                with lock:
                    state["done"] = True
                if LOOP:
                    time.sleep(0.4)            # 等固件把收尾那几行文本吐完
                    try:
                        ser.reset_input_buffer()
                        ser.write(b"m")
                        ser.flush()
                    except Exception as e:
                        with lock:
                            state["error"] = f"重启下一轮失败: {e}"
                        break
                    print("一轮结束, 自动开始下一轮", flush=True)
                    with lock:
                        samples.clear()
                        state.update(bpm=-1, finger=0, count=0, done=False)
                continue

            try:
                red, ir, ac, bpm, finger, beat, tmilli = (int(v) for v in f[1:8])
            except (ValueError, IndexError):
                continue
            temp = None if tmilli <= -100000 else tmilli / 1000.0

            with lock:
                samples.append((red, ir, ac, bpm, finger, beat, temp))
                state.update(red=red, ir=ir, ac=ac, finger=finger,
                             temp=temp, count=len(samples))
                if bpm > 0:
                    state["bpm"] = bpm
                elif not finger:
                    state["bpm"] = -1

    ser.close()
    with lock:
        state["done"] = True
    print("采集线程结束", flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                  # 别让每条请求刷屏

    def do_GET(self):
        if self.path.startswith("/data"):
            with lock:
                tail  = list(samples)[-400:]
                body  = json.dumps({
                    "bpm":    state["bpm"],
                    "finger": state["finger"],
                    "red":    state["red"],
                    "ir":     state["ir"],
                    "count":  state["count"],
                    "done":   state["done"],
                    "error":  state["error"],
                    "ac":     [s[2] for s in tail],
                    "beats":  [s[5] for s in tail],
                    # "temp" 是当前读数 (标量), "tempSeries" 才是趋势图要的数组。
                    # 这俩曾经都叫 temp, 页面拿数组当标量 .toFixed() 直接抛异常,
                    # 把整段 tick() 带崩了 —— 名字分开, 别再合回去。
                    "temp":       state["temp"],
                    "tempSeries": [s[6] for s in tail],
                }).encode()
            ctype = "application/json"
        else:
            body  = HTML
            ctype = "text/html; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    threading.Thread(target=reader, daemon=True).start()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    print(f"\n  >>> 浏览器打开   http://localhost:{HTTP_PORT}\n", flush=True)

    # 每 5 秒在终端也报一下, 方便没开浏览器时看
    def report():
        while True:
            time.sleep(5)
            with lock:
                if state["error"]:
                    print(f"  !! {state['error']}", flush=True)
                    return
                t = state["temp"]
                print(f"  {state['count']:5d} 样本   "
                      f"BPM={state['bpm'] if state['bpm'] > 0 else '--':>3}   "
                      f"体温={f'{t:.2f}C' if t is not None else '--':>7}   "
                      f"手指={'有' if state['finger'] else '无'}   "
                      f"IR={state['ir']}   AC={state['ac']:+d}", flush=True)
    threading.Thread(target=report, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n退出", flush=True)


if __name__ == "__main__":
    main()
