import argparse
import time

import serial

# settings must match daq_rx_dashboard.py
SERIAL_PORT = "/dev/ttyACM0"   # Uno over USB. Check with: ls /dev/ttyACM* /dev/ttyUSB*
SERIAL_BAUD = 115200

RADIO_FREQ_MHZ = 915.0
ENCRYPTION_KEY = b"AurelianDAQ_k_01"
MY_NODE = 1
RX_NODE = 2
TX_POWER_DBM = 20 # lower to 14 for bench testing

#PI5 wiring
CS_PIN = "CE1"     # physical pin 26
RESET_PIN = "D25"  # physical pin 22

FIELDS = ["PT1", "PT2", "PT3", "PT4", "LC1", "TC1"]
SEND_INTERVAL_S = 0.1 # max = 10 packets
# ----------------------------------------------------------------


def setup_radio():
    import board
    import busio
    import digitalio
    import adafruit_rfm69

    spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
    cs = digitalio.DigitalInOut(getattr(board, CS_PIN))
    reset = digitalio.DigitalInOut(getattr(board, RESET_PIN))
    rfm = adafruit_rfm69.RFM69(spi, cs, reset, RADIO_FREQ_MHZ)
    rfm.encryption_key = ENCRYPTION_KEY
    rfm.tx_power = TX_POWER_DBM
    rfm.node = MY_NODE
    rfm.destination = RX_NODE
    return rfm


def parse_line(line):
    """'PT1:452.10,PT2:NC,...' -> {'PT1': '452.10', 'PT2': 'NC', ...} or None."""
    values = {}
    for part in line.split(","):
        if ":" not in part:
            return None
        key, val = part.split(":", 1)
        values[key.strip()] = val.strip()
    if not all(f in values for f in FIELDS):
        return None
    return values


def pack(seq, values):
    """Compact payload: '<seq>|v1,v2,...' — labels stripped so it fits
    the RFM69's 60-byte packet limit even with large readings."""
    return f"{seq}|" + ",".join(values[f] for f in FIELDS)


def open_serial():
    while True:
        try:
            ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.05)
            time.sleep(2)            # opening the port resets the Uno
            ser.reset_input_buffer()
            print(f"Connected to Arduino on {SERIAL_PORT}")
            return ser
        except serial.SerialException as e:
            print(f"Waiting for Arduino on {SERIAL_PORT}: {e}")
            time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-radio", action="store_true",
                    help="print readings instead of transmitting")
    args = ap.parse_args()

    rfm = None if args.no_radio else setup_radio()
    if rfm:
        print(f"Radio up: {RADIO_FREQ_MHZ} MHz, node {MY_NODE} -> {RX_NODE}")

    ser = open_serial()
    seq = 0
    latest = None
    last_send = 0.0
    buf = b""

    while True:
        try:
            buf += ser.read(ser.in_waiting or 1)
        except serial.SerialException:
            print("Arduino disconnected, reconnecting...")
            ser.close()
            ser = open_serial()
            buf = b""
            continue

        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            values = parse_line(raw.decode(errors="ignore").strip())
            if values:
                latest = values

        now = time.monotonic()
        if latest and now - last_send >= SEND_INTERVAL_S:
            payload = pack(seq, latest)
            if rfm:
                rfm.send(payload.encode())
            else:
                print(payload)
            seq = (seq + 1) % 100000
            latest = None
            last_send = now


if __name__ == "__main__":
    main()
