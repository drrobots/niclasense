/*
 * nicla_stream — stream every Nicla Sense ME sensor over USB serial as compact CSV.
 *
 * Board: Arduino Nicla Sense ME (BHI260AP sensor hub + BME688 gas sensor)
 * FQBN:  arduino:mbed_nicla:nicla_sense
 *
 * One CSV line per sample at STREAM_HZ. Accel, gyro, quaternion and orientation run at
 * 200 Hz on the hub -- its measured ceiling, since requesting 400 Hz or more still yields
 * ~197 Hz. The magnetometer tops out at 50 Hz (BMM150) and the BME688 / BSEC air-quality
 * fusion at ~1 Hz, so those columns repeat their most recent value between updates.
 *
 * At 200 Hz a 162-byte line needs 32 kB/s, so the link runs at 1 Mbaud -- the nRF52832
 * UARTE maximum, and about 9x what 115200 could carry. See nicla_bench/ for the
 * measurements behind both numbers.
 *
 * Lines beginning with '#' are metadata (banner + column names), everything else is data.
 *
 * Serial commands:
 *   h        reprint the header block
 *   r        reset the sequence counter and time origin
 *   s<N>     set the streaming rate to N Hz (e.g. "s100"), clamped to
 *            [1, MOTION_RATE_HZ]; digits accumulate until a non-digit arrives
 *   b<N>     set the serial baud to N (e.g. "b115200"), clamped to
 *            [9600, 1000000]; reopens the port, so the host must reconnect
 *            at the new rate afterwards
 *
 * Set STREAM_BLE to 1 to additionally advertise the same sample as a packed binary
 * notification over BLE. See README.md.
 */

#include "Arduino_BHY2.h"

#define STREAM_BLE 0

#if STREAM_BLE
#include <ArduinoBLE.h>
#endif

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

static const char FIRMWARE_VERSION[] = "nicla-stream v2";

// Serial is native USB-CDC on the nRF52832, not a physical UART, so this value doesn't
// change the actual link speed -- but it's still runtime-adjustable via the 'b' command
// (see handleCommands()) since some host CDC-ACM drivers (notably Windows' usbser.sys)
// are picky about the declared baud and may need a lower one to behave.
static uint32_t serialBaud = 1000000;

// Streaming rate is runtime-adjustable via the 's' command (see handleCommands()),
// capped at MOTION_RATE_HZ since the hub never delivers samples faster than that.
static uint32_t streamHz       = 200;
static uint32_t streamPeriodMs = 1000 / streamHz;

static const float MOTION_RATE_HZ = 200.0f;  // accel, gyro, mag, quaternion, orientation
static const float ENV_RATE_HZ    = 1.0f;    // temperature, pressure, humidity, gas, BSEC

// Dynamic ranges requested in setup(). The BHI260 reports XYZ virtual sensors as raw
// int16 counts spanning the full range, so the LSB scale is range/32768.
static const uint16_t ACCEL_RANGE_G   = 4;     // +/- 4 g
static const uint16_t GYRO_RANGE_DPS  = 2000;  // +/- 2000 dps

static const float ACCEL_SCALE = (float)ACCEL_RANGE_G / 32768.0f;    // g per LSB
static const float GYRO_SCALE  = (float)GYRO_RANGE_DPS / 32768.0f;   // dps per LSB
static const float MAG_SCALE   = 1.0f / 16.0f;                       // uT per LSB (BMM150)

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------

SensorXYZ         accel(SENSOR_ID_ACC);    // 4   corrected accelerometer
SensorXYZ         gyro (SENSOR_ID_GYRO);   // 13  corrected gyroscope
SensorXYZ         magn (SENSOR_ID_MAG);    // 22  corrected magnetometer
SensorQuaternion  quat (SENSOR_ID_RV);     // 34  rotation vector
SensorOrientation ori  (SENSOR_ID_ORI);    // 43  heading / pitch / roll
Sensor            temp (SENSOR_ID_TEMP);   // 128 degrees C
Sensor            baro (SENSOR_ID_BARO);   // 129 hPa
Sensor            hum  (SENSOR_ID_HUM);    // 130 %RH
Sensor            gas  (SENSOR_ID_GAS);    // 131 ohm
// SENSOR_ID_BSEC (115) emits an 18-byte frame, which Arduino_BHY2 routes through the
// SensorLongDataPacket path, so every BSEC field is populated without patching the library.
SensorBSEC        bsec (SENSOR_ID_BSEC);

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

static const uint8_t COLUMN_COUNT = 27;

