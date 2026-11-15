#include <Wire.h>
#include <avr/wdt.h>
#include <Adafruit_Si4713.h>

/* ---------- user-adjustable ------------------------------------------- */
#define FIXED_FREQ     10010    // 100.1 MHz (×10 kHz)
#define TX_POWER_dBuV     115
// Antenna capacitor, fixed (×0.25 pF). Found by listening sweeps at 100.1 MHz with the
// 1 m wire plus a 75 cm ground-side wire: best around 40, broad peak (30..40 hard to
// tell apart), clearly worse at 10 and 70. The chip's auto-tune (value 0) landed on
// 53..66 here, and the AN332 example settings with auto-tune sounded worse.
#define TX_ANTCAP          40
#define LOG_MS           100   // serial log period
#define SCAN_MIN_FREQ    8810   // 88.10 MHz
#define SCAN_MAX_FREQ   10790   // 107.90 MHz
#define SCAN_STEP          20   // 200 kHz US FM channel spacing
// I2C hardening: prevents permanent hangs if Si4713 stops responding mid-run.
#define I2C_CLOCK_HZ   100000  // 100 kHz is conservative and robust for longer wiring
#define WIRE_TIMEOUT_US  25000 // 25 ms per I2C transaction; tune if needed

// Auto-recovery: if we detect an I2C timeout or repeated errors, re-init the chip.
#define RECOVERY_COOLDOWN_MS 2000
#define INIT_RETRIES            5
// The Si4713 library waits forever for the chip's CTS bit. A chip knocked into
// powerdown by a transient never sets it, so the hardware watchdog reboots the
// sketch, and setup() hard-resets and re-tunes the chip.
#define WDT_TIMEOUT        WDTO_4S
// Consecutive telemetry reads off-frequency before the chip is re-initialised.
#define STATE_LOST_READS        3
/* ---------------------------------------------------------------------- */

#define LED_PIN        LED_BUILTIN    // on-board LED
#define RST_PIN              12       // Si4713 reset

/* property IDs (from datasheet) */
#define PROP_TX_AUDIO_DEVIATION        0x2101
#define PROP_TX_PILOT_DEVIATION        0x2102
#define PROP_TX_PREEMPHASIS            0x2106
#define PROP_TX_ACOMP_ENABLE           0x2200
#define PROP_TX_ACOMP_THRESHOLD        0x2201
#define PROP_TX_ACOMP_ATTACK_TIME      0x2202
#define PROP_TX_ACOMP_RELEASE_TIME     0x2203
#define PROP_TX_ACOMP_GAIN             0x2204

Adafruit_Si4713 radio(RST_PIN);

static uint32_t g_last_recovery_ms = 0;
static uint16_t g_selected_freq = FIXED_FREQ;
static bool g_frequency_selected = true;

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

static uint16_t scanBestFrequency()
{
    uint16_t best_freq = FIXED_FREQ;
    uint8_t best_noise = 0xFF;

    Serial.println(F("SCAN_BEGIN"));
    for (uint16_t freq = SCAN_MIN_FREQ; freq <= SCAN_MAX_FREQ; freq += SCAN_STEP) {
        wdt_reset();
        radio.readTuneMeasure(freq);
        radio.readTuneStatus();

        Serial.print(F("SCAN "));
        Serial.print(freq / 100.0, 2);
        Serial.print(' ');
        Serial.println(radio.currNoiseLevel);

        if (radio.currNoiseLevel < best_noise) {
            best_noise = radio.currNoiseLevel;
            best_freq = freq;
        }
    }

    Serial.print(F("SCAN_BEST="));
    Serial.print(best_freq / 100.0, 2);
    Serial.print(F(" MHz NOISE="));
    Serial.println(best_noise);
    return best_freq;
}

