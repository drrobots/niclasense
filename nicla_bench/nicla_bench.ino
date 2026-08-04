/*
 * nicla_bench — throughput benchmark for Nicla Sense ME serial streaming.
 *
 * Answers "how fast can this link actually go, and what is the limit?" by free-running
 * the sample loop with no rate limit, so the host measures each encoding's ceiling
 * rather than a rate we picked. Also reports on-MCU formatting cost and the real hub
 * output rates. Drive it with python/bench/runbench.py.
 *
 * Findings this sketch produced (nRF52832 UARTE at 1 Mbaud, its hardware maximum):
 *
 *   encoding              B/sample   max Hz core Serial   max Hz + EasyDMA
 *   CSV full, 27 col         162            238                588
 *   CSV ragged               119            310                732
 *   CSV motion only          119            303                732
 *   binary, 42 B frame        42           1288               2333  (98% of wire)
 *
 * Two ceilings sit below the wire. The core's Serial is an mbed UnbufferedSerial that
 * busy-waits per byte (~15.4 us/B vs the wire's 10.0), capping the port near 65 kB/s at
 * any baud; and Print::print(float) costs ~1464 us for 27 columns, capping CSV near
 * 680 Hz on its own. Binary cuts formatting to ~20 us -- a 73x CPU saving, which matters
 * more than the byte count.
 *
 * None of it is the binding constraint in practice: the BHI260AP tops out near 200 Hz
 * for accel/gyro/quaternion/orientation (requesting 400+ still yields ~197), the
 * magnetometer at 50 Hz, and the BME688/BSEC fusion at ~1 Hz. Hence nicla_stream's
 * 200 Hz, which needs only 33% of a 1 Mbaud link in plain CSV.
 *
 * Commands (single char):
 *   0  idle
 *   L  link blast: fixed 64-byte lines, no sensor work   -> pure link capacity
 *   A  CSV, all 27 columns (nicla_stream's format)
 *   B  CSV, ragged: env columns appended only when fresh
 *   M  CSV, motion only (18 columns, env never sent)
 *   C  binary: 42-byte motion frame + 37-byte env frame when fresh
 *   D  binary, written via EasyDMA
 *   E  CSV full, written via EasyDMA
 *   F  CSV ragged, written via EasyDMA
 *   G  CSV motion only, written via EasyDMA
 *   P  profile: per-encoder formatting cost with output discarded, plus a write-size
 *      sweep comparing the core's write path against EasyDMA
 *   O  ODR probe: measured hub output rate per sensor over 3 s
 *   U  dump the UARTE registers (mode, programmed baud, TX pin)
 *   S<n> set motion sensor rate to n Hz (e.g. "S400")
 *
 * The D/E/F/G modes drive NRF_UARTE0 directly, behind the back of mbed's driver. That
 * needs the TX interrupts masked first (dmaTakeover) or mbed's ISR eats EVENTS_ENDTX and
 * the poll below deadlocks. EasyDMA on the nRF52832 also caps TXD.MAXCNT at 8 bits, so a
 * single transfer cannot exceed 255 bytes.
 */

#include "Arduino_BHY2.h"
#include "nrf.h"

#ifndef BENCH_BAUD
#define BENCH_BAUD 1000000
#endif
#ifndef MOTION_HZ
#define MOTION_HZ 100
#endif
#define ENV_HZ 1.0f

static const uint16_t ACCEL_RANGE_G  = 4;
static const uint16_t GYRO_RANGE_DPS = 2000;
static const float ACCEL_SCALE = (float)ACCEL_RANGE_G / 32768.0f;
static const float GYRO_SCALE  = (float)GYRO_RANGE_DPS / 32768.0f;
static const float MAG_SCALE   = 1.0f / 16.0f;

static const float QUAT_LSB = 0.000061035f;  // SensorQuaternion factor
static const float ORI_LSB  = 0.01098f;      // SensorOrientation factor

SensorXYZ         accel(SENSOR_ID_ACC);
SensorXYZ         gyro (SENSOR_ID_GYRO);
SensorXYZ         magn (SENSOR_ID_MAG);
SensorQuaternion  quat (SENSOR_ID_RV);
SensorOrientation ori  (SENSOR_ID_ORI);
Sensor            temp (SENSOR_ID_TEMP);
Sensor            baro (SENSOR_ID_BARO);
Sensor            hum  (SENSOR_ID_HUM);
Sensor            gas  (SENSOR_ID_GAS);
SensorBSEC        bsec (SENSOR_ID_BSEC);

