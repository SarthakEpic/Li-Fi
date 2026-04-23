# Li-Fi Communication Project - Phase 2 Desktop App

This Phase 2 desktop app extends the original Python + Tkinter Li-Fi chat to support:

- live text chat over USB serial
- small file transfer under 10 KB
- packet-based serial framing
- CRC32 chunk validation
- ACK / NACK retry handling

## Files

- `app.py` - main Phase 2 desktop application
- `received_files/` - created automatically when files are received

## Install `pyserial`

```bash
python -m pip install pyserial
```

## Run the App

```bash
python app.py
```

## Phase 2 Features

- All Phase 1 text chat features remain available
- `Send File` button in the transmitter panel
- File picker for choosing a small text file
- File size limit of 10 KB
- Receiver reconstructs the file and saves it locally
- Receiver displays file content in the receiver panel
- Progress bar and transfer status on the transmitter side
- Chunk checksum validation with retransmission support

## Packet Structure

The app uses line-based serial packets. Each packet is one text line ending with `\n`.

### Chat packet

```text
CHAT|<base64_message>
```

### File start packet

```text
FILE_START|<transfer_id>|<base64_filename>|<file_size>|<chunk_size>|<chunk_count>|<file_crc32>
```

### File chunk packet

```text
FILE_CHUNK|<transfer_id>|<chunk_index>|<chunk_crc32>|<base64_chunk_data>
```

### File end packet

```text
FILE_END|<transfer_id>|<chunk_count>|<file_crc32>
```

### Reliability packets

```text
FILE_ACK|<transfer_id>|<token>
FILE_NACK|<transfer_id>|<token>|<base64_reason>
FILE_ABORT|<transfer_id>|<base64_reason>
```

`token` is:

- `START` for the header packet
- chunk index such as `0`, `1`, `2`
- `END` for the final completion packet

## How File Transfer Works

1. The transmitter app reads the selected file into bytes.
2. The file is split into small chunks of 48 bytes.
3. The transmitter sends:
   - `FILE_START`
   - each `FILE_CHUNK`
   - `FILE_END`
4. Every chunk is base64 encoded so serial transport stays safe for text-based links.
5. Every chunk also carries its own CRC32 checksum.
6. The receiver validates each chunk:
   - if valid, it sends `FILE_ACK`
   - if invalid, it sends `FILE_NACK`
7. If ACK does not arrive in time, the transmitter retries the last packet.
8. After all chunks arrive, the receiver reconstructs the file in order, validates the final CRC32, saves it locally, and displays the text content.

## Receiver Reconstruction

The receiver stores incoming chunks in memory using their chunk index:

- chunk `0`
- chunk `1`
- chunk `2`
- ...

When `FILE_END` arrives, it:

1. confirms all expected chunks exist
2. joins them in index order
3. verifies total file size
4. verifies the full-file CRC32
5. saves the file to `received_files/`
6. shows the file content in the receiver panel

If a filename already exists, the app saves a timestamped copy.

## Testing Workflow

1. Connect both microcontrollers by USB.
2. Run `python app.py`.
3. Connect the transmitter COM port.
4. Connect the receiver COM port.
5. Test chat by sending a text message.
6. Test file transfer with a small `.txt`, `.md`, `.json`, or `.csv` file.

## Arduino / ESP32 Adaptation Notes

Your firmware now needs to forward full packet lines instead of treating everything as plain chat text.

### Transmitter microcontroller should

- read one serial line from USB
- if it receives:
  - `CHAT|...`
  - `FILE_START|...`
  - `FILE_CHUNK|...`
  - `FILE_END|...`
  then send that exact line over the Li-Fi transmit path
- listen for return packets from the receiver side:
  - `FILE_ACK|...`
  - `FILE_NACK|...`
  - `FILE_ABORT|...`
- forward those return packets back to the PC over USB serial

### Receiver microcontroller should

- receive Li-Fi packet lines from the transmitter side
- forward them exactly to the PC over USB serial
- also read ACK / NACK / ABORT lines arriving from the PC over USB serial
- send those return packets back over the Li-Fi return path toward the transmitter side

## Important Firmware Rule

Do not modify the packet text in the microcontroller.

Forward each line exactly as received, including:

- command name
- separators `|`
- base64 data
- CRC values

## Minimal Firmware Pattern

### Transmitter-side board

```cpp
void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() > 0) {
      // Send this full line through the Li-Fi transmitter path
    }
  }

  // If an ACK/NACK/ABORT comes back from Li-Fi receiver path:
  // Serial.println(returnLine);
}
```

### Receiver-side board

```cpp
void loop() {
  // If a line arrives from Li-Fi path:
  // Serial.println(receivedLine);

  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() > 0) {
      // Send this return line back over the Li-Fi reverse path
    }
  }
}
```

## Phase 2 Constraints

This version intentionally does not include:

- encryption
- compression
- large file support
- databases
- networking
