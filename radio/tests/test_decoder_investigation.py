#!/usr/bin/env python3
"""
Investigate decoder output differences between mono.py and refactored decoder
"""

import unittest
import os
import sys
import numpy as np
from pathlib import Path

# Add the project root to the path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import mono
from radio import config as ref_config
from radio.decoder import BWAVParser as RefBWAVParser, ADPCMState as RefADPCMState


class TestDecoderInvestigation(unittest.TestCase):
    """Investigate where decoder differences come from"""

    def setUp(self):
        """Set up test audio file."""
        self.sound_dir = Path(ref_config.SOUND_FILES_PATH)
        if not self.sound_dir.exists():
            self.skipTest(f"Sound directory not found: {self.sound_dir}")
        
        # Find a test file
        test_files = list(self.sound_dir.glob("BGM_Audio_*.bwav"))[:1]
        if not test_files:
            self.skipTest("No test audio files found")
        
        self.test_file = test_files[0]

    def test_compare_first_samples(self):
        """Compare first few samples to see where difference starts."""
        mono_ctx = mono.BWAVParser.parse(self.test_file, skip_crc=True)
        ref_ctx = RefBWAVParser.parse_file(str(self.test_file), skip_crc=True)
        
        if not mono_ctx or not ref_ctx:
            self.skipTest("Failed to parse test file")
        
        # Create decoders
        mono_dec = mono.Decoder(mono_ctx)
        ref_dec = RefADPCMState(ref_ctx)
        
        # Read first chunk
        try:
            mono_chunk = mono_dec.__next__()
        except StopIteration:
            self.skipTest("Mono decoder finished immediately")
        
        ref_chunk = ref_dec.read(len(mono_chunk))
        
        # Compare first 20 samples
        compare_len = min(20, len(mono_chunk), len(ref_chunk))
        mono_arr = np.array(mono_chunk[:compare_len], dtype=np.int16)
        ref_arr = ref_chunk[:compare_len].astype(np.int16)
        
        diff = mono_arr.astype(np.int32) - ref_arr.astype(np.int32)
        
        print(f"\nFirst {compare_len} samples comparison:")
        print(f"Mono:  {mono_arr[:compare_len]}")
        print(f"Ref:   {ref_arr[:compare_len]}")
        print(f"Diff:  {diff[:compare_len]}")
        print(f"Max diff: {np.max(np.abs(diff))}")
        
        # Check if difference starts immediately or builds up
        first_diff_idx = None
        for i in range(compare_len):
            if abs(diff[i]) > 0:
                first_diff_idx = i
                break
        
        if first_diff_idx is not None:
            print(f"\nFirst difference at sample index {first_diff_idx}")
            print(f"  Mono[{first_diff_idx}] = {mono_arr[first_diff_idx]}")
            print(f"  Ref[{first_diff_idx}] = {ref_arr[first_diff_idx]}")
            print(f"  Difference = {diff[first_diff_idx]}")
        else:
            print("\nFirst 20 samples match!")

    def test_compare_context_parsing(self):
        """Compare if context parsing differs."""
        mono_ctx = mono.BWAVParser.parse(self.test_file, skip_crc=True)
        ref_ctx = RefBWAVParser.parse_file(str(self.test_file), skip_crc=True)
        
        if not mono_ctx or not ref_ctx:
            self.skipTest("Failed to parse test file")
        
        # Compare key fields
        print(f"\nContext comparison:")
        print(f"Sample rate: mono={mono_ctx.info.rate}, ref={ref_ctx.info.sample_rate}")
        print(f"Total samples: mono={mono_ctx.info.total_samples}, ref={ref_ctx.info.num_samples}")
        print(f"Loop start: mono={mono_ctx.info.loop_start}, ref={ref_ctx.info.loop_start}")
        print(f"Loop end: mono={mono_ctx.info.loop_end}, ref={ref_ctx.info.loop_end}")
        # Check history samples (different attribute names)
        mono_h1 = getattr(mono_ctx.info, 'hist1', getattr(mono_ctx.info, 'history_sample_1', None))
        mono_h2 = getattr(mono_ctx.info, 'hist2', getattr(mono_ctx.info, 'history_sample_2', None))
        ref_h1 = ref_ctx.info.history_sample_1
        ref_h2 = ref_ctx.info.history_sample_2
        
        print(f"History 1: mono={mono_h1}, ref={ref_h1}")
        print(f"History 2: mono={mono_h2}, ref={ref_h2}")
        
        # Check if histories match
        if mono_h1 is not None:
            self.assertEqual(mono_h1, ref_h1, "History sample 1 should match")
        if mono_h2 is not None:
            self.assertEqual(mono_h2, ref_h2, "History sample 2 should match")

    def test_compare_decoder_state(self):
        """Compare decoder initial state."""
        mono_ctx = mono.BWAVParser.parse(self.test_file, skip_crc=True)
        ref_ctx = RefBWAVParser.parse_file(str(self.test_file), skip_crc=True)
        
        if not mono_ctx or not ref_ctx:
            self.skipTest("Failed to parse test file")
        
        mono_dec = mono.Decoder(mono_ctx)
        ref_dec = RefADPCMState(ref_ctx)
        
        print(f"\nDecoder state comparison:")
        print(f"Mono decoder type: {type(mono_dec)}")
        print(f"Ref decoder type: {type(ref_dec)}")
        
        # Check if mono decoder has same attributes
        if hasattr(mono_dec, 'hist1'):
            print(f"Mono hist1: {mono_dec.hist1}, Ref hist1: {ref_dec.hist1}")
            print(f"Mono hist2: {mono_dec.hist2}, Ref hist2: {ref_dec.hist2}")
            print(f"Mono coef1: {mono_dec.coef1}, Ref coef1: {ref_dec.coef1}")
            print(f"Mono coef2: {mono_dec.coef2}, Ref coef2: {ref_dec.coef2}")

    def test_compare_block_boundaries(self):
        """Compare samples at block boundaries (where headers are read)."""
        mono_ctx = mono.BWAVParser.parse(self.test_file, skip_crc=True)
        ref_ctx = RefBWAVParser.parse_file(str(self.test_file), skip_crc=True)
        
        if not mono_ctx or not ref_ctx:
            self.skipTest("Failed to parse test file")
        
        mono_dec = mono.Decoder(mono_ctx)
        ref_dec = RefADPCMState(ref_ctx)
        
        # Read samples around block boundaries (every 14 samples)
        block_size = 14
        samples_to_check = [0, 13, 14, 27, 28, 41, 42]
        
        mono_samples = []
        ref_samples = []
        
        sample_idx = 0
        for target_idx in samples_to_check:
            # Read up to target
            while sample_idx < target_idx:
                try:
                    mono_chunk = mono_dec.__next__()
                    ref_chunk = ref_dec.read(len(mono_chunk))
                    sample_idx += len(mono_chunk)
                except StopIteration:
                    break
            
            if sample_idx >= target_idx:
                # Get the specific sample
                try:
                    mono_chunk = mono_dec.__next__()
                    ref_chunk = ref_dec.read(len(mono_chunk))
                    if len(mono_chunk) > 0 and len(ref_chunk) > 0:
                        mono_samples.append(mono_chunk[0])
                        ref_samples.append(ref_chunk[0])
                        sample_idx += 1
                except StopIteration:
                    break
        
        if mono_samples and ref_samples:
            print(f"\nBlock boundary samples:")
            for i, (m, r) in enumerate(zip(mono_samples[:10], ref_samples[:10])):
                diff = int(m) - int(r)
                print(f"  Sample {samples_to_check[i]}: mono={m}, ref={r}, diff={diff}")


if __name__ == '__main__':
    unittest.main(verbosity=2)

