#include <Arduino.h>
#include <Wire.h>
#include <math.h>

// ---- pins ----
#define SDA_PIN PA12
#define SCL_PIN PA11
#define ALERT_PIN PA15
#define BUZZER_PIN PB3
#define MOSFET_PIN PB14
#define TEMP_PIN PA4
#define H2_PIN PA5

HardwareSerial Serial1(PA3, PA2);

// ---- INA226 registers ----
#define INA226_ADDR 0x44
#define REG_CONFIG 0x00
#define REG_SHUNT 0x01
#define REG_BUS 0x02
#define REG_POWER 0x03
#define REG_CURRENT 0x04
#define REG_CAL 0x05
#define REG_MASK 0x06
#define REG_ALERT 0x07

// ==================== EDIT THESE TO RECONFIGURE ====================
const float SHUNT_OHMS = 0.003f;     // your shunt
float BUS_VOLTAGE_LIMIT = 0.8f;      // runtime-configurable (SET_V)
const float MAX_CURRENT_A = 20.0f;   // calibration full-scale
float OCP_LIMIT_A = 16.0f;           // cutoff current, runtime-configurable (SET_IH)
float CURRENT_LOWER_LIMIT_A = 0.0f;  // runtime-configurable (SET_IL), 0 = disabled
float TEMP_HIGH_LIMIT = 45.0f;       // deg C, runtime-configurable (SET_TH)
float TEMP_LOW_LIMIT = 25.0f;        // deg C, runtime-configurable (SET_TL)
float H2_LIMIT_V = 4.00f;            // runtime-configurable (SET_H2)
const float NTC_BETA = 3950.0f;
const float NTC_R_FIXED = 100000.0f;      // divider resistor
const float NTC_R0 = 100000.0f;           // NTC resistance @ 25C
const float NTC_T0 = 298.15f;             // 25C in Kelvin
float TEMP_OFFSET = 0.0f;                 // calibrate against a reference thermometer
uint32_t CSV_PERIOD_MS = 1000;            // csv print rate, runtime-configurable (SET_RATE)
const uint32_t BUZZ_PERIOD_MS = 500;      // buz-silent-buz cadence
const bool ENABLE_UART_TELEMETRY = true;  // Set to true to enable, false to disable
const uint32_t CONFIG_WINDOW_MS = 60000;  // window (from boot) during which SET_* commands are accepted
// =====================================================================

const float CURRENT_LSB = MAX_CURRENT_A / 32768.0f;
const float POWER_LSB = 25.0f * CURRENT_LSB;
const uint16_t CAL_REG = (uint16_t)(0.00512f / (CURRENT_LSB * SHUNT_OHMS));

bool faultLatched = false;
bool mosfetOn = false;
bool buzzState = false;
uint32_t lastBuzz = 0;
uint32_t lastCsv = 0;
bool inConfigMode = false;
uint32_t bootTime = 0;

uint8_t faultReason = 0;
String rxLineBuffer = "";

uint16_t computeAlertReg() {
  return (uint16_t)((OCP_LIMIT_A * SHUNT_OHMS) / 0.0000025f);
}

// Fault codes (must match FAULT_MESSAGES in the PC logger exactly):
// 0 = No fault
// 1 = INA226 ALERT pin
// 2 = Bus voltage
// 3 = Over current
// 4 = Hydrogen
// 5 = Temperature
// 6 = Under current

void ina226Write(uint8_t reg, uint16_t val) {
  Wire.beginTransmission(INA226_ADDR);
  Wire.write(reg);
  Wire.write((uint8_t)(val >> 8));
  Wire.write((uint8_t)(val & 0xFF));
  Wire.endTransmission();
}