// ---------------------------------------------------------------------------
// Print sinks
// ---------------------------------------------------------------------------

// Formats into a RAM buffer so the sample can leave in a single blocking write().
class BufPrint : public Print {
public:
  uint8_t *buf;
  size_t   cap;
  size_t   len;
  BufPrint(uint8_t *b, size_t c) : buf(b), cap(c), len(0) {}
  void reset() { len = 0; }
  size_t write(uint8_t c) { if (len < cap) buf[len++] = c; return 1; }
};

// Discards everything; used to time formatting without the UART in the way.
class NullPrint : public Print {
public:
  size_t n;
  NullPrint() : n(0) {}
  size_t write(uint8_t) { n++; return 1; }
};

static uint8_t  txbuf[512];
static BufPrint out(txbuf, sizeof(txbuf));
static NullPrint devnull;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

static char     mode       = '0';
static uint32_t sequence   = 0;
static uint32_t timeOrigin = 0;
static float    motionRate = MOTION_HZ;

static const uint8_t DEC_MOTION[16] = {
  4, 4, 4,     // accel g
  3, 3, 3,     // gyro dps
  2, 2, 2,     // mag uT
  5, 5, 5, 5,  // quaternion
  2, 2, 2      // orientation deg
};
static const uint8_t DEC_ENV[8] = { 2, 2, 2, 0, 0, 0, 0, 2 };

static void motionValues(float *v) {
  v[0]  = accel.x() * ACCEL_SCALE;
  v[1]  = accel.y() * ACCEL_SCALE;
  v[2]  = accel.z() * ACCEL_SCALE;
  v[3]  = gyro.x() * GYRO_SCALE;
  v[4]  = gyro.y() * GYRO_SCALE;
  v[5]  = gyro.z() * GYRO_SCALE;
  v[6]  = magn.x() * MAG_SCALE;
  v[7]  = magn.y() * MAG_SCALE;
  v[8]  = magn.z() * MAG_SCALE;
  v[9]  = quat.x();
  v[10] = quat.y();
  v[11] = quat.z();
  v[12] = quat.w();
  v[13] = ori.heading();
  v[14] = ori.pitch();
  v[15] = ori.roll();
}

static void envValues(float *v) {
  v[0] = temp.value();
  v[1] = hum.value();
  v[2] = baro.value();
  v[3] = gas.value();
  v[4] = (float)bsec.iaq();
  v[5] = (float)bsec.iaq_s();
  v[6] = (float)bsec.co2_eq();
  v[7] = bsec.b_voc_eq();
}

// True when any of the ~1 Hz sensors produced a new reading since the last check.
static bool envFresh() {
  bool fresh = temp.dataAvailable() || hum.dataAvailable() ||
               baro.dataAvailable() || gas.dataAvailable() || bsec.dataAvailable();
  if (fresh) {
    temp.clearDataAvailFlag();
    hum.clearDataAvailFlag();
    baro.clearDataAvailFlag();
    gas.clearDataAvailFlag();
    bsec.clearDataAvailFlag();
  }
  return fresh;
}

// ---------------------------------------------------------------------------
// Encoders — each fills `sink` and returns nothing; caller ships it.
// ---------------------------------------------------------------------------

static void encodeCsv(Print &sink, uint32_t seq, uint32_t t, const float *m,
                      const float *e, bool withEnv) {
  sink.print(seq);
  sink.print(',');
  sink.print(t);
  for (uint8_t i = 0; i < 16; i++) {
    sink.print(',');
    sink.print(m[i], DEC_MOTION[i]);
  }
  if (withEnv) {
    for (uint8_t i = 0; i < 8; i++) {
      sink.print(',');
      sink.print(e[i], DEC_ENV[i]);
    }
    sink.print(',');
    sink.print(bsec.accuracy());
  }
  sink.print('\n');
}

