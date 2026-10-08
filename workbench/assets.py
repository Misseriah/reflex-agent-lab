"""Render a compact bitmap of the observed historical domain coverage."""
import struct
import zlib
from pathlib import Path


def render_coverage():
    # Four equal-width bands correspond to the four observed 240-episode strata.
    width, height = 800, 60
    colors = [(34, 130, 103), (65, 116, 184), (180, 132, 48), (120, 103, 155)]
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            color = colors[min(3, x // 200)] if x % 200 < 196 else (246, 247, 248)
            row.extend(color)
        rows.append(b'\0' + bytes(row))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')
    (Path(__file__).parent / 'static/coverage.png').write_bytes(png)


if __name__ == '__main__':
    render_coverage()
