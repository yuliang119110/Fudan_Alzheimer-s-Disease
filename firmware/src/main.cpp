/*
 * INMP441 录音测试  —  ESP32-S3
 *
 * 接线:
 *   INMP441        ESP32-S3
 *   VDD    ──────► 3.3V
 *   GND    ──────► GND
 *   SCK    ──────► GPIO4
 *   WS     ──────► GPIO5
 *   SD     ──────► GPIO6
 *   L/R    ──────► GND      (接地 = 左声道)
 *
 * 串口 921600 (为了快速上传 PCM)。对着麦克风说话, 看电平条有没有跟着动。
 * 按 r 录 1 分钟, 固件会把 1.92MB 的 PCM 直接通过串口吐出来。
 */

#include <Arduino.h>
#include <driver/i2s.h>
#include <Wire.h>
#include <math.h>

// ---------- 引脚 ----------
// N16R8 上 GPIO26~37 被 Flash(26-32) + Octal PSRAM(33-37) 占用, 不可用。
// GPIO0/3/45/46 是 strapping, GPIO19/20 是 USB, GPIO43/44 是 UART0, GPIO48 是板载 RGB。
static const int PIN_MIC_BCLK = 4;
static const int PIN_MIC_WS   = 5;
static const int PIN_MIC_DIN  = 6;

static const int PIN_SPK_BCLK = 15;
static const int PIN_SPK_WS   = 16;
static const int PIN_SPK_DOUT = 17;

// ---------- 参数 ----------
static const uint32_t  SAMPLE_RATE = 16000;
static const i2s_port_t MIC_PORT   = I2S_NUM_0;   // 录音
static const i2s_port_t SPK_PORT   = I2S_NUM_1;   // 放音

static const int    LOOP_SECONDS = 5;
static const size_t LOOP_SAMPLES = (size_t)SAMPLE_RATE * LOOP_SECONDS;

// INMP441 输出 24bit 数据, MSB 对齐在 32bit 槽里, 低 8 位恒为 0。
// 所以原始值范围恰好是 ±2^31, 右移 16 位正好精确映射到 16bit 的 ±32768。
// 麦克风物理上不可能超出自己的 24 位量程, 因此 >>16 永不削顶。
// 小于 16 就是采集时先放大, 会吃掉余量且削顶不可逆 ——
// 要更响请在下游数字处理里加增益, 别在这里丢余量。
// 串口输入 + / - 可实时调整。
static int MIC_SHIFT = 16;

// ---------- 录音缓冲 (放 PSRAM, 16kHz*16bit 单声道 = 32KB/秒) ----------
static const int    RECORD_SECONDS = 60;
static const size_t RECORD_SAMPLES = (size_t)SAMPLE_RATE * RECORD_SECONDS;  // 960000
static int16_t     *gRecBuf = nullptr;

static void recordAndDump();

// ---------- 一级高通, 去掉 INMP441 的直流偏置 ----------
static float dcX1 = 0.0f, dcY1 = 0.0f;
static inline int32_t dcBlock(int32_t x) {
  float y = (float)x - dcX1 + 0.995f * dcY1;
  dcX1 = (float)x;
  dcY1 = y;
  return (int32_t)y;
}

// ---------- I2S 初始化 ----------
static void micInit() {
  i2s_config_t cfg = {};
  cfg.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate          = SAMPLE_RATE;
  cfg.bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT;   // L/R 接 GND = 左声道
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;   // 标准 Philips I2S
  cfg.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count        = 8;
  cfg.dma_buf_len          = 256;
  cfg.use_apll             = false;
  cfg.tx_desc_auto_clear   = false;
  cfg.fixed_mclk           = 0;
  // mclk_multiple / bits_per_chan 留 0 = 用默认值
  ESP_ERROR_CHECK(i2s_driver_install(MIC_PORT, &cfg, 0, nullptr));

  i2s_pin_config_t pins = {};
  pins.mck_io_num   = I2S_PIN_NO_CHANGE;
  pins.bck_io_num   = PIN_MIC_BCLK;
  pins.ws_io_num    = PIN_MIC_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num  = PIN_MIC_DIN;
  ESP_ERROR_CHECK(i2s_set_pin(MIC_PORT, &pins));
  i2s_zero_dma_buffer(MIC_PORT);
}

