#include <Wire.h>
#include <Adafruit_Si4713.h>

/* ---------- user-adjustable ------------------------------------------- */
#define FIXED_FREQ      8790    // 87.90 MHz (×10 kHz).  Use scanBand() if you prefer auto-scan
#define TX_POWER_dBuV     115
#define LOG_MS           100   // serial log period
// I2C hardening: prevents permanent hangs if Si4713 stops responding mid-run.
#define I2C_CLOCK_HZ   100000  // 100 kHz is conservative and robust for longer wiring
#define WIRE_TIMEOUT_US  25000 // 25 ms per I2C transaction; tune if needed

// Auto-recovery: if we detect an I2C timeout or repeated errors, re-init the chip.
#define RECOVERY_COOLDOWN_MS 2000
#define INIT_RETRIES            5
/* ---------------------------------------------------------------------- */

#define LED_PIN        LED_BUILTIN    // on-board LED
#define RST_PIN              12       // Si4713 reset

/* property IDs (from datasheet) */
#define PROP_TX_AUDIO_DEVIATION        0x2101
#define PROP_TX_PILOT_DEVIATION        0x2102
// #define PROP_TX_LINE_INPUT_LEVEL       0x2104
#define PROP_TX_INPUT_LEVEL            0x3100    // 0.5 dB/LSB pad/boost
#define PROP_TX_PREEMPHASIS            0x2106
#define PROP_TX_ACOMP_ENABLE           0x2200
#define PROP_TX_ACOMP_THRESHOLD        0x2201
#define PROP_TX_ACOMP_ATTACK_TIME      0x2202
#define PROP_TX_ACOMP_RELEASE_TIME     0x2203
#define PROP_TX_ACOMP_GAIN             0x2204

Adafruit_Si4713 radio(RST_PIN);

static uint32_t g_last_recovery_ms = 0;

static void hardResetSi4713()
{
    // Si4713 reset is active-low. We pulse low then release.
    pinMode(RST_PIN, OUTPUT);
    digitalWrite(RST_PIN, LOW);
    delay(5);
    digitalWrite(RST_PIN, HIGH);
    delay(50); // give internal boot time; longer is safer than 20 ms on marginal setups
}

static void setupI2C()
{
    Wire.begin();
    Wire.setClock(I2C_CLOCK_HZ);

    // Many Arduino cores expose this; it prevents permanent hangs in Wire.
#if defined(WIRE_HAS_TIMEOUT)
    Wire.setWireTimeout(WIRE_TIMEOUT_US, true /* reset_on_timeout */);
    Wire.clearWireTimeoutFlag();
#endif
}

// Optional: try to recover from a stuck I2C bus (e.g., SDA held low) by clocking SCL.
static void i2cBusRecover()
{
#if defined(SCL) && defined(SDA)
    pinMode(SCL, OUTPUT);
    pinMode(SDA, INPUT_PULLUP);
    digitalWrite(SCL, HIGH);
    delay(1);

    // Up to 9 clocks to release a stuck slave.
    for (int i = 0; i < 9; i++) {
        digitalWrite(SCL, LOW);
        delayMicroseconds(5);
        digitalWrite(SCL, HIGH);
        delayMicroseconds(5);
    }

    // Generate a STOP: SDA low -> SCL high -> SDA high (approx).
    pinMode(SDA, OUTPUT);
    digitalWrite(SDA, LOW);
    delayMicroseconds(5);
    digitalWrite(SCL, HIGH);
    delayMicroseconds(5);
    digitalWrite(SDA, HIGH);
    delayMicroseconds(5);

    // Restore to I2C mode.
    pinMode(SDA, INPUT_PULLUP);
#endif
}