static uint32_t sequence   = 0;
static uint32_t timeOrigin = 0;
static uint32_t nextSample = 0;

#if STREAM_BLE
// Sample as sent over BLE: uint32 seq, uint32 t_ms, then the remaining 25 columns as
// float32 (bsec_acc is widened to float for a uniform layout). 108 bytes total, which
// fits in a single notification at the ATT MTU macOS negotiates.
struct __attribute__((packed)) BleSample {
  uint32_t seq;
  uint32_t t_ms;
  float    values[25];
};

static const uint32_t BLE_STREAM_HZ        = 10;
static const uint32_t BLE_STREAM_PERIOD_MS = 1000 / BLE_STREAM_HZ;
static uint32_t       nextBleSample        = 0;

BLEService           streamService("6e400001-b5a3-f393-e0a9-e50e24dcca9e");
BLECharacteristic    sampleChar   ("6e400003-b5a3-f393-e0a9-e50e24dcca9e",
                                   BLERead | BLENotify, sizeof(BleSample));
#endif

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

void printHeader() {
  Serial.print("# ");
  Serial.print(FIRMWARE_VERSION);
  Serial.print(" rate_hz=");
  Serial.print(streamHz);
  Serial.print(" baud=");
  Serial.print(serialBaud);
  Serial.print(" columns=");
  Serial.println(COLUMN_COUNT);
  Serial.println(F("#seq,t_ms,"
                   "ax_g,ay_g,az_g,"
                   "gx_dps,gy_dps,gz_dps,"
                   "mx_uT,my_uT,mz_uT,"
                   "qx,qy,qz,qw,"
                   "heading_deg,pitch_deg,roll_deg,"
                   "temp_C,hum_pct,press_hPa,gas_ohm,"
                   "iaq,iaq_s,co2_eq_ppm,bvoc_eq_ppm,bsec_acc"));
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(serialBaud);

  // Standalone: no ESLOV and no BHY2 BLE handler, which leaves ArduinoBLE free for the
  // custom service below.
  BHY2.begin(NICLA_STANDALONE);

  accel.begin(MOTION_RATE_HZ);
  gyro.begin(MOTION_RATE_HZ);
  magn.begin(MOTION_RATE_HZ);
  quat.begin(MOTION_RATE_HZ);
  ori.begin(MOTION_RATE_HZ);

  temp.begin(ENV_RATE_HZ);
  baro.begin(ENV_RATE_HZ);
  hum.begin(ENV_RATE_HZ);
  gas.begin(ENV_RATE_HZ);
  bsec.begin(ENV_RATE_HZ);

  // Must follow begin() — the range is applied to the running virtual sensor.
  accel.setRange(ACCEL_RANGE_G);
  gyro.setRange(GYRO_RANGE_DPS);

#if STREAM_BLE
  if (BLE.begin()) {
    BLE.setLocalName("NiclaStream");
    BLE.setDeviceName("NiclaStream");
    streamService.addCharacteristic(sampleChar);
    BLE.addService(streamService);
    BLE.setAdvertisedService(streamService);
    BLE.advertise();
  }
#endif

  timeOrigin = millis();
  nextSample = timeOrigin;
  printHeader();
}

// ---------------------------------------------------------------------------
// Sampling
// ---------------------------------------------------------------------------

// Fills out[] with the 24 physical values that follow seq and t_ms, in column order.
static void readSample(float *out) {
  out[0]  = accel.x() * ACCEL_SCALE;
  out[1]  = accel.y() * ACCEL_SCALE;
  out[2]  = accel.z() * ACCEL_SCALE;

  out[3]  = gyro.x() * GYRO_SCALE;
  out[4]  = gyro.y() * GYRO_SCALE;
  out[5]  = gyro.z() * GYRO_SCALE;

  out[6]  = magn.x() * MAG_SCALE;
  out[7]  = magn.y() * MAG_SCALE;
  out[8]  = magn.z() * MAG_SCALE;

  out[9]  = quat.x();
  out[10] = quat.y();
  out[11] = quat.z();
  out[12] = quat.w();

  out[13] = ori.heading();
  out[14] = ori.pitch();
  out[15] = ori.roll();

  out[16] = temp.value();
  out[17] = hum.value();
  out[18] = baro.value();
  out[19] = gas.value();

  out[20] = (float)bsec.iaq();
  out[21] = (float)bsec.iaq_s();
  out[22] = (float)bsec.co2_eq();
  out[23] = bsec.b_voc_eq();
}