static void spkInit() {
  i2s_config_t cfg = {};
  cfg.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  cfg.sample_rate          = SAMPLE_RATE;
  cfg.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
  // 用立体声、左右填同样的值: MAX98357A 在 SD 悬空时按 (L+R)/2 输出,
  // 这样不管 SD 配成哪种模式都是满幅, 不会被白白衰减 6dB。
  cfg.channel_format       = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count        = 8;
  cfg.dma_buf_len          = 256;
  cfg.use_apll             = false;
  cfg.tx_desc_auto_clear   = true;    // 欠载时自动填 0, 避免咔哒噪声
  cfg.fixed_mclk           = 0;
  ESP_ERROR_CHECK(i2s_driver_install(SPK_PORT, &cfg, 0, nullptr));

  i2s_pin_config_t pins = {};
  pins.mck_io_num   = I2S_PIN_NO_CHANGE;
  pins.bck_io_num   = PIN_SPK_BCLK;
  pins.ws_io_num    = PIN_SPK_WS;
  pins.data_out_num = PIN_SPK_DOUT;
  pins.data_in_num  = I2S_PIN_NO_CHANGE;
  ESP_ERROR_CHECK(i2s_set_pin(SPK_PORT, &pins));
  i2s_zero_dma_buffer(SPK_PORT);
}

// 读 n 个 16bit 单声道样本: 内部按 32bit 读, 直流阻断 + 移位后转 16bit
static size_t micRead16(int16_t *dst, size_t n) {
  static int32_t raw[512];
  size_t got = 0;
  while (got < n) {
    size_t chunk = (n - got > 512) ? 512 : (n - got);
    size_t bytes = 0;
    if (i2s_read(MIC_PORT, raw, chunk * sizeof(int32_t), &bytes,
                 pdMS_TO_TICKS(1000)) != ESP_OK) break;
    size_t cnt = bytes / sizeof(int32_t);
    if (!cnt) break;
    for (size_t i = 0; i < cnt; i++) {
      int32_t s = dcBlock(raw[i] >> MIC_SHIFT);
      dst[got++] = (int16_t)constrain(s, -32768, 32767);
    }
  }
  return got;
}

// ==================== 放音 ====================
// 单声道样本写成左右两路一样的立体声帧 (见 spkInit 里的说明)
static void spkWrite(const int16_t *src, size_t n) {
  static int16_t frame[512 * 2];
  size_t i = 0;
  while (i < n) {
    size_t chunk = (n - i > 512) ? 512 : (n - i);
    for (size_t k = 0; k < chunk; k++) {
      frame[k * 2]     = src[i + k];
      frame[k * 2 + 1] = src[i + k];
    }
    size_t bytes = 0;
    i2s_write(SPK_PORT, frame, chunk * 2 * sizeof(int16_t), &bytes, portMAX_DELAY);
    i += chunk;
  }
}

static void playTone(float freq, int ms, float amp) {
  const int total = SAMPLE_RATE * ms / 1000;
  const int fade  = SAMPLE_RATE / 200;      // 5ms 淡入淡出, 防止咔哒声
  static int16_t buf[512];
  double phase = 0.0, step = 2.0 * PI * freq / SAMPLE_RATE;

  for (int done = 0; done < total; ) {
    int chunk = (total - done > 512) ? 512 : (total - done);
    for (int k = 0; k < chunk; k++) {
      int   idx = done + k;
      float env = 1.0f;
      if (idx < fade)              env = (float)idx / fade;
      else if (idx > total - fade) env = (float)(total - idx) / fade;
      buf[k] = (int16_t)(sin(phase) * amp * env * 32767.0);
      phase += step;
      if (phase >= 2.0 * PI) phase -= 2.0 * PI;
    }
    spkWrite(buf, chunk);
    done += chunk;
  }
}

