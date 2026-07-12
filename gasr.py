#!/bin/ld-linux-patched.so /bin/python
import sys
import ctypes
import threading
import queue
from soda_api_pb2 import ExtendedSodaConfigMsg, SodaResponse, SodaRecognitionResult

import time
from rich.live import Live
from rich.console import Console
from rich.text import Text
import subprocess

SPEED_FACTOR = 5
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
        self.speed_factor = SPEED_FACTOR
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

        # Threading synchronization
        self.audio_queue = queue.Queue(maxsize=100) # Buffer up to 100 chunks
        self.running = False
        self.handle = None

    def _producer_thread(self):
        """Reads data from FFmpeg and pushes to the queue."""
        p = subprocess.Popen(FF_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        
        try:
            with p.stdout as fh:
                while self.running:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    # If queue is full, this will block, naturally applying backpressure
                    self.audio_queue.put(chunk)
        finally:
            p.terminate()
            self.audio_queue.put(None) # Sentinel to signal end of stream

    def _consumer_thread(self):
        """Pulls data from the queue and feeds the C library."""
        self.handle = ctypes.c_void_p(self.sodalib.CreateExtendedSodaAsync(self.config))
        self.sodalib.ExtendedSodaStart(self.handle)
        
        # Calculation for pacing
        # bytes_per_second = 16000 * 1 * 2 = 32000
        bytes_per_second = SAMPLE_RATE * CHANNEL_COUNT * 2
        # This is how many seconds of REAL time pass per chunk
        # If speed is 2.0, we want to wait only half the time a chunk normally takes
        seconds_per_chunk_at_realtime = CHUNK_SIZE / bytes_per_second
        seconds_per_chunk_target = seconds_per_chunk_at_realtime / SPEED_FACTOR

        while self.running:
            try:
                # Get chunk from queue with a timeout so we can check self.running
                chunk = self.audio_queue.get(timeout=1.0)
                if chunk is None: # End of stream
                    break
                
                start_time = time.perf_counter()

                # 1. Feed the audio
                self.sodalib.ExtendedAddAudio(self.handle, chunk, len(chunk))

                # 2. Precise sleep for pacing
                elapsed = time.perf_counter() - start_time
                sleep_time = seconds_per_chunk_target - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
                self.audio_queue.task_done()
            except queue.Empty:
                continue

    def start(self):
        self.running = True
        self.handle = ctypes.c_void_p(self.sodalib.CreateExtendedSodaAsync(self.config))
        self.sodalib.ExtendedSodaStart(self.handle)

        # Start the threads
        self.producer = threading.Thread(target=self._producer_thread, daemon=True)
        self.consumer = threading.Thread(target=self._consumer_thread, daemon=True)
        
        self.producer.start()
        self.consumer.start()

        # Keep main thread alive while consumer is working
        self.consumer.join()
        print("Processing complete.")

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
