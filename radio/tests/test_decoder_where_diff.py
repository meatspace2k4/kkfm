#!/usr/bin/env python3
"""
Find where decoder differences start occurring
"""

import unittest
import os
import sys
import numpy as np
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import mono
from radio import config as ref_config
from radio.decoder import BWAVParser as RefBWAVParser, ADPCMState as RefADPCMState


class TestFindDifference(unittest.TestCase):
    """Find where the difference starts"""

    def setUp(self):
        self.sound_dir = Path(ref_config.SOUND_FILES_PATH)
        if not self.sound_dir.exists():
            self.skipTest(f"Sound directory not found: {self.sound_dir}")
        
        test_files = list(self.sound_dir.glob("BGM_Audio_*.bwav"))[:1]
        if not test_files:
            self.skipTest("No test audio files found")
        
        self.test_file = test_files[0]

    def test_find_first_difference(self):
        """Find the exact sample where difference first appears."""
        mono_ctx = mono.BWAVParser.parse(self.test_file, skip_crc=True)
        ref_ctx = RefBWAVParser.parse_file(str(self.test_file), skip_crc=True)
        
        if not mono_ctx or not ref_ctx:
            self.skipTest("Failed to parse test file")
        
        mono_dec = mono.Decoder(mono_ctx)
        ref_dec = RefADPCMState(ref_ctx)
        
        mono_samples = []
        ref_samples = []
        sample_idx = 0
        max_samples_to_check = 10000  # Check first 10k samples
        
        while sample_idx < max_samples_to_check:
            try:
                mono_chunk = mono_dec.__next__()
            except StopIteration:
                break
            
            ref_chunk = ref_dec.read(len(mono_chunk))
            
            if len(mono_chunk) == 0 or len(ref_chunk) == 0:
                break
            
            # Compare sample by sample
            for i in range(min(len(mono_chunk), len(ref_chunk))):
                mono_val = int(mono_chunk[i])
                ref_val = int(ref_chunk[i])
                
                if abs(mono_val - ref_val) > 1:  # More than rounding difference
                    print(f"\nFirst significant difference at sample {sample_idx + i}:")
                    print(f"  Mono: {mono_val}")
                    print(f"  Ref:  {ref_val}")
                    print(f"  Diff: {mono_val - ref_val}")
                    print(f"  Block: {(sample_idx + i) // 14}")
                    print(f"  In-block position: {(sample_idx + i) % 14}")
                    return
                
                mono_samples.append(mono_val)
                ref_samples.append(ref_val)
            
            sample_idx += len(mono_chunk)
        
        print(f"\nNo significant difference found in first {sample_idx} samples")
        print("Decoders match perfectly!")

    def test_compare_around_loop(self):
        """Compare samples around loop transition."""
        mono_ctx = mono.BWAVParser.parse(self.test_file, skip_crc=True)
        ref_ctx = RefBWAVParser.parse_file(str(self.test_file), skip_crc=True)
        
        if not mono_ctx or not ref_ctx:
            self.skipTest("Failed to parse test file")
        
        loop_end = mono_ctx.info.loop_end
        if loop_end == 0xFFFFFFFF:
            self.skipTest("File doesn't loop")
        
        mono_dec = mono.Decoder(mono_ctx)
        ref_dec = RefADPCMState(ref_ctx)
        
        # Read up to loop_end - 100, then compare around loop
        target_samples = loop_end - 100
        sample_idx = 0
        
        while sample_idx < target_samples:
            try:
                mono_chunk = mono_dec.__next__()
                ref_chunk = ref_dec.read(len(mono_chunk))
                sample_idx += len(mono_chunk)
            except StopIteration:
                break
        
        # Now compare samples around loop transition
        print(f"\nComparing samples around loop_end={loop_end}:")
        for offset in [-10, -5, -1, 0, 1, 5, 10]:
            # Read to target position
            while sample_idx < loop_end + offset:
                try:
                    mono_chunk = mono_dec.__next__()
                    ref_chunk = ref_dec.read(len(mono_chunk))
                    if len(mono_chunk) > 0:
                        mono_val = mono_chunk[0]
                        ref_val = ref_chunk[0] if len(ref_chunk) > 0 else 0
                        diff = int(mono_val) - int(ref_val)
                        if abs(diff) > 1:
                            print(f"  Sample {sample_idx}: mono={mono_val}, ref={ref_val}, diff={diff}")
                        sample_idx += len(mono_chunk)
                except StopIteration:
                    break


if __name__ == '__main__':
    unittest.main(verbosity=2)

