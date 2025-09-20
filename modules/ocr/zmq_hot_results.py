#!/usr/bin/env python3
"""
ZMQ Hot-Path Results Subscriber (binary HotBin payload).

- Connects to the daemon's ZMQ PUB address (e.g., tcp://<vm>:5560)
- Unpacks the fixed HotBin struct and yields a dict similar to the SHM JSON

Usage:
  python -m modules.ocr.zmq_hot_results --addr tcp://192.168.50.12:5560

Integration (example):
  from modules.ocr.zmq_hot_results import HotBinSubscriber
  sub = HotBinSubscriber('tcp://192.168.50.12:5560')
  for pkt in sub.iter_packets():
      handle(pkt)
"""
from __future__ import annotations

import argparse
import struct
import time
from typing import Dict, Any, Iterator, Optional

import zmq


HOTBIN_FMT = '<f d Q Q Q Q 40s I 512s'
HOTBIN_SIZE = struct.calcsize(HOTBIN_FMT)


def parse_hotbin(raw: bytes) -> Optional[Dict[str, Any]]:
    if len(raw) < HOTBIN_SIZE:
        return None
    conf, origin_ts, t1, t2, t3, t4, cid_bytes, text_len, text_bytes = struct.unpack(HOTBIN_FMT, raw[:HOTBIN_SIZE])
    try:
        cid = cid_bytes.split(b'\x00', 1)[0].decode('utf-8', errors='ignore')
        txt = text_bytes[:text_len].decode('utf-8', errors='ignore') if text_len > 0 else ''
    except Exception:
        return None
    return {
        'type': 'ocr_trading_signal',
        'data': {
            'full_raw_text': txt,
            'overall_confidence': float(conf),
            'correlation_id': cid,
            'origin_timestamp_s': float(origin_ts),
            't1_preproc_ns': int(t1),
            't2_tess_begin_ns': int(t2),
            't3_ocr_end_ns': int(t3),
            't4_total_ns': int(t4),
        }
    }


class HotBinSubscriber:
    def __init__(self, addr: str):
        self.addr = addr
        self.ctx = zmq.Context.instance()
        self.sock = self.ctx.socket(zmq.SUB)
        self.sock.connect(addr)
        self.sock.setsockopt_string(zmq.SUBSCRIBE, '')

    def recv_packet(self, timeout_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
        if timeout_ms is not None:
            poller = zmq.Poller()
            poller.register(self.sock, zmq.POLLIN)
            events = dict(poller.poll(timeout=timeout_ms))
            if self.sock not in events:
                return None
        raw = self.sock.recv()
        return parse_hotbin(raw)

    def iter_packets(self) -> Iterator[Dict[str, Any]]:
        while True:
            pkt = self.recv_packet()
            if pkt is not None:
                yield pkt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--addr', default='tcp://127.0.0.1:5560')
    args = ap.parse_args()

    sub = HotBinSubscriber(args.addr)
    print(f"[SUB] Connected to {args.addr}, waiting for HotBin frames...")
    last = time.time()
    count = 0
    for pkt in sub.iter_packets():
        count += 1
        d = pkt['data']
        print(f"conf={d['overall_confidence']:.1f} total_ms={d['t4_total_ns']/1e6:.1f} text='{d['full_raw_text'][:80]}'")
        now = time.time()
        if now - last >= 5.0:
            print(f"[rate] {count/ (now-last):.1f} fps")
            count = 0
            last = now
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

