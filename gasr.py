#!/bin/ld-linux-patched.so /bin/python
import sys
import ctypes
from soda_api_pb2 import ExtendedSodaConfigMsg, SodaResponse, SodaRecognitionResult

import time
from rich.live import Live
from rich.console import Console
from rich.text import Text
import subprocess

SPEED_FACTOR = 3.5
CHANNEL_COUNT = 1
SAMPLE_RATE = 16000
CHUNK_SIZE = 2048 # 2 chunks per frame, a frame is a single s16

FF_CMD = [
    "ffmpeg",
    "-loglevel", "31",
    "-ss", "00:00:00",
    "-i", "/tmp/a.mp3",
    "-ac", "1",
    "-ar", "16000",
    "-f", "s16le",
    "-acodec", "pcm_s16le",
    "pipe:"
]

CALLBACK = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_byte), ctypes.c_int, ctypes.c_void_p)
class SodaConfig(ctypes.Structure):
    _fields_ = [('soda_config', ctypes.c_char_p),
                ('soda_config_size', ctypes.c_int),
                ('callback', CALLBACK),
                ('callback_handle', ctypes.c_void_p)]


class SodaClient():
    def __init__(self, callback=None):
        self.sodalib = ctypes.CDLL('./libsoda.so')
        if callback == None:
            callback = CALLBACK(self.resultHandler)
        else:
            callback = CALLBACK(callback)
        cfg_proto = ExtendedSodaConfigMsg()
        cfg_proto.channel_count = CHANNEL_COUNT
        cfg_proto.sample_rate = SAMPLE_RATE
        cfg_proto.api_key = 'dummy_api_key'
        cfg_proto.language_pack_directory = './SODAModels/'
        cfg_serialized = cfg_proto.SerializeToString()
        self.config = SodaConfig(cfg_serialized, len(cfg_serialized), callback, None)
        self.sodalib.CreateExtendedSodaAsync.restype = ctypes.c_void_p

        self.console = Console()
        self.live = Live("", console=self.console, refresh_per_second=2)

    def start(self):
        self.handle = ctypes.c_void_p(self.sodalib.CreateExtendedSodaAsync(self.config))
        self.sodalib.ExtendedSodaStart(self.handle)
        
        # Calculation for pacing
        # bytes_per_second = 16000 * 1 * 2 = 32000
        bytes_per_second = SAMPLE_RATE * CHANNEL_COUNT * 2
        # This is how many seconds of REAL time pass per chunk
        # If speed is 2.0, we want to wait only half the time a chunk normally takes
        seconds_per_chunk_at_realtime = CHUNK_SIZE / bytes_per_second
        seconds_per_chunk_target = seconds_per_chunk_at_realtime / SPEED_FACTOR

        with self.live:
            # IMPORTANT: Removed "-re" so FFmpeg pushes data as fast as possible
            p = subprocess.Popen(FF_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            
            with p.stdout as fh:
                chunk = fh.read(CHUNK_SIZE)
                while chunk:
                    start_time = time.perf_counter()

                    # 1. Feed the audio
                    self.sodalib.ExtendedAddAudio(self.handle, chunk, len(chunk))

                    # 2. Calculate precise sleep to maintain the target speed
                    elapsed = time.perf_counter() - start_time
                    sleep_time = seconds_per_chunk_target - elapsed

                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    
                    chunk = fh.read(CHUNK_SIZE)

    def delete(self):
        self.sodalib.DeleteExtendedSodaAsync(self.handle)

    def resultHandler(self, response, rlen, instance):
        res = SodaResponse()
        res.ParseFromString(ctypes.string_at(response, rlen))
        if res.soda_type == SodaResponse.SodaMessageType.RECOGNITION:
            if res.recognition_result.result_type == SodaRecognitionResult.ResultType.FINAL:
                self.console.print(f'[green]*[/green] {res.recognition_result.hypothesis[0]}')

            elif res.recognition_result.result_type == SodaRecognitionResult.ResultType.PARTIAL:
                text = Text.from_markup(f'[yellow]*[/yellow] {res.recognition_result.hypothesis[0]}')
                self.live.update(text)




if __name__ == '__main__':
    client = SodaClient()
    try:
        client.start()
    except KeyboardInterrupt:
        client.delete()