static bool initRadio()
{
    setupI2C();
    hardResetSi4713();

    if (!radio.begin()) {
        return false;
    }

    if (!g_frequency_selected) {
        g_selected_freq = scanBestFrequency();
        g_frequency_selected = true;
    }

    /* --------- Frequency & output power -------------------------------- */
    radio.setTXpower(TX_POWER_dBuV, TX_ANTCAP);
    radio.tuneFM(g_selected_freq);
    /* ------------------------------------------------------------------ */

    /* --------- audio processing ---------------------------------------- */
    // Line input stays at the chip default (0x2104 = 0x327C: 636 mVpk full scale).
    radio.setProperty(PROP_TX_PREEMPHASIS,      0x0000); // 75 µs, US receivers (AN332: 0 = 75 µs, 1 = 50 µs, 2 = off)

    // Compressor + limiter. The music sits 10-25 dB under full scale; the compressor
    // lifts everything below the threshold by the gain so the carrier is modulated
    // properly, and the limiter holds peaks at the set deviation.
    radio.setProperty(PROP_TX_ACOMP_ENABLE,       0x0003); // bit1 limiter, bit0 compressor
    radio.setProperty(PROP_TX_ACOMP_THRESHOLD,    0xFFD8); // -40 dBFS (chip default)
    radio.setProperty(PROP_TX_ACOMP_GAIN,         0x000F); // 15 dB (chip default)
    radio.setProperty(PROP_TX_ACOMP_RELEASE_TIME, 0x0002);
    /* ------------------------------------------------------------------ */

    /* -------- RDS ------------------------------------------------------ */
    radio.beginRDS();
    radio.setRDSstation("KKFM");
    radio.setRDSbuffer ("Si4713 demo");
    /* ------------------------------------------------------------------ */

    /* --------- deviation: set after beginRDS(), which overwrites it ----- */
    // 66.25 audio + 6.75 pilot + 2.00 RDS = 75.00 kHz total.
    radio.setProperty(PROP_TX_AUDIO_DEVIATION,  6625);
    radio.setProperty(PROP_TX_PILOT_DEVIATION,   675);
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
        wdt_reset();
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
    Serial.println(F("BOOT"));
    wdt_enable(WDT_TIMEOUT);

    // Initial bring-up (retry a few times so you don't have to mash Run).
    bool ok = false;
    for (int i = 0; i < INIT_RETRIES; i++) {
        wdt_reset();
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
    Serial.print(g_selected_freq / 100.0, 2);
    Serial.print(F(" MHz  PWR="));
    Serial.print(TX_POWER_dBuV);
    Serial.println(F(" dBµV"));
    radio.readTuneStatus();
    Serial.print(F("BOOT antcap="));
    Serial.println(radio.currAntCap);
    Serial.println(F("LOG  ms  freq  noise  dBFS  overMod"));
}

void loop()
{
    static uint32_t t0 = 0;
    static uint32_t overmod_until = 0;
    static uint8_t off_freq_reads = 0;
    const uint32_t now = millis();

    wdt_reset();

    // 'T' on serial: re-run the chip's antenna auto-tune and report the capacitor it chose.
    // The chip only tunes the antenna when told to tune, so whatever was near the wire at
    // boot (a hand on RESET) is baked in until the next tune.
    // 'C<n>' on serial: set the antenna capacitor by hand to n (1..191, 0.25 pF each).
    if (Serial.available()) {
        const int c = Serial.read();
        if (c == 'T') {
            radio.setTXpower(TX_POWER_dBuV);
            radio.tuneFM(g_selected_freq);
            radio.readTuneStatus();
            Serial.print(F("RETUNE antcap="));
            Serial.println(radio.currAntCap);
        } else if (c == 'C') {
            const long v = Serial.parseInt();
            if (v >= 1 && v <= 191) {
                radio.setTXpower(TX_POWER_dBuV, (uint8_t)v);
                radio.readTuneStatus();
                Serial.print(F("SETCAP antcap="));
                Serial.println(radio.currAntCap);
            }
        }
    }

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

        // A chip that answers but is no longer on our frequency has lost its state.
        if (radio.currFreq == g_selected_freq) {
            off_freq_reads = 0;
        } else if (++off_freq_reads >= STATE_LOST_READS) {
            off_freq_reads = 0;
            recoverIfNeeded(F("StateLost"));
        }

        /* bit 2 (0x04) is SQINT / OVERMOD when deviation > TX_AUDIO_DEVIATION */
        const bool over = radio.currASQ & 0x04;
        if (over) {
            overmod_until = now + 1000; // Keep LED on for 1 second on overmod
        }
        
        digitalWrite(LED_PIN, (now < overmod_until) ? HIGH : LOW);

        Serial.print(now);                       Serial.print(' ');
        Serial.print(radio.currFreq / 100.0, 2);  Serial.print(' ');
        Serial.print(radio.currNoiseLevel);       Serial.print(' ');
        Serial.print(radio.currInLevel);          Serial.print(' ');
        Serial.println(over ? 1 : 0);
    }
}
