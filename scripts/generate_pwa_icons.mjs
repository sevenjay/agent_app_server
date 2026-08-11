import { writeFileSync } from "node:fs";
import { deflateSync } from "node:zlib";

const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
const COLORS = {
  background: [15, 20, 26],
  brand: [54, 210, 210],
  signal: [123, 215, 137],
};

const CRC_TABLE = Array.from({ length: 256 }, (_, value) => {
  let result = value;
  for (let bit = 0; bit < 8; bit += 1) {
    result = (result & 1) ? (0xedb88320 ^ (result >>> 1)) : (result >>> 1);
  }
  return result >>> 0;
});

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const name = Buffer.from(type, "ascii");
  const length = Buffer.alloc(4);
  const checksum = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  checksum.writeUInt32BE(crc32(Buffer.concat([name, data])));
  return Buffer.concat([length, name, data, checksum]);
}

function png(width, height, pixels) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 6;

  const scanlines = Buffer.alloc(height * (1 + width * 4));
  for (let y = 0; y < height; y += 1) {
    const row = y * (1 + width * 4);
    scanlines[row] = 0;
    pixels.copy(scanlines, row + 1, y * width * 4, (y + 1) * width * 4);
  }

  return Buffer.concat([
    PNG_SIGNATURE,
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(scanlines, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function renderIcon(size, maskable = false) {
  const scale = size / 512;
  const samples = size < 256 ? 4 : 2;
  const pixels = Buffer.alloc(size * size * 4);
  const geometry = maskable
    ? { radius: 124, stroke: 58, dotX: 356, dotY: 69, dotRadius: 29 }
    : { radius: 156, stroke: 72, dotX: 376, dotY: 88, dotRadius: 36 };

  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      let brandCoverage = 0;
      let signalCoverage = 0;
      for (let sy = 0; sy < samples; sy += 1) {
        for (let sx = 0; sx < samples; sx += 1) {
          const px = (x + (sx + 0.5) / samples) / scale;
          const py = (y + (sy + 0.5) / samples) / scale;
          const dx = px - 256;
          const dy = py - 256;
          const distance = Math.hypot(dx, dy);
          const angle = Math.abs(Math.atan2(dy, dx) * 180 / Math.PI);
          const onArc = Math.abs(distance - geometry.radius) <= geometry.stroke / 2
            && angle >= 35;
          const onDot = Math.hypot(px - geometry.dotX, Math.abs(dy) - geometry.dotY)
            <= geometry.dotRadius;
          if (onDot) signalCoverage += 1;
          else if (onArc) brandCoverage += 1;
        }
      }

      const sampleCount = samples * samples;
      const signal = signalCoverage / sampleCount;
      const brand = brandCoverage / sampleCount;
      const offset = (y * size + x) * 4;
      for (let channel = 0; channel < 3; channel += 1) {
        const base = COLORS.background[channel];
        pixels[offset + channel] = Math.round(
          base * (1 - brand - signal)
          + COLORS.brand[channel] * brand
          + COLORS.signal[channel] * signal,
        );
      }
      pixels[offset + 3] = 255;
    }
  }
  return png(size, size, pixels);
}

for (const [name, size, maskable] of [
  ["apple-touch-icon.png", 180, false],
  ["icon-192.png", 192, false],
  ["icon-512.png", 512, false],
  ["icon-maskable-512.png", 512, true],
]) {
  writeFileSync(new URL(`../static/icons/${name}`, import.meta.url), renderIcon(size, maskable));
}