static bool initRadio()
{
    setupI2C();
    hardResetSi4713();

    if (!radio.begin()) {
        return false;
    }

    /* --------- Frequency & output power -------------------------------- */
    radio.setTXpower(TX_POWER_dBuV);
    radio.tuneFM(FIXED_FREQ);             // comment & call scanBand() if you want auto-scan
    /* ------------------------------------------------------------------ */

    /* --------- deviation, input pad, limiter --------------------------- */
    // radio.setProperty(PROP_TX_AUDIO_DEVIATION,  6500);   // 65 kHz audio peak
    radio.setProperty(PROP_TX_AUDIO_DEVIATION,  6000);   // 60 kHz audio peak
    radio.setProperty(PROP_TX_PILOT_DEVIATION,   750);   // 7.5 kHz pilot

    // Input level/pad: if LED is "blinking rapidly" it's usually OVERMOD due to hot audio.
    // Increase LIATTEN to reduce modulation peaks (trading some SNR for stability).
    // radio.setProperty(PROP_TX_INPUT_LEVEL,      0x515E); // LIATTEN=2, LILEVEL=350 mVPK
    // radio.setProperty(PROP_TX_INPUT_LEVEL,      0x6120); // LIATTEN=6 (~18 dB pad), LILEVEL≈200 mVpk
    radio.setProperty(PROP_TX_INPUT_LEVEL,      0x8120);   // LIATTEN=8 (~24 dB pad), LILEVEL≈200 mVpk
    // US FM broadcast uses 75 µs pre-emphasis.
    // (0 = 75 µs, 1 = none, 2 = 50 µs)
    radio.setProperty(PROP_TX_PREEMPHASIS,      0x0000);

    radio.setProperty(PROP_TX_ACOMP_ENABLE,     0x0002); // limiter-only
    // radio.setProperty(PROP_TX_ACOMP_ENABLE,     0x0003); // compressor + limiter
    // radio.setProperty(PROP_TX_ACOMP_THRESHOLD,  0xFFF6); // −10 dBFS knee
    // radio.setProperty(PROP_TX_ACOMP_THRESHOLD,  0xFFF8); // about –6 dBFS knee
    radio.setProperty(PROP_TX_ACOMP_THRESHOLD,  0xFFFA);   // ≈ –5 dBFS knee
    // radio.setProperty(PROP_TX_ACOMP_THRESHOLD,  0xFFFC); // about –3 dBFS knee (compressor)

    // radio.setProperty(PROP_TX_ACOMP_ATTACK_TIME,    0); // 0.5 ms
    // radio.setProperty(PROP_TX_ACOMP_ATTACK_TIME, 0x0001);  // 1.5 ms
    radio.setProperty(PROP_TX_ACOMP_RELEASE_TIME, 0x0002);  // 64 ms enum 2
    // radio.setProperty(PROP_TX_ACOMP_RELEASE_TIME, 0x0001); // 32 ms
    radio.setProperty(PROP_TX_ACOMP_GAIN,         0x0000); // leave 0 dB makeup
    /* ------------------------------------------------------------------ */

    /* -------- RDS ------------------------------------------------------ */
    radio.beginRDS();
    radio.setRDSstation("KKFM");
    radio.setRDSbuffer ("Si4713 demo");
    /* ------------------------------------------------------------------ */

    return true;
}

static void recoverIfNeeded(const __FlashStringHelper* why)
{
    const uint32_t now = millis();
    if (now - g_last_recovery_ms < RECOVERY_COOLDOWN_MS) {
        return;
    }
    g_last_recovery_ms = now;

    Serial.print(F("RECOVER: "));
    Serial.print(why);
    Serial.print(F(" at ms="));
    Serial.println(now);

    i2cBusRecover();

    for (int i = 0; i < INIT_RETRIES; i++) {
        if (initRadio()) {
            Serial.println(F("RECOVER: initRadio OK"));
            return;
        }
        Serial.print(F("RECOVER: initRadio failed, retry "));
        Serial.println(i + 1);
        delay(200);
    }

    Serial.println(F("RECOVER: giving up (will keep trying later)"));
}

void setup()
{
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    Serial.begin(115200);

    // Initial bring-up (retry a few times so you don't have to mash Run).
    bool ok = false;
    for (int i = 0; i < INIT_RETRIES; i++) {
        if (initRadio()) {
            ok = true;
            break;
        }
        Serial.print(F("initRadio failed, retry "));
        Serial.println(i + 1);
        delay(250);
    }

    // If we still can't talk to the chip, keep running and let recovery logic retry.
    if (!ok) {
        Serial.println(F("WARN: Si4713 not responding at setup; will auto-recover"));
    }

    Serial.print(F("TX_FREQ="));
    Serial.print(FIXED_FREQ / 100.0, 2);
    Serial.print(F(" MHz  PWR="));
    Serial.print(TX_POWER_dBuV);
    Serial.println(F(" dBµV"));
    Serial.println(F("LOG  ms  freq  noise  inLevel  overMod"));
}

void loop()
{
    static uint32_t t0 = 0;
    const uint32_t now = millis();

    if (now - t0 >= LOG_MS) {
        t0 = now;

#if defined(WIRE_HAS_TIMEOUT)
        if (Wire.getWireTimeoutFlag()) {
            Wire.clearWireTimeoutFlag();
            recoverIfNeeded(F("WireTimeout"));
        }
#endif

        radio.readASQ();                  // updates currASQ & currInLevel
        radio.readTuneStatus();           // updates currNoiseLevel

        /* bit 2 (0x04) is SQINT / OVERMOD when deviation > TX_AUDIO_DEVIATION
           datasheet §5.3.1, fig 14 text 12-13-B30.pdf](file-service://file-4LtwgcEdv2CdUPadp3L2V8) */
        const bool over = radio.currASQ & 0x04;
        digitalWrite(LED_PIN, over ? HIGH : LOW);

        Serial.print(now);                       Serial.print(' ');
        Serial.print(radio.currFreq / 100.0, 2);  Serial.print(' ');
        Serial.print(radio.currNoiseLevel);       Serial.print(' ');
        Serial.print(radio.currInLevel);          Serial.print(' ');
        Serial.println(over ? 1 : 0);
    }
}
