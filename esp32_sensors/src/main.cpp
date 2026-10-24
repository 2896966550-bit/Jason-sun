// Equis sensor protocol v2. Existing GPIOs and 115200 baud retained.
#include <Arduino.h>
#include <Wire.h>
#include <DHT.h>
#include <esp_timer.h>
#include <math.h>

static constexpr char FW[] = "2.0.3";
static constexpr uint32_t PERIOD_US = 10000, ENV_US = 2000000;
static constexpr size_t BATCH = 5;
DHT dht(4, DHT22);
uint8_t address = 0x68, whoAmI = 0, initReg = 0, initActual = 0;
const char *initError = "not_initialized";
bool configured = false;
char bootId[9];
uint32_t sequence = 0, missedSlots = 0, i2cErrors = 0;
int64_t nextImu = 0, nextEnv = 0, lastValidUs = 0;
struct Sample { int64_t us; float a[3], g[3]; bool valid; };
Sample samples[BATCH];
size_t sampleCount = 0;

bool writeReg(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(address); Wire.write(reg); Wire.write(value);
  return Wire.endTransmission(true) == 0;
}
bool readRegs(uint8_t reg, uint8_t *out, size_t n) {
  Wire.beginTransmission(address); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(address, (uint8_t)n, (uint8_t)true) != n) {
    while (Wire.available()) Wire.read();
    return false;
  }
  for (size_t i=0; i<n; ++i) out[i] = Wire.read();
  return true;
}
bool initImu() {
  uint8_t who = 0;
  bool found = false;
  initError = "probe_failed";
  for (uint8_t candidate : {uint8_t(0x68), uint8_t(0x69)}) {
    address = candidate;
    if (readRegs(0x75, &who, 1)) {
      whoAmI = who;
      if (who == 0x68 || who == 0x70) { found = true; break; }
      initError = "unsupported_who_am_i";
    }
  }
  if (!found) return false;
  // PLL; DLPF=4: accel 21Hz/gyro 20Hz; 1kHz/(9+1); +/-8g; +/-500deg/s.
  const uint8_t regs[] = {0x6B, 0x1A, 0x19, 0x1C, 0x1B};
  const uint8_t vals[] = {0x01, 0x04, 0x09, 0x10, 0x08};
  for (size_t i=0; i<sizeof(regs); ++i) {
    initReg = regs[i];
    if (!writeReg(regs[i], vals[i])) { initError = "register_write_failed"; return false; }
    if (regs[i] == 0x6B) delay(10);
    uint8_t actual = 0;
    if (!readRegs(regs[i], &actual, 1)) { initError = "register_read_failed"; return false; }
    initActual = actual;
    if (actual != vals[i]) { initError = "register_mismatch"; return false; }
  }
  if (whoAmI == 0x70) {
    initReg = 0x1D;
    if (!writeReg(0x1D,0x04) || !readRegs(0x1D,&initActual,1) || initActual != 0x04) {
      initError = "accel_filter_mismatch"; return false;
    }
  }
  lastValidUs = 0;
  initError = "none";
  return true;
}
int16_t signed16(const uint8_t *b) {
  return (int16_t)((uint16_t(b[0]) << 8) | uint16_t(b[1]));
}
void header(const char *type) {
  Serial.printf("{\"v\":2,\"type\":\"%s\",\"fw\":\"%s\",\"boot\":\"%s\",\"seq\":%lu",
                type, FW, bootId, (unsigned long)sequence++);
}
void numberOrNull(float v, int digits) {
  if (isfinite(v)) Serial.print(v, digits); else Serial.print("null");
}
void flushBatch() {
  if (!sampleCount) return;
  char buffer[1536];
  size_t used = snprintf(buffer, sizeof(buffer),
    "{\"v\":2,\"type\":\"imu\",\"fw\":\"%s\",\"boot\":\"%s\",\"seq\":%lu,"
    "\"unit\":\"m/s2\",\"gyro_unit\":\"deg/s\",\"samples\":[",
    FW, bootId, (unsigned long)sequence++);
  for (size_t j=0; j<sampleCount; ++j) {
    Sample &s = samples[j];
    int n;
    if (s.valid) {
      n = snprintf(buffer+used, sizeof(buffer)-used,
        "%s[%llu,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,true]", j?",":"",
        (unsigned long long)s.us, s.a[0],s.a[1],s.a[2],s.g[0],s.g[1],s.g[2]);
    } else {
      n = snprintf(buffer+used, sizeof(buffer)-used,
        "%s[%llu,null,null,null,null,null,null,false]", j?",":"", (unsigned long long)s.us);
    }
    if (n<0 || (size_t)n>=sizeof(buffer)-used) { sampleCount=0; return; }
    used += n;
  }
  buffer[used++]=']'; buffer[used++]='}'; buffer[used++]='\n';
  Serial.write((const uint8_t*)buffer, used); // one buffered write, not hundreds of locked writes
  sampleCount=0;
}
void emitInfo() {
  header("info");
  Serial.printf(",\"imu_model\":\"%s\"", whoAmI==0x70?"MPU6500-compatible":whoAmI==0x68?"MPU6050-compatible":"unknown");
  Serial.printf(",\"who_am_i\":%u,\"init_error\":\"%s\",\"init_reg\":%u,\"init_actual\":%u", whoAmI, initError, initReg, initActual);
  Serial.printf(",\"t_us\":%llu,\"imu_nominal_hz\":100,\"env_period_ms\":2000,"
                "\"accel_range_g\":8,\"gyro_range_dps\":500,\"dlpf_cfg\":4,"
                "\"mpu_addr\":%u,\"imu_configured\":%s,\"missed_slots\":%lu,\"i2c_errors\":%lu}\n",
                (unsigned long long)esp_timer_get_time(), address, configured?"true":"false",
                (unsigned long)missedSlots, (unsigned long)i2cErrors);
}
void captureImu() {
  uint8_t raw[14], ready = 0;
  bool statusOk = configured && readRegs(0x3A, &ready, 1);
  if (statusOk && !(ready & 1)) return; // poll for fresh hardware output, no duplicates
  Sample &s = samples[sampleCount++];
  s.us = esp_timer_get_time();
  for (size_t k=0; k<3; ++k) s.a[k] = s.g[k] = NAN;
  s.valid = statusOk && readRegs(0x3B, raw, sizeof(raw));
  if (!s.valid) { ++i2cErrors; configured = false; }
  if (s.valid) {
    if (lastValidUs && s.us-lastValidUs >= 2*PERIOD_US) {
      missedSlots += (uint32_t)((s.us-lastValidUs)/PERIOD_US)-1;
    }
    lastValidUs = s.us;
    for (size_t k=0; k<3; ++k) {
      s.a[k] = signed16(raw+2*k) / 4096.0f * 9.80665f;
      s.g[k] = signed16(raw+8+2*k) / 65.5f;
    }
  }
  if (sampleCount == BATCH) flushBatch();
}
void captureEnvironment() {
  // DHT blocks briefly. Timestamps/gaps are preserved, never synthetic catch-up samples.
  float t = dht.readTemperature(), h = dht.readHumidity();
  bool valid = isfinite(t) && isfinite(h) && t>=-40 && t<=80 && h>=0 && h<=100;
  header("env");
  Serial.printf(",\"t_us\":%llu,\"temperature_unit\":\"degC\",\"humidity_unit\":\"percentRH\",\"t\":",
                (unsigned long long)esp_timer_get_time());
  numberOrNull(valid?t:NAN, 1); Serial.print(",\"h\":"); numberOrNull(valid?h:NAN, 1);
  Serial.printf(",\"valid\":%s}\n", valid?"true":"false");
  if (!configured) configured = initImu();
  emitInfo();
}
void setup() {
  Serial.setTxBufferSize(2048); // avoid blocking IMU polling while a batch transmits
  Serial.begin(115200);
  snprintf(bootId, sizeof(bootId), "%08lx", (unsigned long)esp_random());
  Wire.begin(21,22); Wire.setClock(100000); Wire.setTimeOut(20);
  dht.begin(); configured = initImu();
  nextImu = esp_timer_get_time()+PERIOD_US;
  nextEnv = esp_timer_get_time()+ENV_US;
  emitInfo();
}
void loop() {
  int64_t now = esp_timer_get_time();
  if (now >= nextImu) {
    nextImu = now+(configured?1000:PERIOD_US);
    captureImu();
  }
  if (now >= nextEnv) {
    flushBatch(); captureEnvironment(); nextEnv = esp_timer_get_time()+ENV_US;
  }
  delay(1);
}
