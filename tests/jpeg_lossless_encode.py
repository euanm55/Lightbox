"""Tiny JPEG lossless (ITU T.81 process 14, SOF3) encoder used only for tests.

pydicom cannot *write* JPEG lossless, but hospital CDs very often use it, so
the viewer's decoder needs 16-bit test material. This writes one component,
predictor `predictor` (1-7), optional point transform 0, optional restart
interval, using a single generic Huffman table that covers all categories.
"""

from __future__ import annotations

import struct

import numpy as np


def _huffman_table():
    # One code per category 0..16 with lengths chosen so the canonical code is valid:
    # lengths 2,3,3,3,3,3,4,5,6,7,8,9,10,11,12,13,14 (17 symbols)
    lengths = [2, 3, 3, 3, 3, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    symbols = list(range(17))
    counts = [0] * 16
    for L in lengths:
        counts[L - 1] += 1
    # canonical codes
    codes = {}
    code = 0
    k = 0
    for L in range(1, 17):
        for _ in range(counts[L - 1]):
            codes[symbols[k]] = (code, L)
            code += 1
            k += 1
        code <<= 1
    return counts, symbols, codes


class _BitWriter:
    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def write(self, value: int, nbits: int):
        for i in range(nbits - 1, -1, -1):
            self.acc = (self.acc << 1) | ((value >> i) & 1)
            self.n += 1
            if self.n == 8:
                self.out.append(self.acc)
                if self.acc == 0xFF:
                    self.out.append(0x00)
                self.acc = 0
                self.n = 0

    def flush(self):
        if self.n:
            self.write((1 << (8 - self.n)) - 1, 8 - self.n)  # pad with ones


def encode(img: np.ndarray, precision: int = 16, predictor: int = 1, restart: int = 0) -> bytes:
    """Encode a 2-D unsigned array as a lossless JPEG codestream."""
    h, w = img.shape
    img = img.astype(np.int64)
    counts, symbols, codes = _huffman_table()
    out = bytearray(b"\xff\xd8")
    out += b"\xff\xc4" + struct.pack(">H", 2 + 1 + 16 + len(symbols)) + bytes([0x00]) + bytes(counts) + bytes(symbols)
    out += b"\xff\xc3" + struct.pack(">HBHHB", 8 + 3, precision, h, w, 1) + bytes([1, 0x11, 0])
    if restart:
        out += b"\xff\xdd" + struct.pack(">HH", 4, restart)
    out += b"\xff\xda" + struct.pack(">HB", 6 + 2, 1) + bytes([1, 0x00]) + bytes([predictor, 0, 0])
    bw = _BitWriter()
    initial = 1 << (precision - 1)
    mcu = 0
    rst = 0
    interval_row, interval_col = 0, 0
    for y in range(h):
        for x in range(w):
            if restart and mcu > 0 and mcu % restart == 0:
                bw.flush()
                bw.out += bytes([0xFF, 0xD0 + (rst & 7)])
                rst += 1
                interval_row, interval_col = y, x
            if y == interval_row and x == interval_col:
                pred = initial
            elif y == interval_row:
                pred = img[y, x - 1]
            elif x == 0:
                pred = img[y - 1, x]
            else:
                ra, rb, rc = img[y, x - 1], img[y - 1, x], img[y - 1, x - 1]
                pred = {1: ra, 2: rb, 3: rc, 4: ra + rb - rc, 5: ra + ((rb - rc) >> 1), 6: rb + ((ra - rc) >> 1), 7: (ra + rb) >> 1}[predictor]
            diff = int(img[y, x] - pred)
            # modulo 2^16 into signed range
            diff = ((diff + 32768) & 0xFFFF) - 32768
            if diff == 0:
                ssss = 0
            elif diff == -32768:
                ssss = 16
            else:
                ssss = int(abs(diff)).bit_length()
            code, length = codes[ssss]
            bw.write(code, length)
            if 0 < ssss < 16:
                bits = diff if diff > 0 else diff + (1 << ssss) - 1
                bw.write(bits, ssss)
            mcu += 1
    bw.flush()
    out += bw.out
    out += b"\xff\xd9"
    return bytes(out)