// Decimal places per value, matched to each sensor's real resolution so the line stays
// short without discarding precision.
static const uint8_t DECIMALS[24] = {
  4, 4, 4,        // accel g
  3, 3, 3,        // gyro dps
  2, 2, 2,        // mag uT
  5, 5, 5, 5,     // quaternion
  2, 2, 2,        // orientation deg
  2, 2, 2, 0,     // temp, humidity, pressure, gas ohm
  0, 0, 0, 2      // iaq, iaq_s, co2_eq, bvoc_eq
};

// Formats into RAM so the finished line leaves in one write(). The core's Serial is an
// mbed UnbufferedSerial: every write() call busy-waits on the UART, so printing field by
// field spends ~16 us per byte spinning instead of ~10. Batching buys back the margin
// that 200 Hz needs.
class LineBuffer : public Print {
public:
  LineBuffer(uint8_t *buffer, size_t capacity) : buf(buffer), cap(capacity), len(0) {}
  void reset() { len = 0; }
  size_t write(uint8_t c) { if (len < cap) buf[len++] = c; return 1; }

  uint8_t *buf;
  size_t   cap;
  size_t   len;
};

static uint8_t    lineBytes[256];
static LineBuffer line(lineBytes, sizeof(lineBytes));

static void printCsv(uint32_t seq, uint32_t tMs, const float *values) {
  line.reset();
  line.print(seq);
  line.print(',');
  line.print(tMs);
  for (uint8_t i = 0; i < 24; i++) {
    line.print(',');
    line.print(values[i], DECIMALS[i]);
  }
  line.print(',');
  line.println(bsec.accuracy());
  Serial.write(lineBytes, line.len);
}

// Applies a new streaming rate, clamped to what the hub actually delivers, and
// realigns the sample grid so the next sample fires immediately rather than
// waiting out whatever period was already in progress.
static void setStreamRate(uint32_t hz) {
  if (hz < 1) hz = 1;
  if (hz > (uint32_t)MOTION_RATE_HZ) hz = (uint32_t)MOTION_RATE_HZ;
  streamHz       = hz;
  streamPeriodMs = 1000 / streamHz;
  nextSample     = millis();
}

// Reopens Serial at a new baud. Only meaningful as a value the host's CDC-ACM driver
// reads back (see the serialBaud comment above) -- the host must reconnect afterwards.
static void setBaud(uint32_t baud) {
  if (baud < 9600) baud = 9600;
  if (baud > 1000000) baud = 1000000;
  serialBaud = baud;
  Serial.end();
  delay(10);
  Serial.begin(serialBaud);
}

// '\0' when idle, otherwise the command ('s' or 'b') whose digit argument is
// currently being accumulated.
static char     pendingCmd = '\0';
static uint32_t cmdArg     = 0;

static void handleCommands() {
  while (Serial.available()) {
    char c = Serial.read();

    if (pendingCmd) {
      if (c >= '0' && c <= '9') {
        cmdArg = cmdArg * 10 + (c - '0');
        continue;
      }
      if (cmdArg > 0) {
        if (pendingCmd == 's') setStreamRate(cmdArg);
        else if (pendingCmd == 'b') setBaud(cmdArg);
      }
      pendingCmd = '\0';
      // fall through so a non-digit terminator can still be acted on below
    }

    switch (c) {
      case 'h':
        printHeader();
        break;
      case 'r':
        sequence   = 0;
        timeOrigin = millis();
        nextSample = timeOrigin;
        break;
      case 's':
      case 'b':
        pendingCmd = c;
        cmdArg     = 0;
        break;
      default:
        break;
    }
  }
}

void loop() {
  BHY2.update();
  handleCommands();

  uint32_t now = millis();
  if ((int32_t)(now - nextSample) >= 0) {
    float values[24];
    readSample(values);
    printCsv(sequence, now - timeOrigin, values);
    sequence++;

    // Advance on the fixed grid, resynchronising if we ever fall a full period behind.
    nextSample += streamPeriodMs;
    if ((int32_t)(now - nextSample) >= 0) {
      nextSample = now + streamPeriodMs;
    }

#if STREAM_BLE
    if ((int32_t)(now - nextBleSample) >= 0) {
      BleSample sample;
      sample.seq  = sequence;
      sample.t_ms = now - timeOrigin;
      memcpy(sample.values, values, sizeof(values));
      sample.values[24] = (float)bsec.accuracy();
      sampleChar.writeValue(&sample, sizeof(sample));
      nextBleSample = now + BLE_STREAM_PERIOD_MS;
    }
#endif
  }

#if STREAM_BLE
  BLE.poll();
#endif
}
