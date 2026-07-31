#include "yuhuang_engine.h"
#include "yuhuang_state.h"
#include "yuhuang_socket.h"
#include "x11_keycheck.h"
#ifdef YUHUANG_HAVE_PANEL
#include "yuhuang_window.h"
#endif
#include <fcitx/inputpanel.h>
#include <fcitx/event.h>
#include <fcitx-config/iniparser.h>
#include <iostream>
#include <fstream>
#include <sstream>
#include <algorithm>
#include <unordered_set>
#include <ctime>

namespace yuhuang {

// ★ PTT 关键事件日志落盘：fcitx5 手动重启后 stdout 常接在
// 已销毁的终端上（两次卡麦事故的引擎日志全部丢失），取证必须不依赖终端。
static void logPtt(const std::string &msg) {
    std::cout << "[YuHuang] " << msg << std::endl;
    static std::ofstream f;
    if (!f.is_open()) {
        const char *home = std::getenv("HOME");
        if (home) {
            f.open(std::string(home) + "/.config/yuhuang/engine.log",
                   std::ios::app);
        }
    }
    if (f.is_open()) {
        char buf[32];
        std::time_t t = std::time(nullptr);
        std::strftime(buf, sizeof(buf), "%F %T", std::localtime(&t));
        f << buf << " " << msg << std::endl;
    }
}

// Known system shortcut conflicts (GNOME / KDE common)
static const std::unordered_set<std::string> kKnownConflicts = {
    "Super_L", "Super_R",
    "Alt_L", "Alt_R",
    "Alt_Tab", "Alt_F2", "Alt_F4",
    "Alt_grave", "Alt_Space",
    "Control_L", "Control_R",
    "Control_Alt_T", "Control_Alt_Delete",
    "Control_Escape",
    "Super_Space",
    "Super_A", "Super_S", "Super_D", "Super_W",
    "XF86Search", "XF86PowerOff",
    "Print", "Scroll_Lock",
};

// ---- Apply config to engine state ----
void YuHuangEngine::applyConfig() {
    triggerKey_ = config_.triggerKey.value();

    std::cout << "[YuHuang] Config loaded: trigger="
              << triggerKey_.toString()
              << ", backend=" << config_.backendSocket.value()
              << ", vad_timeout=" << config_.vadSilenceTimeoutMs.value() << "ms"
              << ", asr_interval=" << config_.asrIntermediateInterval.value()
              << std::endl;

    if (config_.checkConflicts.value()) {
        checkSystemConflict(triggerKey_);
    }

    if (backend_ && backend_->isConnected()) {
        sendConfigToBackend();
    }
}

void YuHuangEngine::checkSystemConflict(const fcitx::Key &key) {
    std::string keyStr = key.toString();
    if (kKnownConflicts.count(keyStr) > 0) {
        std::cerr << "[YuHuang] WARNING: Trigger key '" << keyStr
                  << "' may conflict with a system shortcut!" << std::endl;
        std::cerr << "[YuHuang]   Change it in fcitx5 config tool if needed."
                  << std::endl;
    }
    if (!key.isModifier()) {
        std::cout << "[YuHuang] Trigger key '" << keyStr
                  << "' is a non-modifier key. Will be consumed on press."
                  << std::endl;
    }
}

void YuHuangEngine::sendConfigToBackend() {
    // Convert int (ms) to double (seconds) for backend
    double tempVal = config_.llmTemperature.value() / 100.0;
    double optDelay = config_.llmOptimizeDelayMs.value() / 1000.0;
    double commitDelay = config_.llmAutoCommitDelayMs.value() / 1000.0;
    double vadTimeout = config_.vadSilenceTimeoutMs.value() / 1000.0;
    double interInterval = config_.asrIntermediateInterval.value() / 1000.0;

    std::string cmd =
        R"({"type":"config","llm":)" + std::string(R"({)")
        + R"("enabled":)" + (config_.llmEnabled.value() ? "true" : "false")
        + R"(,"base_url":")" + config_.llmBaseUrl.value() + R"(")"
        + R"(,"api_key":")" + config_.llmApiKey.value() + R"(")"
        + R"(,"model":")" + config_.llmModel.value() + R"(")"
        + R"(,"temperature":)" + std::to_string(tempVal)
        + R"(,"max_tokens":)" + std::to_string(config_.llmMaxTokens.value())
        + R"(,"optimize_delay":)" + std::to_string(optDelay)
        + R"(,"auto_commit_delay":)" + std::to_string(commitDelay)
        + R"(})"
        + R"(,"vad":)" + std::string(R"({)")
        + R"("silence_timeout":)" + std::to_string(vadTimeout)
        + R"(,"intermediate_interval":)" + std::to_string(interInterval)
        + R"(})"
        + R"(,"audio_device":")" + config_.audioDevice.value() + R"(")"
        + R"(})";
    backend_->sendCommand(cmd);
}

// ---- Constructor / Destructor ----
YuHuangEngine::YuHuangEngine(fcitx::Instance *instance)
    : instance_(instance),
      factory_(std::function<YuHuangState*(fcitx::InputContext&)>(
          [this](fcitx::InputContext &ic) -> YuHuangState* {
              return new YuHuangState(this, &ic);
          }
      )) {
    instance->inputContextManager().registerProperty("yuhuangState", &factory_);

    reloadConfig();
    applyConfig();

    backend_ = std::make_unique<BackendClient>(config_.backendSocket.value());

    // 将跨线程调度器挂载到 fcitx5 事件循环
    eventDispatcher_.attach(&instance_->eventLoop());

    // 注册回调（无论当前是否已连接，保证后续重连时回调依然有效）
    backend_->setResultCallback([this](const std::string &type,
                                        const std::string &text,
                                        const std::string &raw_msg) {
        std::cout << "[YuHuang] CB recv: type=" << type
                  << " text=" << text.substr(0, 40) << std::endl;

        // 通过 EventDispatcher 调度到 fcitx5 主线程执行
        eventDispatcher_.schedule([this, type, text, raw_msg]() {
            std::cout << "[YuHuang] CB exec on main: type=" << type
                      << " text=" << text.substr(0, 40) << std::endl;

            YuHuangState *state = currentState();
            if (!state) {
                std::cout << "[YuHuang] currentState() returned nullptr!" << std::endl;
                return;
            }

            std::cout << "[YuHuang] Got state, ic="
                      << (state->inputContext() ? "OK" : "NULL")
                      << " program="
                      << (state->inputContext() ? state->inputContext()->program() : "?")
                      << std::endl;

            // 简易 JSON 字段提取（无三方库依赖）
            auto extractField = [](const std::string &json,
                                   const std::string &field) -> std::string {
                std::string key = "\"" + field + "\":";
                size_t pos = json.find(key);
                if (pos == std::string::npos) return "";
                pos += key.size();
                while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t'))
                    pos++;
                if (pos >= json.size()) return "";
                if (json[pos] == '"') {
                    pos++;
                    std::string result;
                    while (pos < json.size()) {
                        if (json[pos] == '\\' && pos + 1 < json.size()) {
                            result += json[pos + 1];
                            pos += 2;
                        } else if (json[pos] == '"') {
                            break;
                        } else {
                            result += json[pos];
                            pos++;
                        }
                    }
                    return result;
                }
                size_t end = json.find_first_of(",}]}\n", pos);
                if (end == std::string::npos) return json.substr(pos);
                std::string val = json.substr(pos, end - pos);
                size_t s = val.find_first_not_of(" \t");
                if (s == std::string::npos) return "";
                size_t e = val.find_last_not_of(" \t");
                return val.substr(s, e - s + 1);
            };

            if (type == "preedit") {
                // 分段预编辑（三色渲染）— 从 flat JSON 提取各颜色段
                std::string green = extractField(raw_msg, "green");
                std::string yellow = extractField(raw_msg, "yellow");
                std::string red = extractField(raw_msg, "red");
                std::cout << "[YuHuang] preedit: green=" << green.size()
                          << " yellow=" << yellow.size()
                          << " red=" << red.size()
                          << " total=" << (green.size()+yellow.size()+red.size())
                          << " raw_len=" << raw_msg.size() << std::endl;
                std::vector<TextSegment> segments;
                if (!green.empty()) segments.push_back({green, "green"});
                if (!yellow.empty()) segments.push_back({yellow, "yellow"});
                if (!red.empty()) segments.push_back({red, "red"});
                std::cout << "[YuHuang] preedit: segments=" << segments.size()
                          << " ic=" << (state->inputContext() ? "OK" : "NULL")
                          << std::endl;
                state->updatePreedit(segments);
            } else if (type == "intermediate") {
                state->updatePreedit(text);
            } else if (type == "final") {
                state->updatePreedit(text);
            } else if (type == "optimized") {
                state->updatePreedit(text);
            } else if (type == "commit") {
                state->commitText(text);
            } else if (type == "reset") {
                state->reset();
            } else if (type == "error") {
                state->updatePreedit("[! " + text + "]");
            }
        });
    });

    if (backend_->connect()) {
        backend_->startReceiveLoop();
        std::cout << "[YuHuang] Backend connected, sending config..." << std::endl;
        sendConfigToBackend();
        std::cout << "[YuHuang] Ready for push-to-talk (hold "
                  << triggerKey_.toString() << " to speak)" << std::endl;
    } else {
        std::cerr << "[YuHuang] Warning: Backend not available at "
                  << config_.backendSocket.value() << std::endl;
        std::cerr << "[YuHuang] Start it with: yuhuang-backend" << std::endl;
    }
}

YuHuangEngine::~YuHuangEngine() {
    stopPttWatchdog();
    if (backend_) {
        backend_->disconnect();
    }
}

// ---- Activate / Deactivate ----
void YuHuangEngine::activate(const fcitx::InputMethodEntry &entry,
                               fcitx::InputContextEvent &event) {
    FCITX_UNUSED(entry);
    auto *ic = event.inputContext();
    std::cout << "[YuHuang] Activated on: "
              << (ic ? ic->program() : "?") << std::endl;

    if (backend_ && !backend_->isConnected()) {
        // 完全断开旧连接（关闭 fd、设 connected_=false），
        // 否则 connect() 会因为 connected_=true 直接返回，复用已 shutdown 的旧 fd
        backend_->disconnect();
        if (backend_->connect()) {
            backend_->startReceiveLoop();
            sendConfigToBackend();
            std::cout << "[YuHuang] Backend reconnected on activate" << std::endl;
        }
    }

    // Clear any leftover preedit on activation
    if (ic) {
        auto *state = ic->propertyFor(&factory_);
        if (state) {
            state->reset();
        }
    }
}

void YuHuangEngine::deactivate(const fcitx::InputMethodEntry &entry,
                                 fcitx::InputContextEvent &event) {
    FCITX_UNUSED(entry);
    auto *ic = event.inputContext();
    std::cout << "[YuHuang] Deactivated on: "
              << (ic ? ic->program() : "?") << std::endl;

    // ★ 统一走 stopListeningInternal（顺带修复旧版不清
    // triggerPressed_ 的潜伏 bug：焦点切走后引擎仍认为触发键按着）
    stopListeningInternal("focus-out");
}

// ---- ★ PTT 停止统一入口与物理键盘看门狗 ----

void YuHuangEngine::stopListeningInternal(const char *reason) {
    // 注意：不在这里销毁 pttWatchdog_——本函数可能在看门狗自身回调内
    // 被调用，回调内销毁自身事件源是 UB。回调见 !triggerPressed_
    // 后不续期自然停摆，下次 startPttWatchdog 会 reset 重建。
    triggerPressed_ = false;
    watchdogMisses_ = 0;
    x11WatchKeyEnd();  // 退订 raw 事件，防止在连接上无限堆积
    if (!listening_) return;
    listening_ = false;
    logPtt(std::string("PTT: stop listening (") + reason + ")");
    if (backend_ && backend_->isConnected()) {
        backend_->sendCommand("{\"type\":\"stop_listening\"}");
    }
}

void YuHuangEngine::startPttWatchdog() {
    // X11 不可用（Wayland 会话/无 DISPLAY）时不启用，保留另外两层防御
    if (x11WatchKeyBegin(triggerKey_.sym()) < 0) {
        logPtt("PTT watchdog unavailable (no X11), "
               "rescue-press is the only defense");
        return;
    }

    watchdogMisses_ = 0;
    pttWatchdog_.reset();  // 销毁旧事件源（此时不在其回调内，安全）
    pttWatchdog_ = instance_->eventLoop().addTimeEvent(
        CLOCK_MONOTONIC, fcitx::now(CLOCK_MONOTONIC) + 200000, 10000,
        [this](fcitx::EventSourceTime *source, uint64_t) {
            if (!triggerPressed_) {
                return true;  // 已正常停止：不续期，自然停摆
            }
            int down = x11WatchKeyPoll();
            if (down == 0) {
                // 连续 2 次（~400ms）未按下才判定丢松键，防瞬时竞态
                if (++watchdogMisses_ >= 2) {
                    logPtt("PTT watchdog: physical key released "
                           "but no release event received "
                           "(grabbed by compositor?) -> force stop");
                    stopListeningInternal("watchdog");
                    return true;  // 已停止，不续期
                }
            } else {
                watchdogMisses_ = 0;  // 仍按着（或查询失败）：重置计数
            }
            source->setNextInterval(200000);  // 续期 200ms
            source->setOneShot();
            return true;
        });
}

void YuHuangEngine::stopPttWatchdog() {
    // 仅供回调外部使用（析构/重配置）；回调内部靠不续期自然停摆
    pttWatchdog_.reset();
    watchdogMisses_ = 0;
}

// ---- Key Event (PTT Core) ----
void YuHuangEngine::keyEvent(const fcitx::InputMethodEntry &entry,
                               fcitx::KeyEvent &keyEvent) {
    FCITX_UNUSED(entry);

    auto *ic = keyEvent.inputContext();
    if (!ic) return;

    const fcitx::Key &key = keyEvent.key();
    bool isRelease = keyEvent.isRelease();

    // ★ 自动重连：backend 断连后在任意按键时尝试重连
    if (backend_ && !backend_->isConnected()) {
        backend_->disconnect();  // 清理旧 fd
        if (backend_->connect()) {
            backend_->startReceiveLoop();
            sendConfigToBackend();
            std::cout << "[YuHuang] Backend auto-reconnected on key event" << std::endl;
        }
    }

    // Log every key event for debugging (remove in production?)
    std::cout << "[YuHuang] keyEvent: sym=0x" << std::hex << key.sym()
              << std::dec << " key=" << key.toString()
              << " release=" << isRelease
              << " mods=0x" << std::hex << key.states()
              << std::dec << std::endl;

    // PTT trigger key handling
    // Use sym comparison + fuzzy states match (release events add own modifier)
    if (key.sym() == triggerKey_.sym()) {
        if (isRelease) {
            if (triggerPressed_) {
                logPtt("PTT: trigger released");
                stopListeningInternal("release");
                keyEvent.filterAndAccept();
            }
        } else {
            // ★ 重复按下救援：修饰键无自动重复，triggerPressed_
            // 已真时再收到按下 = 松键事件被合成器吞了，用户在补按
            // 救援 -> 当停止处理。限定 isModifier()：非修饰触发键有
            // 自动重复，会误杀正常长按。
            if (triggerPressed_ && key.isModifier()) {
                logPtt("PTT: duplicate press while held "
                       "(release event was lost) -> rescue stop");
                stopListeningInternal("rescue-press");
                keyEvent.filterAndAccept();
                return;
            }
            triggerPressed_ = true;
            listening_ = true;
            logPtt("PTT: trigger pressed -> start listening");
            if (backend_ && backend_->isConnected()) {
                backend_->sendCommand("{\"type\":\"start_listening\"}");
            }
            startPttWatchdog();  // ★ 监控物理键位，防松键事件丢失
            keyEvent.filterAndAccept();
        }
        return;
    }

    // If trigger is pressed, pass other keys through
    if (triggerPressed_) {
        return;
    }

    // Ignore pure release events for non-trigger keys
    if (isRelease) return;

    // Esc: cancel preedit
    if (key.sym() == vk::Escape) {
        auto *state = ic->propertyFor(&factory_);
        state->reset();
        if (backend_ && backend_->isConnected()) {
            backend_->sendCommand("{\"type\":\"reset\"}");
        }
        keyEvent.filterAndAccept();
        return;
    }

    // Return: commit current preedit
    if (key.sym() == vk::Return) {
        auto *state = ic->propertyFor(&factory_);
        // 三区文本画在 fcitx 面板上，应用内嵌 preedit 是空的，草稿全文取自 state
        std::string text = state->pendingText();
        if (text.empty()) {
            auto &inputPanel = ic->inputPanel();
            text = inputPanel.clientPreedit().toString();
            if (text.empty()) text = inputPanel.preedit().toString();
        }
        if (!text.empty()) {
            state->commitText(text);
            if (backend_ && backend_->isConnected()) {
                backend_->sendCommand("{\"type\":\"commit_now\"}");
            }
            keyEvent.filterAndAccept();
        }
        return;
    }

    // F5: force LLM optimization
    if (key.sym() == vk::F5) {
        if (backend_ && backend_->isConnected()) {
            backend_->sendCommand("{\"type\":\"optimize_now\"}");
        }
        keyEvent.filterAndAccept();
        return;
    }

    // F6: toggle listening mode (manual override)
    if (key.sym() == vk::F6) {
        listening_ = !listening_;
        if (backend_ && backend_->isConnected()) {
            backend_->sendCommand(listening_
                ? "{\"type\":\"start_listening\"}"
                : "{\"type\":\"stop_listening\"}");
        }
        auto *state = ic->propertyFor(&factory_);
        state->updatePreedit(listening_ ? "[Listening...]" : "");
        keyEvent.filterAndAccept();
        return;
    }

    // Other keys: pass through
}

// ---- Reset ----
void YuHuangEngine::reset(const fcitx::InputMethodEntry &entry,
                           fcitx::InputContextEvent &event) {
    FCITX_UNUSED(entry);
    auto *state = event.inputContext()->propertyFor(&factory_);
    state->reset();
}

// ---- Current focused state ----
YuHuangState *YuHuangEngine::currentState() {
    auto *focusedIC = instance_->lastFocusedInputContext();
    if (!focusedIC) return nullptr;
    return focusedIC->propertyFor(&factory_);
}

// ---- 自绘悬浮窗 ----
PanelWindow *YuHuangEngine::panel() {
#ifdef YUHUANG_HAVE_PANEL
    if (!panelTried_) {
        panelTried_ = true;   // 只试一次，连不上 X 就永远走候选栏回退
        auto w = std::make_unique<PanelWindow>();
        if (w->available()) {
            panelWindow_ = std::move(w);
            // Expose 重绘等 X 事件挂在 fcitx 主事件循环里处理
            panelIO_ = instance_->eventLoop().addIOEvent(
                panelWindow_->fd(), fcitx::IOEventFlag::In,
                [this](fcitx::EventSourceIO *, int, fcitx::IOEventFlags) {
                    panelWindow_->processEvents();
                    return true;
                });
            logPtt("panel: self-drawn floating window ready");
        } else {
            logPtt("panel: X unavailable, fall back to candidate bar");
        }
    }
    return panelWindow_.get();
#else
    return nullptr;
#endif
}

PanelWindow *YuHuangEngine::panelIfCreated() {
#ifdef YUHUANG_HAVE_PANEL
    return panelWindow_.get();
#else
    return nullptr;
#endif
}

// Register addon factory
FCITX_ADDON_FACTORY(YuHuangEngineFactory);

} // namespace yuhuang
