# Equis ESP32 传感器 v2

目标板 esp32dev，当前固件2.0.3。按WHO_AM_I选择6050（0x68）或6500（0x70）兼容分支；不承诺仅凭寄存器即可认证原厂芯片。GPIO4=DHT22，GPIO21/22=MPU6050 SDA/SCL，串口 115200。未更改接线或传感器供电。主机读取入口为 `agent/tools/sensor_reader.py`；`agent/hardware/esp32_firmware/esp32_firmware.ino` 是旧版归档，不作为本协议烧录源。

## 采样与单位

- DHT22：上电等待后每至少两秒读取一次，单位 degC、percentRH；失败输出 null 和 valid=false，不沿用上次温度假装当前成功。
- MPU6050/MPU6500 兼容分支：配置输出率 100Hz，±8g/±500deg/s，DLPF=4（6050加速度21Hz；6500独立加速度滤波约20Hz；陀螺20Hz）。主机必须以实际设备时间计算有效采样率与缺口，不能把配置值当实测采样率。
- 串口启用2048字节发送缓冲；每最多五个 IMU 观测打包传输，采用整包单次写入，避免逐数字打印阻塞采样及每帧重复元数据占满115200串口。加速度按9.80665换为m/s²，陀螺为deg/s。不自动减去零偏或重力。
- DHT读取、I2C及串口传输可能引入抖动；记录missed_slots、i2c_errors和设备时间，不补造等间隔样本。未完成带宽与时间关联评定，不能作为高频振动仪或毫米误差依据。

## 串口协议

每行一个严格 JSON 对象，公共字段为 v=2、type、fw、boot、seq。boot 是本次启动标识；seq 按输出包连续递增；设备 t_us 来自 ESP32 64位单调时钟，不是UTC，不与相机曝光同步。

- info：采样配置、量程、滤波、当前I2C地址、MPU配置成功状态、累计失败计数，每次启动及环境采样后输出。
- env：t_us、temperature_unit、humidity_unit、t、h、valid。
- imu：unit=m/s2、gyro_unit=deg/s、samples。每条样本是 `[t_us, ax, ay, az, gx, gy, gz, valid]`；无新观测或I2C失败时数值为null。主机统计无效样本、序号缺口和时间倒退，不将其改成零。

主机每次重连清空状态；IMU超过0.5秒、环境超过5秒即失效，也检查设备时间的新鲜度。旧协议仅保留诊断兼容和单位来源标记。未声明单位的数据不获准严格事件融合。

## 回放与诊断

在 agent 目录运行：

```powershell
python -B scripts/sensor_diagnostics.py --port COM10 --seconds 30 --out sensor-check.jsonl
```

JSONL留存原始包、主机接收墙钟/单调时钟、接纳状态和原因；同名summary.json保留计数、最后快照、实际采样率、最长间隔和窗口去均值加速度RMS。RMS只是局部变化描述，旋转、姿态变化、零偏和传感器噪声均会影响它；不能自动解释成振动严重度、风险或尺寸不确定度。

烧录前保存原固件和源码备份。主机测试、固件编译、板上协议验证、长时间稳定性和物理校准分别记录，不相互替代。

参数依据：[MPU6050 官方寄存器手册](https://invensense.tdk.com/wp-content/uploads/2015/02/MPU-6000-Register-Map1.pdf)、[Adafruit DHT 库](https://github.com/adafruit/DHT-sensor-library/blob/master/DHT.cpp)、[pySerial API](https://pyserial.readthedocs.io/en/latest/pyserial_api.html)。

实机发现：用户此前称MPU6050的模块返回WHO_AM_I=0x70，因此当前使用MPU6500兼容分支，配置0x1D独立加速度滤波。参考[MPU6500官方寄存器手册](https://invensense.tdk.com/wp-content/uploads/2015/02/MPU-6500-Register-Map2.pdf)。
