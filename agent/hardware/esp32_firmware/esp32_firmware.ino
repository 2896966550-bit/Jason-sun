/*
 * Equis 多传感器数据采集固件 — ESP32
 *
 * ⚠️ 归档提示（代码审计后补充）
 *   本 .ino 是早期 Arduino 版本，**当前生产使用的是 PlatformIO 版本**：
 *       <项目根>\esp32_sensors\src\main.cpp  (配合 esp32_sensors\platformio.ini)
 *   两者都能用，但单位为不同量程：
 *       main.cpp : ±8g / ±500°/s，输出 m/s²
 *       本 .ino  : ±2g / ±250°/s，输出 **g**
 *   下游 tools/sensor_reader.py 会依据 JSON 里的 "unit" 字段自动换算，
 *   所以两者都能被正确解析。**烧录前请确认你烧的是哪一个。**
 *
 * 硬件连接:
 *   DHT22:   VCC→3.3V  GND→GND  DATA→GPIO4
 *   MPU6050: VCC→3.3V  GND→GND  SDA→GPIO21  SCL→GPIO22
 *
 * 输出格式 (115200 baud, 每 2 秒一条，符合 DHT22 采样间隔要求):
 *   {"t":25.3,"h":48.2,"unit":"g",
 *    "ax":0.00,"ay":-0.03,"az":1.00,"gx":0.1,"gy":0.2,"gz":0.3}
 *
 * ────────────────────────────────────────────────────────────
 * 代码审计修复记录（原始版本的问题）
 * ────────────────────────────────────────────────────────────
 * 1) 单位契约缺失（严重）：文件头注释声称 az=9.81（m/s²），但代码
 *    az_raw/16384.0 得到的是 **g**，静止时 az=1.00。而下游
 *    sensor_reader.py / perception/bus.py 一律按 m/s² 标注与判定，
 *    导致加速度被低估 9.8 倍 —— "振动过大"告警几乎永不触发。
 *    修复：输出中显式声明 "unit":"g"，让 sensor_reader._normalize_units
 *    真正生效（该函数此前是死代码，因为没有任何固件发 unit 字段）。
 *
 * 2) DHT22 采样间隔违规（严重）：原为 delay(1000)，而 DHT22 手册要求
 *    采样间隔 ≥2 s，否则读数漂移/失败。修复：改为 2000 ms。
 *
 * 3) 故障时伪造数据（严重）：原代码把 NaN 强转为 0，于是传感器断线时
 *    会输出"温度 0°C / 湿度 0%RH"这种看似有效的假数据，并流入温度补偿
 *    与不确定度计算。修复：输出 null，由主机侧识别为"读数无效"。
 *
 * 4) I2C 读取未校验（中等）：Wire.requestFrom 的返回值未检查，
 *    读取失败时会把陈旧/零值当成有效测量。修复：检查 available()。
 *
 * 5) (Wire.read()<<8)|Wire.read() 的求值顺序在 C++ 中未规定
 *    （非 UB，GCC 恰好从左到右）。修复：改为显式两步读取。
 */

#include <Wire.h>
#include <DHT.h>

// ── 引脚定义 ──
#define DHTPIN 4
#define DHTTYPE DHT22

// ── 采样间隔：DHT22 手册要求 ≥2 s ──
#define SAMPLE_INTERVAL_MS 2000

// ── 全局对象 ──
DHT dht(DHTPIN, DHTTYPE);
const int MPU_ADDR = 0x68;  // MPU6050 I2C 地址

// ── 初始化 ──
void setup() {
  Serial.begin(115200);

  // DHT22
  dht.begin();
  delay(1000);  // DHT22 上电稳定

  // MPU6050 唤醒
  Wire.begin(21, 22);         // SDA=21, SCL=22
  Wire.setClock(400000);      // 400kHz I2C
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B);           // PWR_MGMT_1
  Wire.write(0x00);           // 0 = 唤醒
  Wire.endTransmission(true);

  // 加速度计量程 ±2g → 16384 LSB/g
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x1C);           // ACCEL_CONFIG
  Wire.write(0x00);           // ±2g
  Wire.endTransmission(true);

  // 陀螺仪量程 ±250°/s → 131 LSB/(°/s)
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x1B);           // GYRO_CONFIG
  Wire.write(0x00);           // ±250°/s
  Wire.endTransmission(true);
}

// ── 读 16 位有符号大端值（显式两步，避免求值顺序问题）──
static int16_t read_i16() {
  uint8_t hi = Wire.read();
  uint8_t lo = Wire.read();
  return (int16_t)((hi << 8) | lo);
}

// ── 打印 float 或 null（NaN 不伪装成 0）──
static void print_float_or_null(float v, int digits) {
  if (isnan(v) || isinf(v)) {
    Serial.print("null");
  } else {
    Serial.print(v, digits);
  }
}

// ── 主循环 ──
void loop() {
  // 1. 读取 DHT22
  float temp = dht.readTemperature();
  float hum  = dht.readHumidity();

  // 2. 读取 MPU6050 加速度计（6 字节，起始 0x3B）
  float ax = NAN, ay = NAN, az = NAN;
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 6, true);
  if (Wire.available() >= 6) {
    int16_t ax_raw = read_i16();
    int16_t ay_raw = read_i16();
    int16_t az_raw = read_i16();
    // ±2g 量程 → 16384 LSB/g（单位：g）
    ax = ax_raw / 16384.0;
    ay = ay_raw / 16384.0;
    az = az_raw / 16384.0;
  }

  // 3. 读取 MPU6050 陀螺仪（6 字节，起始 0x43）
  float gx = NAN, gy = NAN, gz = NAN;
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x43);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 6, true);
  if (Wire.available() >= 6) {
    int16_t gx_raw = read_i16();
    int16_t gy_raw = read_i16();
    int16_t gz_raw = read_i16();
    // ±250°/s 量程 → 131 LSB/(°/s)
    gx = gx_raw / 131.0;
    gy = gy_raw / 131.0;
    gz = gz_raw / 131.0;
  }

  // 4. 串口输出 JSON
  //    关键：显式声明 "unit":"g"，主机侧据此换算为 m/s²。
  Serial.print("{\"t\":");
  print_float_or_null(temp, 1);
  Serial.print(",\"h\":");
  print_float_or_null(hum, 1);
  Serial.print(",\"unit\":\"g\"");
  Serial.print(",\"ax\":");
  print_float_or_null(ax, 3);
  Serial.print(",\"ay\":");
  print_float_or_null(ay, 3);
  Serial.print(",\"az\":");
  print_float_or_null(az, 3);
  Serial.print(",\"gx\":");
  print_float_or_null(gx, 2);
  Serial.print(",\"gy\":");
  print_float_or_null(gy, 2);
  Serial.print(",\"gz\":");
  print_float_or_null(gz, 2);
  Serial.println("}");

  // 每 2 秒一条（DHT22 采样间隔要求 ≥2 s）
  delay(SAMPLE_INTERVAL_MS);
}