// ==================== 测试项 ====================
// 测试音刻意用接近满幅 (0.9), 因为 GAIN 引脚若悬空只有 3dB, 信号小了会听不见
static void testSpeaker() {
  Serial.println();
  Serial.println("[喇叭] 1kHz 正弦 1.5 秒 ...");
  playTone(1000, 1500, 0.9f);
  delay(300);
  Serial.println("[喇叭] 扫频 200Hz -> 4kHz (听低频下潜和高频延展) ...");
  for (float f = 200; f <= 4200; f *= 1.12f) playTone(f, 90, 0.8f);
  delay(200);
  Serial.println("[喇叭] 完成。听到声音 = MAX98357A + 喇叭 OK");
  Serial.println();
}

static void testLoopback() {
  if (!gRecBuf) { Serial.println("[回环] PSRAM 缓冲没分配成功!"); return; }
  Serial.println();
  Serial.printf("[回环] 录音 %d 秒, 现在说话 ...\n", LOOP_SECONDS);
  size_t got = 0;
  while (got < LOOP_SAMPLES) {
    size_t want = LOOP_SAMPLES - got;
    if (want > 512) want = 512;
    size_t n = micRead16(gRecBuf + got, want);
    if (!n) break;
    got += n;
  }
  Serial.printf("[回环] 录到 %u 样本 (%.1f 秒), 回放 ...\n",
                (unsigned)got, (double)got / SAMPLE_RATE);
  spkWrite(gRecBuf, got);
  Serial.println("[回环] 完成");
  Serial.println();
}

// ==================== MAX30102 (I2C 心率/血氧) ====================
// 接线: SDA->GPIO8  SCL->GPIO9  VIN->3V3  GND->GND  INT->不接
// GPIO8/9 在 N16R8 上完全空闲 (不在 Flash 26-32 / Octal PSRAM 33-37 /
// strapping 0/3/45/46 / USB 19/20 / UART0 43/44 里), 而且正好是
// Arduino-ESP32 在 esp32-s3 上的默认 I2C 引脚。
static const int     PIN_I2C_SDA = 8;
static const int     PIN_I2C_SCL = 9;
static const uint8_t MAX_ADDR    = 0x57;      // 7bit 地址

