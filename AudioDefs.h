#pragma once
#include <cstdint>
#include <cstddef>

const int SAMPLE_RATE = 48000;
const int CHANNELS = 2;
const int BLK_SAMP = 14;
const int BLK_BYTES = 8;

const int   MIX_BLOCK_FRAMES = 448;
const size_t PCM_BUFFER_SECONDS = 12;
const size_t PCM_RING_FRAMES = SAMPLE_RATE * PCM_BUFFER_SECONDS;
const size_t DAC_BUFFER_SECONDS = 4;
const size_t DAC_BUFFER_FRAMES = SAMPLE_RATE * DAC_BUFFER_SECONDS;
const size_t STREAM_CHUNK_FRAMES = MIX_BLOCK_FRAMES;
const uint16_t DEFAULT_STREAM_PORT = 8765;
const int HTTP_BACKLOG = 32;
const uint32_t PCM_READ_TIMEOUT_MS = 200;

struct WavHeader {
    char riff[4] = {'R', 'I', 'F', 'F'};
    uint32_t file_size = 0xFFFFFFFF;
    char wave[4] = {'W', 'A', 'V', 'E'};
    char fmt[4] = {'f', 'm', 't', ' '};
    uint32_t fmt_size = 16;
    uint16_t format = 1;
    uint16_t channels = CHANNELS;
    uint32_t sample_rate = SAMPLE_RATE;
    uint32_t byte_rate = SAMPLE_RATE * CHANNELS * sizeof(int16_t);
    uint16_t block_align = CHANNELS * sizeof(int16_t);
    uint16_t bits_per_sample = 16;
    char data[4] = {'d', 'a', 't', 'a'};
    uint32_t data_size = 0xFFFFFFFF;
};

const size_t WAV_HEADER_SIZE = sizeof(WavHeader);
const uint64_t STREAM_CONTENT_LENGTH = 0xFFFFFFFFull;