static inline void put16(uint8_t *b, size_t &n, int16_t v) {
  b[n++] = (uint8_t)(v & 0xFF);
  b[n++] = (uint8_t)((v >> 8) & 0xFF);
}
static inline void put32(uint8_t *b, size_t &n, uint32_t v) {
  b[n++] = (uint8_t)(v & 0xFF);
  b[n++] = (uint8_t)((v >> 8) & 0xFF);
  b[n++] = (uint8_t)((v >> 16) & 0xFF);
  b[n++] = (uint8_t)((v >> 24) & 0xFF);
}
static inline void putf(uint8_t *b, size_t &n, float f) {
  uint32_t u;
  memcpy(&u, &f, 4);
  put32(b, n, u);
}

// 42-byte motion frame. Accel/gyro/mag go out as the hub's own int16 counts, so the
// MCU does no scaling at all; quaternion and orientation are converted back to the
// int16 the hub actually sent.
static size_t encodeBinMotion(uint8_t *b, uint32_t seq, uint32_t t) {
  size_t n = 0;
  b[n++] = 0xAA;
  b[n++] = 0x55;
  b[n++] = 0x01;
  put16(b, n, (int16_t)(seq & 0xFFFF));
  put32(b, n, t);
  put16(b, n, accel.x()); put16(b, n, accel.y()); put16(b, n, accel.z());
  put16(b, n, gyro.x());  put16(b, n, gyro.y());  put16(b, n, gyro.z());
  put16(b, n, magn.x());  put16(b, n, magn.y());  put16(b, n, magn.z());
  put16(b, n, (int16_t)lroundf(quat.x() / QUAT_LSB));
  put16(b, n, (int16_t)lroundf(quat.y() / QUAT_LSB));
  put16(b, n, (int16_t)lroundf(quat.z() / QUAT_LSB));
  put16(b, n, (int16_t)lroundf(quat.w() / QUAT_LSB));
  put16(b, n, (int16_t)lroundf(ori.heading() / ORI_LSB));
  put16(b, n, (int16_t)lroundf(ori.pitch()   / ORI_LSB));
  put16(b, n, (int16_t)lroundf(ori.roll()    / ORI_LSB));
  uint8_t x = 0;
  for (size_t i = 2; i < n; i++) x ^= b[i];
  b[n++] = x;
  return n;
}

static size_t encodeBinEnv(uint8_t *b, uint32_t seq, uint32_t t, const float *e) {
  size_t n = 0;
  b[n++] = 0xAA;
  b[n++] = 0x55;
  b[n++] = 0x02;
  put16(b, n, (int16_t)(seq & 0xFFFF));
  put32(b, n, t);
  putf(b, n, e[0]); putf(b, n, e[1]); putf(b, n, e[2]); putf(b, n, e[3]);
  put16(b, n, (int16_t)e[4]); put16(b, n, (int16_t)e[5]); put16(b, n, (int16_t)e[6]);
  putf(b, n, e[7]);
  b[n++] = (uint8_t)bsec.accuracy();
  uint8_t x = 0;
  for (size_t i = 2; i < n; i++) x ^= b[i];
  b[n++] = x;
  return n;
}

// ---------------------------------------------------------------------------
// Direct UARTE EasyDMA write
//
// The Arduino core's Serial is an mbed UnbufferedSerial, which pushes bytes through
// a 32-byte FIFO one nrfx transaction at a time. This hands the whole buffer to
// EasyDMA in a single transfer instead. `buf` must live in RAM.
// ---------------------------------------------------------------------------

static void uarteDump() {
  Serial.flush();
  delay(2);
  Serial.print("# uarte enable=");
  Serial.print(NRF_UARTE0->ENABLE);          // 8 = UARTE, 4 = legacy UART
  Serial.print(" baudrate=0x");
  Serial.print(NRF_UARTE0->BAUDRATE, HEX);
  Serial.print(" psel_txd=");
  Serial.print(NRF_UARTE0->PSEL.TXD);
  Serial.print(" uart0_enable=");
  Serial.println(NRF_UART0->ENABLE);
}

// mbed's driver enables the ENDTX interrupt and its ISR clears the event, so a
// naive poll on EVENTS_ENDTX can spin forever. Mask only the TX interrupts while
// we own the transmitter; RX must keep working or the board stops hearing commands.
#define UARTE_INT_ENDTX     (1UL << 8)
#define UARTE_INT_TXSTOPPED (1UL << 22)
#define UARTE_INT_TXDRDY    (1UL << 7)
#define UARTE_TX_INTS  (UARTE_INT_ENDTX | UARTE_INT_TXSTOPPED | UARTE_INT_TXDRDY)

