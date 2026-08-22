# Arduino Uno GPIO Flashing Playbook

Use this procedure for the Aircheck Arduino Uno when the UART jumpers cannot be moved and USB flashing is not an option.

## Known configuration

- Pi host: `jpark@aircheck-pi.local`
- Board: Arduino Uno with ATmega328P
- Sketch: `/home/jpark/Aircheck/kkfm/radio/si4713_sketch`
- Serial device: `/dev/serial0`, linked to `/dev/ttyS0`
- Board target: `arduino:avr:uno`
- Upload protocol: Arduino/Optiboot at 115200 baud
- Reset method: manually press the Uno RESET button

## 1. Connect and enter the sketch directory

```bash
ssh jpark@aircheck-pi.local
cd /home/jpark/Aircheck/kkfm/radio/si4713_sketch
```

## 2. Keep other programs off the UART

Check the transmitter watchdog before flashing:

```bash
systemctl is-active acnh-aircheck-tx-watchdog.service
```

If it reports `active`, stop it and remember to restart it after verification:

```bash
sudo systemctl stop acnh-aircheck-tx-watchdog.service
```

## 3. Confirm the intended sketch settings

```bash
grep -nE 'FIXED_FREQ|TX_POWER_dBuV|Serial.begin' si4713_sketch.ino
```

The frequency uses units of 10 kHz. For example, `8810` means 88.10 MHz.

## 4. Compile to a predictable location

```bash
mkdir -p /tmp/si4713-build

arduino-cli compile \
  --fqbn arduino:avr:uno \
  --output-dir /tmp/si4713-build \
  /home/jpark/Aircheck/kkfm/radio/si4713_sketch
```

Confirm that the upload image exists:

```bash
ls -l /tmp/si4713-build/si4713_sketch.ino.hex
```

## 5. Flash with manual RESET timing

Hold the Uno RESET button down before running the command. Release RESET when `RELEASE RESET NOW` appears.

```bash
echo "Keep holding RESET"; \
sleep 3; \
echo "RELEASE RESET NOW"; \
sleep 0.2; \
avrdude \
  -p atmega328p \
  -c arduino \
  -P /dev/serial0 \
  -b 115200 \
  -D \
  -U flash:w:/tmp/si4713-build/si4713_sketch.ino.hex:i
```

Success requires all of these results:

```text
AVR device initialized and ready to accept instructions
device signature = 0x1e950f
bytes of flash written
bytes of flash verified
avrdude done. Thank you.
```

The verification pass compares the board's flash with the compiled image byte-for-byte.

## 6. Verify the transmitter's reported frequency

Run this while the watchdog remains stopped:

```bash
timeout 6s python3 -c 'import serial,itertools; s=serial.Serial("/dev/serial0",115200,timeout=0.5); print("".join(x.decode("utf-8","replace") for x in itertools.islice(s,15)),end=""); s.close()'
```

The second column is the frequency reported by the Si4713, in MHz:

```text
25604 88.00 0 -66 0
```

## 7. Restore monitoring and verify its API record

If the watchdog was active before flashing, restart it:

```bash
sudo systemctl start acnh-aircheck-tx-watchdog.service
```

Then inspect the status record consumed by the monitoring front end:

```bash
cat /run/kkfm-watchdog/tx_status.json
```

Confirm that `freq_mhz` matches the serial telemetry.

## Failure signatures

`stk500_getsync()` with responses such as `0x31`, `0x32`, `0x2e`, or `0x20` means `avrdude` is reading ASCII telemetry from the normal sketch. The board did not remain in its bootloader during synchronization. Repeat the direct `avrdude` procedure and release RESET at the printed prompt.

Do not treat a successful `arduino-cli compile` as a flash. Compilation only creates the `.hex` file. Flashing is complete only after `avrdude` reports both the write and verification passes as successful.