int16_t ina226Read(uint8_t reg) {
  Wire.beginTransmission(INA226_ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom(INA226_ADDR, (uint8_t)2);
  uint16_t val = ((uint16_t)Wire.read() << 8) | Wire.read();
  return (int16_t)val;
}

void ina226Init() {
  Wire.setSDA(SDA_PIN);
  Wire.setSCL(SCL_PIN);
  Wire.begin();
  ina226Write(REG_CONFIG, 0x4127);  // default conv times, continuous mode
  ina226Write(REG_CAL, CAL_REG);
  ina226Write(REG_ALERT, computeAlertReg());  // shunt-over-limit threshold
  ina226Write(REG_MASK, 0x8001);              // enable SOL alert + latch
}

float readBusVoltage() {
  const float BUS_DEADBAND = 0.300f;  // Ignore anything below 300 mV

  float voltage = ina226Read(REG_BUS) * 0.00125f;

  if (voltage < BUS_DEADBAND)
    voltage = 0.0f;

  return voltage;
}
float readCurrent() {
  const uint8_t FILTER_SIZE = 16;
  const float CURRENT_DEADBAND = 0.003f;

  static float buffer[FILTER_SIZE];
  static uint8_t index = 0;
  static bool filled = false;

  float current = ina226Read(REG_CURRENT) * CURRENT_LSB;

  buffer[index] = current;

  index++;

  if (index >= FILTER_SIZE) {
    index = 0;
    filled = true;
  }

  uint8_t count = filled ? FILTER_SIZE : index;

  float sum = 0;

  for (uint8_t i = 0; i < count; i++)
    sum += buffer[i];

  current = sum / count;

  if (fabs(current) < CURRENT_DEADBAND)
    current = 0.0f;

  return current;
}
float readPower() {
  return ina226Read(REG_POWER) * POWER_LSB;
}
float readShuntMv() {
  return ina226Read(REG_SHUNT) * 0.0025f;
}

float readTemperature() {
  const uint16_t samples = 128;
  uint32_t adcSum = 0;
  for (uint16_t i = 0; i < samples; i++) {
    adcSum += analogRead(TEMP_PIN);
    delayMicroseconds(200);
  }

  float adc = (float)adcSum / samples;
  if (adc <= 1) adc = 1;
  if (adc >= 4094) adc = 4094;

  float resistance = NTC_R_FIXED * (adc / (4095.0f - adc));
  float tempK = 1.0f / ((1.0f / NTC_T0) + (1.0f / NTC_BETA) * log(resistance / NTC_R0));

  return (tempK - 273.15f) + TEMP_OFFSET;
}

float readH2() {
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) {
    sum += analogRead(H2_PIN);
    delayMicroseconds(100);
  }
  float adc = sum / 16.0f;
  return adc * 3.3f / 4095.0f;
}

void checkFaults(float busV, float current, float temp, float h2v) {
  bool configWindowOpen = (millis() - bootTime <= CONFIG_WINDOW_MS);

  if (configWindowOpen) {
    // Re-arm the INA226's internal alert latch every cycle so a raised or
    // lowered OCP limit takes effect immediately instead of staying tripped
    // from an earlier (now-stale) threshold. Reading MASK/ENABLE clears the
    // chip's alert flag; it re-asserts on the very next conversion if the
    // condition is still genuinely violated.
    ina226Read(REG_MASK);
  }

  bool alertActive = (digitalRead(ALERT_PIN) == LOW);

  // Default = no fault
  faultReason = 0;

  // Priority order
  if (alertActive) {
    faultReason = 1;
  } else if (busV < BUS_VOLTAGE_LIMIT) {
    faultReason = 2;
  } else if (current > OCP_LIMIT_A) {
    faultReason = 3;
  } else if (CURRENT_LOWER_LIMIT_A > 0.0f && current < CURRENT_LOWER_LIMIT_A) {
    faultReason = 6;
  } else if (h2v > H2_LIMIT_V) {
    faultReason = 4;
  } else if (temp > TEMP_HIGH_LIMIT || temp < TEMP_LOW_LIMIT) {
    faultReason = 5;
  }

  if (configWindowOpen) {
    // Non-latching while tuning: MOSFET/fault state track the live
    // comparison against whatever limits are currently set, so widening
    // a limit clears the fault immediately instead of requiring a reset.
    faultLatched = (faultReason != 0);
  } else {
    // Normal latching behavior once tuning is over: a fault, once
    // tripped, stays tripped until the board is physically reset.
    if (faultReason != 0)
      faultLatched = true;
  }

  mosfetOn = faultLatched;
  digitalWrite(MOSFET_PIN, mosfetOn ? HIGH : LOW);

  if (faultLatched) {
    if (millis() - lastBuzz >= BUZZ_PERIOD_MS) {
      lastBuzz = millis();
      buzzState = !buzzState;
      digitalWrite(BUZZER_PIN, buzzState ? HIGH : LOW);
    }
  } else {
    digitalWrite(BUZZER_PIN, LOW);
  }
}