static uint32_t dmaSavedInten = 0;
static bool     dmaOwned      = false;
static uint32_t dmaTimeouts   = 0;

static void dmaTakeover() {
  if (dmaOwned) return;
  Serial.flush();
  delay(2);
  dmaSavedInten = NRF_UARTE0->INTENSET;
  NRF_UARTE0->INTENCLR = UARTE_TX_INTS;
  NRF_UARTE0->EVENTS_ENDTX = 0;
  dmaOwned = true;
}

static void dmaRelease() {
  if (!dmaOwned) return;
  NRF_UARTE0->EVENTS_ENDTX = 0;
  NRF_UARTE0->INTENSET = dmaSavedInten & UARTE_TX_INTS;
  dmaOwned = false;
}

static inline void dmaStart(const uint8_t *buf, size_t len) {
  NRF_UARTE0->EVENTS_ENDTX  = 0;
  NRF_UARTE0->TXD.PTR       = (uint32_t)buf;
  NRF_UARTE0->TXD.MAXCNT    = len;
  NRF_UARTE0->TASKS_STARTTX = 1;
}

// Bounded so a driver conflict shows up as a counter instead of a hung board.
static inline bool dmaWait() {
  uint32_t t0 = micros();
  while (NRF_UARTE0->EVENTS_ENDTX == 0) {
    if ((uint32_t)(micros() - t0) > 20000) { dmaTimeouts++; return false; }
  }
  NRF_UARTE0->EVENTS_ENDTX = 0;
  return true;
}

static inline bool dmaWrite(const uint8_t *buf, size_t len) {
  dmaStart(buf, len);
  return dmaWait();
}

// Double buffer so the next sample can be built while the previous one is on the wire.
static uint8_t dmabuf[2][512];
static uint8_t dmaSlot = 0;
static bool    dmaBusy = false;

// ---------------------------------------------------------------------------
// Reports
// ---------------------------------------------------------------------------

static void profile() {
  const uint16_t N = 500;
  float m[16], e[8];
  motionValues(m);
  envValues(e);

  uint32_t t0, dt;

  t0 = micros();
  for (uint16_t i = 0; i < N; i++) BHY2.update();
  dt = micros() - t0;
  Serial.print("# bhy2_update_us=");
  Serial.println((float)dt / N, 2);

  t0 = micros();
  for (uint16_t i = 0; i < N; i++) motionValues(m);
  dt = micros() - t0;
  Serial.print("# read_motion_us=");
  Serial.println((float)dt / N, 2);

  devnull.n = 0;
  t0 = micros();
  for (uint16_t i = 0; i < N; i++) encodeCsv(devnull, i, i * 20, m, e, true);
  dt = micros() - t0;
  Serial.print("# fmt_csv_full_us=");
  Serial.print((float)dt / N, 2);
  Serial.print(" bytes=");
  Serial.println((float)devnull.n / N, 1);

  devnull.n = 0;
  t0 = micros();
  for (uint16_t i = 0; i < N; i++) encodeCsv(devnull, i, i * 20, m, e, false);
  dt = micros() - t0;
  Serial.print("# fmt_csv_motion_us=");
  Serial.print((float)dt / N, 2);
  Serial.print(" bytes=");
  Serial.println((float)devnull.n / N, 1);

  uint8_t tmp[64];
  size_t len = 0;
  t0 = micros();
  for (uint16_t i = 0; i < N; i++) len = encodeBinMotion(tmp, i, i * 20);
  dt = micros() - t0;
  Serial.print("# fmt_bin_motion_us=");
  Serial.print((float)dt / N, 2);
  Serial.print(" bytes=");
  Serial.println(len);

  t0 = micros();
  for (uint16_t i = 0; i < N; i++) len = encodeBinEnv(tmp, i, i * 20, e);
  dt = micros() - t0;
  Serial.print("# fmt_bin_env_us=");
  Serial.print((float)dt / N, 2);
  Serial.print(" bytes=");
  Serial.println(len);

  // Cost of pushing bytes out the UART, by transfer size and by write path.
  // 10 bits per byte, so the wire itself costs 1e7/baud us per byte.
  memset(dmabuf[0], 'x', sizeof(dmabuf[0]));
  Serial.print("\n# wire_us_per_byte=");
  Serial.println(10000000.0f / (float)BENCH_BAUD, 3);

  const uint16_t sizes[5] = { 16, 42, 64, 170, 512 };
  for (uint8_t s = 0; s < 5; s++) {
    uint16_t sz = sizes[s];
    uint16_t reps = (sz > 200) ? 100 : N;

    t0 = micros();
    for (uint16_t i = 0; i < reps; i++) Serial.write(dmabuf[0], sz);
    dt = micros() - t0;
    float coreUs = (float)dt / reps;

    dmaTakeover();
    t0 = micros();
    for (uint16_t i = 0; i < reps; i++) dmaWrite(dmabuf[0], sz);
    dt = micros() - t0;
    dmaRelease();
    float dmaUs = (float)dt / reps;

    Serial.print("# write n=");
    Serial.print(sz);
    Serial.print(" core_us=");
    Serial.print(coreUs, 1);
    Serial.print(" (");
    Serial.print(coreUs / sz, 3);
    Serial.print(" us/B)  dma_us=");
    Serial.print(dmaUs, 1);
    Serial.print(" (");
    Serial.print(dmaUs / sz, 3);
    Serial.println(" us/B)");
  }

  Serial.print("# baud=");
  Serial.print(BENCH_BAUD);
  Serial.print(" motion_rate_hz=");
  Serial.print(motionRate);
  Serial.print(" dma_timeouts=");
  Serial.println(dmaTimeouts);
}