static bool maxWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MAX_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool maxRead(uint8_t reg, uint8_t *buf, size_t n) {
  Wire.beginTransmission(MAX_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;   // false = 不发停止位, 直接转读
  if (Wire.requestFrom((int)MAX_ADDR, (int)n) != (int)n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

static void i2cScan() {
  Serial.println("[I2C] 扫描总线 ...");
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("   发现设备 0x%02X%s\n", a, (a == MAX_ADDR) ? "   <-- MAX30102" : "");
      found++;
    }
  }
  if (!found)
    Serial.println("   总线上一个设备都没有: 查 SDA/SCL 是否接反、VIN 是否上电");
}

static bool maxInit() {
  // 地址别记反: PART_ID 在 0xFF (值 0x15), REV_ID 在 0xFE (值 0x03)。
  // 实测 0xFE=0x03 / 0xFF=0x15, 正好印证。
  uint8_t part = 0, rev = 0;
  if (!maxRead(0xFF, &part, 1)) {
    Serial.println("!! 读不到 PART_ID —— I2C 没通");
    i2cScan();
    return false;
  }
  maxRead(0xFE, &rev, 1);
  Serial.printf("[MAX30102] PART_ID=0x%02X  REV_ID=0x%02X%s\n", part, rev,
                (part == 0x15) ? "   (MAX30102, 正确)" :
                (part == 0x11) ? "   !! 这是 MAX30100, 不是 MAX30102" :
                                 "   !! PART_ID 不认识");

  maxWrite(0x09, 0x40);                       // RESET
  delay(100);
  for (int i = 0; i < 50; i++) {              // 等复位完成 (硬件自动清 bit6)
    uint8_t m = 0xFF;
    maxRead(0x09, &m, 1);
    if (!(m & 0x40)) break;
    delay(10);
  }

  maxWrite(0x02, 0x00);   // 关中断 1
  maxWrite(0x03, 0x00);   // 关中断 2
  maxWrite(0x04, 0x00);   // FIFO_WR_PTR = 0
  maxWrite(0x05, 0x00);   // OVF_COUNTER = 0
  maxWrite(0x06, 0x00);   // FIFO_RD_PTR = 0
  maxWrite(0x08, 0x4F);   // 4 次平均 / 溢出回卷 / 剩 15 空位触发
  maxWrite(0x09, 0x03);   // SpO2 模式 (RED + IR 双 LED)
  maxWrite(0x0A, 0x2B);   // 4096nA 量程 / 200sps / 411us 18bit -> 4 次平均后 FIFO 出 50Hz
  maxWrite(0x0C, 0x24);   // RED 电流 0x24 * 0.2mA = 7.2mA
  maxWrite(0x0D, 0x24);   // IR  电流 7.2mA
  return true;
}

// 取出 FIFO 里攒的样本, 每个 6 字节 (RED 3 + IR 3, 只有低 18 位有效)
static int maxReadFifo(uint32_t *red, uint32_t *ir, int maxN) {
  uint8_t wp = 0, rp = 0;
  if (!maxRead(0x04, &wp, 1) || !maxRead(0x06, &rp, 1)) return 0;
  int n = (int)wp - (int)rp;
  if (n < 0) n += 32;                          // FIFO 是 32 深度环形缓冲
  if (n > maxN) n = maxN;
  for (int i = 0; i < n; i++) {
    uint8_t b[6];
    if (!maxRead(0x07, b, 6)) return i;
    red[i] = (((uint32_t)b[0] << 16) | ((uint32_t)b[1] << 8) | b[2]) & 0x3FFFF;
    ir[i]  = (((uint32_t)b[3] << 16) | ((uint32_t)b[4] << 8) | b[5]) & 0x3FFFF;
  }
  return n;
}

// AS6221 的函数和状态定义在本文件更靠后的位置, 但这里的采集循环要用到,
// 所以先声明一下 (C++ 里函数用前必须可见)。
static bool  gTempOk = false;
static float tempReadC();

// 简易心率检测: 一阶高通压掉基线漂移, 交流分量过自适应阈值算一次心跳。
// 这只是 bring-up 用的粗糙算法, 不是正经 HR 算法 —— 目的是让你看到
// 传感器确实在跟着脉搏动。
//
// 连续采集 MAX_SECONDS 秒, 每个样本吐一行 CSV 给上位机 (tools/pulse.py) 画波形。
// 固件这边不做任何排版 —— 串口协议越简单, 上位机越不可能解析错。
static const int MAX_SECONDS = 60;

static void testMax30102() {
  Serial.println();
  if (!maxInit()) return;

  Serial.printf("[MAX30102] 连续采集 %d 秒, 请把食指指腹轻按在传感器上\n", MAX_SECONDS);
  Serial.printf("[MAX30102] 温度: %s\n", gTempOk ? "AS6221 在线, 一起发" : "AS6221 不在, 温度列填 -100000");
  Serial.println("[MAX30102] 数据走 CSV, 用 tools/pulse.py 看实时波形");
  Serial.println("D,BEGIN");

  const uint32_t t0 = millis();
  float dcRed = 0, dcIr = 0;

  // AS6221 只有 4 sps, 没必要每个样本都去读 I2C —— 每 250ms 更新一次足够
  float    tempC      = NAN;
  uint32_t lastTempMs = 0;

  // 用一阶高通 (直流阻断器) 滤掉基线漂移, 而不是拿慢速低通去"跟踪"它。
  // 之前用 alpha=0.02 的低通相减, 时间常数 2 秒 —— 手指压力/血流造成的
  // 漂移周期约 7~8 秒, 比它慢, 于是漂移整个穿了过来, 真正的脉搏被淹没。
  // y[n] = x[n] - x[n-1] + a*y[n-1],  a = 1 - 2*pi*fc/fs
  // FIFO 实际出数率是 50Hz (200sps / 4 次平均), 所以 a = 0.874 对应 fc ≈ 1.0Hz,
  // 比当初设想的 0.5Hz 高一倍。这不是 bug: 漂移才是读数出不来的元凶, 截止频率
  // 抬高换来的是对 <0.3Hz 压力漂移更强的压制, 代价是 1.2Hz 的脉搏幅度只剩 ~77%。
  // 心率数字走 millis() 量心跳间隔, 不受这个衰减影响, 只有波形幅度变小。
  // 想让波形更饱满, 把 a 提到 0.937 (fc = 0.5Hz) 即可。
  const float HP_A = 0.874f;
  float hpX1 = 0, hpY1 = 0;

  float prevAc = 0, acPeak = 1, acLast = 0, bpm = 0;
  uint32_t lastBeat = 0;

  while (millis() - t0 < (uint32_t)MAX_SECONDS * 1000) {
    uint32_t red[16], ir[16];
    int n = maxReadFifo(red, ir, 16);
    if (n == 0) { delay(5); continue; }

    if (gTempOk && millis() - lastTempMs >= 250) {
      lastTempMs = millis();
      float t = tempReadC();
      if (!isnan(t)) tempC = t;
    }

    for (int i = 0; i < n; i++) {
      dcRed += 0.02f * ((float)red[i] - dcRed);
      dcIr  += 0.02f * ((float)ir[i]  - dcIr);

      float x = (float)ir[i];
      float y = x - hpX1 + HP_A * hpY1;
      hpX1 = x;
      hpY1 = y;
      acLast = y;

      int  finger  = (dcIr > 10000.0f) ? 1 : 0;   // IR 直流够高 = 有东西把光反回来
      bool beatNow = false;

      if (finger) {
        float mag = fabsf(acLast);
        if (mag > acPeak) acPeak = mag;
        acPeak *= 0.99f;    // τ ≈ 4 秒: 跟得上幅度变化, 又不会被单次抖动带偏

        // 上升穿过 40% 峰值 -> 算一次心跳
        float thr = acPeak * 0.4f;
        if (acLast > thr && prevAc <= thr) {
          uint32_t now = millis();
          if (!lastBeat) {
            lastBeat = now;                       // 第一跳只记时间, 算不了间隔
          } else if (now - lastBeat > 300) {      // 不应期 300ms = 生理上限 200BPM
            float inst = 60000.0f / (float)(now - lastBeat);
            bpm = (bpm > 0) ? (bpm * 0.7f + inst * 0.3f) : inst;
            lastBeat = now;
            beatNow  = true;
            playTone(2000, 25, 0.6f);             // 每跳响一声, 喇叭接了就能听见
          }
        }
        prevAc = acLast;
      } else {
        acPeak = 1; prevAc = 0; lastBeat = 0; bpm = 0;   // 手指拿开就复位
        hpX1 = 0; hpY1 = 0;
      }

      // 温度用毫摄氏度整数发, 上位机不用解析浮点
      int tmilli = isnan(tempC) ? -100000 : (int)lroundf(tempC * 1000.0f);

      Serial.printf("D,%lu,%lu,%d,%d,%d,%d,%d\n",
                    (unsigned long)red[i], (unsigned long)ir[i],
                    (int)acLast,
                    (bpm > 0) ? (int)(bpm + 0.5f) : -1,
                    finger, beatNow ? 1 : 0, tmilli);
    }
  }

  Serial.println("D,END");
  Serial.println("[MAX30102] 采集结束");
  Serial.println();
}

// ==================== AS6221 (I2C 高精度温度) ====================
// 接线: SDA->GPIO10  SCL->GPIO11  VCC->3V3  GND->GND  ADD0->GND  ALT->不接
//
// AS6221 和 MAX30102 **不在同一条总线上**: MAX30102 在 GPIO8/9, AS6221 在
// GPIO10/11。ESP32-S3 有两个 I2C 控制器, 正好各喂一条 —— Wire 和 Wire1。
// 两条独立总线的好处是互不干扰, 也不存在地址冲突。
static const int PIN_T_SDA = 10;
static const int PIN_T_SCL = 11;

// AS6221 的地址由 ADD0 和 ALERT/ADD1 共同决定 (0x44~0x4B 共 8 个), 手册
// 明确要求这两个脚都不能悬空 —— 而 ALT 正是 ALERT/ADD1, 悬空时地址不确定。
// 所以这里不写死地址: 开机扫一遍, 扫到谁就用谁。
static uint8_t gTempAddr = 0x48;    // 0x48 = ADD0/ADD1 都接 GND 时的地址

static bool tRead(uint8_t reg, uint8_t *buf, size_t n) {
  Wire1.beginTransmission(gTempAddr);
  Wire1.write(reg);
  if (Wire1.endTransmission(false) != 0) return false;   // false = 不发停止位, 转读
  if (Wire1.requestFrom((int)gTempAddr, (int)n) != (int)n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire1.read();
  return true;
}

static bool tRead16(uint8_t reg, uint16_t *out) {
  uint8_t b[2];
  if (!tRead(reg, b, 2)) return false;
  *out = ((uint16_t)b[0] << 8) | b[1];   // MSB 先到
  return true;
}

static bool tempFind() {
  Serial.println("[AS6221] 扫描 Wire1 (GPIO10/11) ...");
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire1.beginTransmission(a);
    if (Wire1.endTransmission() == 0) {
      bool isAs = (a >= 0x44 && a <= 0x4B);
      Serial.printf("   发现设备 0x%02X%s\n", a, isAs ? "   <-- AS6221" : "");
      if (isAs) { gTempAddr = a; found++; }
    }
  }
  if (!found) {
    Serial.println("   没扫到 AS6221 (地址应在 0x44~0x4B)。检查:");
    Serial.println("     1. ALT 悬空会让地址不确定 —— 把它也接到 GND, 地址就固定 0x48");
    Serial.println("     2. SDA(10)/SCL(11) 有没有接反, VCC 是否上电");
    Serial.println("     3. 这条总线上的上拉电阻 (模块一般自带, 光板子要外加 4.7k)");
    gTempOk = false;
    return false;
  }
  Serial.printf("[AS6221] 用地址 0x%02X\n", gTempAddr);
  gTempOk = true;
  return true;
}

// TVAL 是 16bit 二进制补码, 1 LSB = 1/128 °C = 0.0078125 °C
static float tempReadC() {
  uint16_t raw = 0;
  if (!tRead16(0x00, &raw)) return NAN;
  return (float)(int16_t)raw / 128.0f;
}

static void testTemp() {
  Serial.println();
  if (!gTempOk && !tempFind()) return;

  uint16_t cfg = 0;
  if (tRead16(0x01, &cfg))
    Serial.printf("[AS6221] CONFIG = 0x%04X (先不动它, 默认转换速率够看反应)\n", cfg);

  Serial.println("[AS6221] 读 20 秒。用手指捏住传感器, 看温度往上爬 ...");
  Serial.println("   时间     温度(℃)   原始值");
  Serial.println("   -------  --------  ------");

  const uint32_t t0 = millis();
  while (millis() - t0 < 20000) {
    uint16_t raw = 0;
    if (tRead16(0x00, &raw))
      Serial.printf("   %5.1fs  %8.3f  0x%04X\n",
                    (double)(millis() - t0) / 1000.0,
                    (double)((int16_t)raw) / 128.0, raw);
    else
      Serial.println("   !! 读 TVAL 失败");
    delay(500);
  }
  Serial.println("[AS6221] 完成。室温 20~28℃, 捏住往上爬 = 正常");
  Serial.println();
}

// ---------- 电平条 ----------
// 显示灵敏度和数据缩放解耦: >>16 之后正常说话大约 RMS 300,
// 所以用 512 做满刻度, 这样说话能占到半屏以上
static String meter(double rms) {
  int n = (int)(rms / 512.0 * 32.0);
  n = constrain(n, 0, 32);
  String s;
  for (int i = 0; i < 32; i++) s += (i < n) ? '#' : '.';
  return s;
}

// ---------- 串口命令 ----------
static void printMenu() {
  Serial.println();
  Serial.println("   s / 1    喇叭测试 (1kHz 正弦 + 扫频)");
  Serial.println("   l / 2    回环: 录 5 秒 -> 立刻回放");
  Serial.println("   r        录 1 分钟, 然后通过串口上传 PCM");
  Serial.println("   m / 3    MAX30102 心率 (60 秒 CSV, 配 tools/pulse.py 看波形)");
  Serial.println("   t / 4    AS6221 温度 (I2C, 测 20 秒)");
  Serial.printf ("   + / -    麦克风增益 (MIC_SHIFT, 当前 %d)\n", MIC_SHIFT);
  Serial.println("   h        显示本菜单");
  Serial.println();
}

static void handleSerial() {
  while (Serial.available()) {
    int c = Serial.read();
    switch (c) {
      case 's': case 'S': case '1':
        testSpeaker();
        break;
      case 'l': case 'L': case '2':
        testLoopback();
        break;
      case 'r': case 'R':
        recordAndDump();
        break;
      case 'm': case 'M': case '3':
        testMax30102();
        break;
      case 't': case 'T': case '4':
        testTemp();
        break;
      case '+':
        if (MIC_SHIFT > 1) { MIC_SHIFT--; Serial.printf(">> 增益提高, MIC_SHIFT = %d\n", MIC_SHIFT); }
        break;
      case '-':
        if (MIC_SHIFT < 30) { MIC_SHIFT++; Serial.printf(">> 增益降低, MIC_SHIFT = %d\n", MIC_SHIFT); }
        break;
      case 'h': case 'H': case '?':
        printMenu();
        break;
      default: break;    // 忽略换行等
    }
  }
}

// ==================== 录音并上传 ====================
// 录满 RECORD_SECONDS 秒, 然后按下面的帧格式把 PCM 吐到串口:
//
//     \nPCM <样本数> <采样率> <校验和>\n
//     <样本数 * 2 字节, 小端有符号 16bit>
//     \n<<<END>>>\n
//
// 校验和是所有字节的 32 位累加和, 上位机据此确认没有丢字节。
static void recordAndDump() {
  if (!gRecBuf) {
    Serial.println("!! PSRAM 缓冲没分配成功, 无法录音");
    return;
  }

  Serial.printf("\n[录音] 开始录 %d 秒, 请说话 ...\n", RECORD_SECONDS);
  uint32_t t0 = millis();
  size_t got = 0;
  int lastReport = -1;

  while (got < RECORD_SAMPLES) {
    size_t want = RECORD_SAMPLES - got;
    if (want > 512) want = 512;
    size_t n = micRead16(gRecBuf + got, want);
    if (!n) break;
    got += n;

    int sec = (millis() - t0) / 1000;
    if (sec != lastReport) {
      lastReport = sec;
      Serial.printf("  ... %2d / %d 秒\n", sec, RECORD_SECONDS);
    }
  }
  Serial.printf("[录音] 录到 %u 样本 (%.1f 秒)\n", (unsigned)got, (double)got / SAMPLE_RATE);

  // 算校验和
  const uint8_t *p = (const uint8_t *)gRecBuf;
  const size_t total = got * sizeof(int16_t);
  uint32_t sum = 0;
  for (size_t i = 0; i < total; i++) sum += p[i];

  delay(300);          // 让上位机从文本模式切到二进制读取
  Serial.printf("\nPCM %u %lu %lu\n", (unsigned)got, (unsigned long)SAMPLE_RATE,
                (unsigned long)sum);
  Serial.flush();

  size_t sent = 0;
  while (sent < total) {
    size_t chunk = total - sent;
    if (chunk > 4096) chunk = 4096;
    Serial.write(p + sent, chunk);
    sent += chunk;
  }
  Serial.flush();

  Serial.println("\n<<<END>>>");
  Serial.println("[录音] 上传完成");
}

// ---------- 统计 ----------
static uint32_t tPrint = 0;
static double   sumSq  = 0;
static int32_t  peak   = 0;
static size_t   cnt    = 0;

void setup() {
  // 必须在 begin() 之前设置, 否则一次 write 4096 字节会被 256 字节的
  // 默认 TX 缓冲拖成一段段等待, 1.92MB 要传很久
  Serial.setTxBufferSize(16384);
  Serial.begin(921600);
  delay(400);

  Serial.println();
  Serial.println("=====================================");
  Serial.println("   INMP441 录音测试   ESP32-S3");
  Serial.println("=====================================");
  Serial.printf ("   芯片  : %s rev%d\n", ESP.getChipModel(), ESP.getChipRevision());
  Serial.printf ("   Flash : %lu MB\n", (unsigned long)(ESP.getFlashChipSize() / 1048576));
  Serial.printf ("   PSRAM : %.1f MB  (free %lu KB)\n",
                 ESP.getPsramSize() / 1048576.0,
                 (unsigned long)(ESP.getFreePsram() / 1024));
  Serial.println("-------------------------------------");
  Serial.println("   VDD->3V3  GND->GND  L/R->GND");
  Serial.println("   SCK->GPIO4  WS->GPIO5  SD->GPIO6");
  Serial.println("-------------------------------------");

  micInit();
  spkInit();

  // 两条独立的 I2C 总线 (ESP32-S3 有两个 I2C 控制器, 正好各喂一条):
  //   Wire  -> GPIO8/9   : MAX30102
  //   Wire1 -> GPIO10/11 : AS6221
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  Wire1.begin(PIN_T_SDA, PIN_T_SCL);

  gTempOk = tempFind();      // 开机扫一次, 之后直接读 TVAL

  gRecBuf = (int16_t *)ps_malloc(RECORD_SAMPLES * sizeof(int16_t));
  Serial.printf("   录音缓冲: %u KB @ PSRAM  %s\n",
                (unsigned)(RECORD_SAMPLES * sizeof(int16_t) / 1024),
                gRecBuf ? "OK" : "!! 分配失败");
  Serial.printf("   PSRAM 剩余: %lu KB\n", (unsigned long)(ESP.getFreePsram() / 1024));

  Serial.printf("\n   I2S0 已启动: 16kHz / 32bit / 左声道 / MIC_SHIFT=%d\n", MIC_SHIFT);
  printMenu();

  Serial.println("   RMS      峰值    电平");
  Serial.println("   -------- ------- --------------------------------");
  tPrint = millis();
}

void loop() {
  handleSerial();

  int16_t buf[256];
  size_t n = micRead16(buf, 256);

  if (n == 0) {
    Serial.println("!! i2s_read 返回 0 字节");
    delay(300);
    return;
  }

  for (size_t i = 0; i < n; i++) {
    int32_t v = buf[i];
    int32_t a = (v < 0) ? -v : v;
    if (a > peak) peak = a;
    sumSq += (double)v * (double)v;
    cnt++;
  }

  if (millis() - tPrint >= 250) {
    double rms = cnt ? sqrt(sumSq / cnt) : 0.0;
    Serial.printf("   %8.1f %7ld  %s\n", rms, (long)peak, meter(rms).c_str());
    tPrint = millis();
    sumSq  = 0;
    peak   = 0;
    cnt    = 0;
  }
}
