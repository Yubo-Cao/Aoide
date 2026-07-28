#include "yuhuang_engine.h"
#include "yuhuang_state.h"
#include "yuhuang_socket.h"
#include <fcitx/inputpanel.h>
#include <fcitx/event.h>
#include <fcitx-config/iniparser.h>
#include <iostream>
#include <fstream>
#include <sstream>
#include <algorithm>
#include <unordered_set>

namespace yuhuang {

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
                // v3.0 分段预编辑（三色渲染）— 从 flat JSON 提取各颜色段
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

    if (listening_) {
        listening_ = false;
        if (backend_ && backend_->isConnected()) {
            backend_->sendCommand("{\"type\":\"stop_listening\"}");
        }
    }
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
                triggerPressed_ = false;
                std::cout << "[YuHuang] PTT: trigger released -> stop listening"
                          << std::endl;
                if (listening_) {
                    listening_ = false;
                    if (backend_ && backend_->isConnected()) {
                        backend_->sendCommand("{\"type\":\"stop_listening\"}");
                    }
                }
                keyEvent.filterAndAccept();
            }
        } else {
            triggerPressed_ = true;
            listening_ = true;
            std::cout << "[YuHuang] PTT: trigger pressed -> start listening"
                      << std::endl;
            if (backend_ && backend_->isConnected()) {
                backend_->sendCommand("{\"type\":\"start_listening\"}");
            }
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
        auto &inputPanel = ic->inputPanel();
        std::string text = inputPanel.clientPreedit().toString();
        if (text.empty()) text = inputPanel.preedit().toString();
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

// Register addon factory
FCITX_ADDON_FACTORY(YuHuangEngineFactory);

} // namespace yuhuang