static void odrProbe() {
  uint32_t nAcc = 0, nGyr = 0, nMag = 0, nQuat = 0, nOri = 0, nEnv = 0, loops = 0;
  accel.clearDataAvailFlag(); gyro.clearDataAvailFlag(); magn.clearDataAvailFlag();
  quat.clearDataAvailFlag();  ori.clearDataAvailFlag();
  envFresh();

  uint32_t t0 = millis();
  while (millis() - t0 < 3000) {
    BHY2.update();
    loops++;
    if (accel.dataAvailable()) { nAcc++;  accel.clearDataAvailFlag(); }
    if (gyro.dataAvailable())  { nGyr++;  gyro.clearDataAvailFlag(); }
    if (magn.dataAvailable())  { nMag++;  magn.clearDataAvailFlag(); }
    if (quat.dataAvailable())  { nQuat++; quat.clearDataAvailFlag(); }
    if (ori.dataAvailable())   { nOri++;  ori.clearDataAvailFlag(); }
    if (envFresh())            { nEnv++; }
  }
  uint32_t el = millis() - t0;

  Serial.print("# odr requested=");
  Serial.print(motionRate);
  Serial.print(" loops_per_s=");
  Serial.println(loops * 1000.0f / el, 1);
  Serial.print("# acc=");  Serial.print(nAcc * 1000.0f / el, 1);
  Serial.print(" gyro="); Serial.print(nGyr * 1000.0f / el, 1);
  Serial.print(" mag=");  Serial.print(nMag * 1000.0f / el, 1);
  Serial.print(" quat="); Serial.print(nQuat * 1000.0f / el, 1);
  Serial.print(" ori=");  Serial.print(nOri * 1000.0f / el, 1);
  Serial.print(" env=");  Serial.println(nEnv * 1000.0f / el, 2);
}

static void setMotionRate(float hz) {
  motionRate = hz;
  accel.configure(hz, 1);
  gyro.configure(hz, 1);
  magn.configure(hz, 1);
  quat.configure(hz, 1);
  ori.configure(hz, 1);
  Serial.print("# motion_rate_hz=");
  Serial.println(hz);
}

// ---------------------------------------------------------------------------

static void handleCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'S') {
      // "S400" — digits terminated by any non-digit.
      float hz = 0;
      uint32_t deadline = millis() + 100;
      while (millis() < deadline) {
        if (!Serial.available()) continue;
        char d = Serial.read();
        if (d < '0' || d > '9') break;
        hz = hz * 10 + (d - '0');
      }
      if (hz > 0) setMotionRate(hz);
      continue;
    }
    if (c == 'P') { profile(); mode = '0'; continue; }
    if (c == 'O') { odrProbe(); mode = '0'; continue; }
    if (c == 'U') { uarteDump(); mode = '0'; continue; }
    if (c == '0' || c == 'L' || c == 'A' || c == 'B' || c == 'M' || c == 'C' ||
        c == 'D' || c == 'E' || c == 'F' || c == 'G') {
      if (dmaBusy) { dmaWait(); dmaBusy = false; }
      dmaRelease();
      mode = c;
      sequence = 0;
      timeOrigin = millis();
      if (c == 'D' || c == 'E' || c == 'F' || c == 'G') dmaTakeover();
    }
  }
}