void applySerialCommand(String line) {
  line.trim();
  if (line.length() == 0) return;

  bool windowOpen = (millis() - bootTime <= CONFIG_WINDOW_MS);
  if (!windowOpen) {
    Serial1.println("[WARN] Configuration window closed (60s elapsed). Reset the BMS to reconfigure.");
    return;
  }

  if (line == "1") {
    inConfigMode = true;
    Serial1.println("[SYS] Entered Configuration Mode. Telemetry Paused.");
    return;
  }
  if (line == "0") {
    inConfigMode = false;
    Serial1.println("[SYS] Exited Configuration Mode. Resuming Telemetry.");
    return;
  }

  int sep = line.indexOf(':');
  if (sep == -1) {
    Serial1.println("[ERR] Unrecognized command");
    return;
  }

  String key = line.substring(0, sep);
  float val = line.substring(sep + 1).toFloat();

  if (key == "SET_V") {
    BUS_VOLTAGE_LIMIT = val;
    Serial1.print("[CFG] BUS_VOLTAGE_LIMIT=");
    Serial1.println(BUS_VOLTAGE_LIMIT, 3);
  } else if (key == "SET_IH") {
    OCP_LIMIT_A = val;
    ina226Write(REG_ALERT, computeAlertReg());
    Serial1.print("[CFG] OCP_LIMIT_A=");
    Serial1.println(OCP_LIMIT_A, 3);
  } else if (key == "SET_IL") {
    CURRENT_LOWER_LIMIT_A = val;
    Serial1.print("[CFG] CURRENT_LOWER_LIMIT_A=");
    Serial1.println(CURRENT_LOWER_LIMIT_A, 3);
  } else if (key == "SET_TL") {
    TEMP_LOW_LIMIT = val;
    Serial1.print("[CFG] TEMP_LOW_LIMIT=");
    Serial1.println(TEMP_LOW_LIMIT, 2);
  } else if (key == "SET_TH") {
    TEMP_HIGH_LIMIT = val;
    Serial1.print("[CFG] TEMP_HIGH_LIMIT=");
    Serial1.println(TEMP_HIGH_LIMIT, 2);
  } else if (key == "SET_H2") {
    H2_LIMIT_V = val;
    Serial1.print("[CFG] H2_LIMIT_V=");
    Serial1.println(H2_LIMIT_V, 2);
  } else if (key == "SET_RATE") {
    if (val > 0) {
      CSV_PERIOD_MS = (uint32_t)val;
      Serial1.print("[CFG] CSV_PERIOD_MS=");
      Serial1.println(CSV_PERIOD_MS);
    } else {
      Serial1.println("[ERR] Invalid rate");
    }
  } else {
    Serial1.println("[ERR] Unknown key");
  }
}

void handleIncomingSerial() {
  while (Serial1.available() > 0) {
    char c = Serial1.read();

    if (c == '\n') {
      rxLineBuffer.trim();
      // Only accept lines that actually look like one of our commands.
      // Anything else (stray echoed debug text, noise, etc.) is dropped
      // silently instead of being fed to the parser.
      if (rxLineBuffer == "0" || rxLineBuffer == "1" || rxLineBuffer.startsWith("SET_")) {
        applySerialCommand(rxLineBuffer);
      }
      rxLineBuffer = "";
    } else if (c == '\r') {
      // ignore
    } else if (isPrintable(c)) {
      rxLineBuffer += c;
      if (rxLineBuffer.length() > 40) rxLineBuffer = "";  // overflow guard
    }
    // non-printable bytes (noise/corruption) are dropped, not appended
  }
}

void setup() {
  pinMode(ALERT_PIN, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(MOSFET_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
  digitalWrite(MOSFET_PIN, LOW);

  analogReadResolution(12);
  ina226Init();

  if (ENABLE_UART_TELEMETRY) {
    Serial1.begin(115200);
    Serial1.println("DATA,bus_V,current_A,power_W,temp_C,h2_V,mosfet,faultReason");
    bootTime = millis();
  }
}

void loop() {
  float busV = readBusVoltage();
  float cur = readCurrent();
  float pw = readPower();
  float temp = readTemperature();
  float h2 = readH2();

  // Safety checks always execute regardless of UART state
  checkFaults(busV, cur, temp, h2);

  if (ENABLE_UART_TELEMETRY) {
    uint32_t currentMillis = millis();

    // 1. RX Listening Logic — always active so settings can be pushed
    //    any time from the PC app, not just in the boot window.
    handleIncomingSerial();

    // 2. CSV Telemetry Output (Only transmits when NOT in config mode)
    if (!inConfigMode && (currentMillis - lastCsv >= CSV_PERIOD_MS)) {
      lastCsv = currentMillis;

      Serial1.print("DATA,");
      Serial1.print(busV, 3);
      Serial1.print(",");
      Serial1.print(cur, 3);
      Serial1.print(",");
      Serial1.print(pw, 3);
      Serial1.print(",");
      Serial1.print(temp, 2);
      Serial1.print(",");
      Serial1.print(h2, 2);
      Serial1.print(",");
      Serial1.print(mosfetOn ? 1 : 0);
      Serial1.print(",");
      Serial1.println(faultReason);
    }
  }
}
