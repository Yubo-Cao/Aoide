// WebRTC APM 1.3: 16 kHz mono noise suppression (+ optional AGC2), no render/AEC path.
#include <modules/audio_processing/include/audio_processing.h>
#include <cstdint>
#include <new>

struct Denoiser {
    rtc::scoped_refptr<webrtc::AudioProcessing> apm;
    webrtc::StreamConfig format{16000, 1};
    // level: 0 low, 1 moderate, 2 high, 3 very high.
    Denoiser(int level = 1, bool high_pass = true, bool agc = false)
        : apm(webrtc::AudioProcessingBuilder().Create()) {
        using NS = decltype(webrtc::AudioProcessing::Config::noise_suppression);
        webrtc::AudioProcessing::Config cfg;
        cfg.echo_canceller.enabled = false;
        cfg.high_pass_filter.enabled = high_pass;
        cfg.noise_suppression.enabled = true;
        cfg.noise_suppression.level = static_cast<NS::Level>(level);
        cfg.gain_controller1.enabled = false;
        // AGC2 adaptive digital gain: raises quiet/distant speech, never above 0 dBFS.
        cfg.gain_controller2.enabled = agc;
        cfg.gain_controller2.adaptive_digital.enabled = agc;
        cfg.residual_echo_detector.enabled = false;
        apm->ApplyConfig(cfg);
    }
};

extern "C" {
void* yh_denoise_create() { try { return new Denoiser(); } catch (...) { return nullptr; } }
void* yh_denoise_create_ex(int level, int high_pass, int agc) {
    if (level < 0 || level > 3) return nullptr;
    try { return new Denoiser(level, high_pass != 0, agc != 0); } catch (...) { return nullptr; }
}
void yh_denoise_free(void* p) { delete static_cast<Denoiser*>(p); }
int yh_denoise_reset(void* p) { return static_cast<Denoiser*>(p)->apm->Initialize(); }
int yh_denoise_process(void* p, const int16_t* src, int16_t* dst, int count) {
    if (!p || count % 160) return -1;
    auto* d = static_cast<Denoiser*>(p);
    for (int i = 0; i < count; i += 160) {
        int result = d->apm->ProcessStream(src + i, d->format, d->format, dst + i);
        if (result) return result;
    }
    return 0;
}
}