void setup() {
  Serial.begin(BENCH_BAUD);
  BHY2.begin(NICLA_STANDALONE);

  accel.begin(MOTION_HZ); gyro.begin(MOTION_HZ); magn.begin(MOTION_HZ);
  quat.begin(MOTION_HZ);  ori.begin(MOTION_HZ);
  temp.begin(ENV_HZ); baro.begin(ENV_HZ); hum.begin(ENV_HZ);
  gas.begin(ENV_HZ);  bsec.begin(ENV_HZ);

  accel.setRange(ACCEL_RANGE_G);
  gyro.setRange(GYRO_RANGE_DPS);

  timeOrigin = millis();
  Serial.print("# bench ready baud=");
  Serial.println(BENCH_BAUD);
}

void loop() {
  BHY2.update();
  handleCommands();
  if (mode == '0') return;

  uint32_t t = millis() - timeOrigin;
  float m[16], e[8];

  switch (mode) {
    case 'L': {
      // 64 bytes: sequence, then filler. No sensor access at all.
      out.reset();
      out.print(sequence);
      while (out.len < 63) out.write('x');
      out.write('\n');
      Serial.write(txbuf, out.len);
      break;
    }
    case 'A':
      motionValues(m);
      envValues(e);
      out.reset();
      encodeCsv(out, sequence, t, m, e, true);
      Serial.write(txbuf, out.len);
      break;
    case 'B': {
      motionValues(m);
      bool fresh = envFresh();
      if (fresh) envValues(e);
      out.reset();
      encodeCsv(out, sequence, t, m, e, fresh);
      Serial.write(txbuf, out.len);
      break;
    }
    case 'M':
      motionValues(m);
      out.reset();
      encodeCsv(out, sequence, t, m, e, false);
      Serial.write(txbuf, out.len);
      break;
    case 'C': {
      size_t n = encodeBinMotion(txbuf, sequence, t);
      Serial.write(txbuf, n);
      if (envFresh()) {
        envValues(e);
        n = encodeBinEnv(txbuf, sequence, t, e);
        Serial.write(txbuf, n);
      }
      break;
    }
    case 'D': {
      // Binary over EasyDMA, double buffered: build sample N+1 while N is on the wire.
      uint8_t *b = dmabuf[dmaSlot];
      size_t n = encodeBinMotion(b, sequence, t);
      if (envFresh()) {
        envValues(e);
        n += encodeBinEnv(b + n, sequence, t, e);
      }
      if (dmaBusy) dmaWait();
      dmaStart(b, n);
      dmaBusy = true;
      dmaSlot ^= 1;
      break;
    }
    case 'E': {
      // Full 27-column CSV over EasyDMA, double buffered.
      motionValues(m);
      envValues(e);
      BufPrint bp(dmabuf[dmaSlot], sizeof(dmabuf[0]));
      encodeCsv(bp, sequence, t, m, e, true);
      if (dmaBusy) dmaWait();
      dmaStart(dmabuf[dmaSlot], bp.len);
      dmaBusy = true;
      dmaSlot ^= 1;
      break;
    }
    case 'F': {
      // Ragged CSV over EasyDMA.
      motionValues(m);
      bool fresh = envFresh();
      if (fresh) envValues(e);
      BufPrint bp(dmabuf[dmaSlot], sizeof(dmabuf[0]));
      encodeCsv(bp, sequence, t, m, e, fresh);
      if (dmaBusy) dmaWait();
      dmaStart(dmabuf[dmaSlot], bp.len);
      dmaBusy = true;
      dmaSlot ^= 1;
      break;
    }
    case 'G': {
      // Motion-only CSV over EasyDMA.
      motionValues(m);
      BufPrint bp(dmabuf[dmaSlot], sizeof(dmabuf[0]));
      encodeCsv(bp, sequence, t, m, e, false);
      if (dmaBusy) dmaWait();
      dmaStart(dmabuf[dmaSlot], bp.len);
      dmaBusy = true;
      dmaSlot ^= 1;
      break;
    }
  }
  sequence++;
}
