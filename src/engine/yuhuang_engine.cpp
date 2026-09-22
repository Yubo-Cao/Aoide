#include "yuhuang_engine.h"
#include "yuhuang_state.h"
#include "yuhuang_socket.h"
#include "x11_keycheck.h"
#ifdef YUHUANG_HAVE_PANEL
#include "yuhuang_window.h"
#endif
#include <fcitx/inputpanel.h>
#include <fcitx/event.h>
#include <fcitx-utils/capabilityflags.h>
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

// 触发键里出现过的全部修饰键取并集。
fcitx::KeyStates YuHuangEngine::triggerModifierUnion() const {
    fcitx::KeyStates all;
    for (const auto &k : triggerKeys_) {
        all |= k.states();
        // 纯修饰键触发（如 Control+Super_L）时，作为主键的那个修饰键本身
        // 不在 states() 里，要从 sym 补回来，否则按住期间它会被当成打断。
        all |= fcitx::Key::keySymToStates(k.sym());
    }
    return all;
}

std::string YuHuangEngine::triggerKeysToString() const {
    std::string out;
    for (const auto &k : triggerKeys_) {
        if (!out.empty()) out += " / ";
        out += k.toString();
    }
    return out.empty() ? std::string("<none>") : out;
}

// sym 相同 + 实际修饰键包含该触发键要求的修饰键。
// 只比 sym 会误吞普通键（实录：TriggerKey=Ctrl+Alt+Shift+Y 后 Shift+Y
// 输入大写失效，因为 Y 的 sym 被当成 PTT 吞掉）。
bool YuHuangEngine::isTriggerKey(const fcitx::Key &k) const {
    for (const auto &t : triggerKeys_) {
        if (k.sym() != t.sym()) continue;
        auto required = t.states();
        if ((k.states() & required) == required) return true;
    }
    return false;
}

// ---- Apply config to engine state ----
void YuHuangEngine::applyConfig() {
    triggerKeys_ = config_.triggerKey.value();
    // 空列表意味着 PTT 完全没法触发，而最容易走到这里的情况是升级：
    // 老配置里的标量 TriggerKey=... 不会被列表 marshaller 解析（它要的是
    // TriggerKey/0=...），于是静默变成空列表。宁可退回默认键并且吵一声，
    // 也不要让用户对着一个永远不响应的输入法。
    if (triggerKeys_.empty()) {
        triggerKeys_ = fcitx::KeyList{fcitx::Key("Pause")};
        std::cerr << "[YuHuang] WARNING: TriggerKey is empty -- falling back "
                     "to Pause. A scalar 'TriggerKey=<key>' from an older "
                     "config is not read as a list; write 'TriggerKey/0=<key>' "
                     "instead, or set it once in fcitx5-configtool."
                  << std::endl;
    }
    triggerMode_ = config_.triggerMode.value();

    std::cout << "[YuHuang] Config loaded: trigger="
              << triggerKeysToString()
              << ", mode="
              << (triggerMode_ == PttMode::Toggle ? "toggle" : "hold")
              << ", backend=" << config_.backendSocket.value()
              << ", vad_timeout=" << config_.vadSilenceTimeoutMs.value() << "ms"
              << ", asr_interval=" << config_.asrIntermediateInterval.value()
              << std::endl;

    if (config_.checkConflicts.value()) {
        for (const auto &k : triggerKeys_) {
            checkSystemConflict(k);
        }
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

// 断连时尝试重连一次。已连接则什么都不做，返回当前是否已连上。
bool YuHuangEngine::tryReconnect() {
    if (!backend_) return false;
    if (backend_->isConnected()) return true;
    backend_->disconnect();
    if (!backend_->connect()) return false;
    backend_->startReceiveLoop();
    isFinalizing_ = false;
    isRecording_ = false;
    sendConfigToBackend();
    std::cout << "[YuHuang] Backend reconnected" << std::endl;
    return true;
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

    backend_ = std::make_unique<BackendClient>(config_.backendSocket.value());

    // 将跨线程调度器挂载到 fcitx5 事件循环
    eventDispatcher_.attach(&instance_->eventLoop());

    // 后台重连：后端是个会独立重启的 systemd 用户服务（升级、换模型、
    // Restart=on-failure 自愈），重启后 addon 必须自己接回去，否则输入法
    // 表面上正常、按下去却毫无反应。2s 一轮，连上后回调即变成空操作。
    reconnectTimer_ = instance_->eventLoop().addTimeEvent(
        CLOCK_MONOTONIC, fcitx::now(CLOCK_MONOTONIC) + 2000000, 100000,
        [this](fcitx::EventSourceTime *source, uint64_t) {
            tryReconnect();
            source->setNextInterval(2000000);
            source->setOneShot();
            return true;
        });

    // ★ 注册全局按键监听（PreInputMethod 阶段，在拼音等输入法之前拿到按键）
    // PTT 专用键 → 开始/结束录音；其他键+录音中 → 打断暂扣；否则放行给拼音
    keyWatcher_ = instance_->watchEvent(
        fcitx::EventType::InputContextKeyEvent,
        fcitx::EventWatcherPhase::PreInputMethod,
        [this](fcitx::Event &event) {
            onGlobalKey(static_cast<fcitx::KeyEvent &>(event));
        });

    // ★ 注册焦点变化监听（焦点切走时若正在录音 → 打断收尾）
    focusWatcher_ = instance_->watchEvent(
        fcitx::EventType::InputContextFocusOut,
        fcitx::EventWatcherPhase::PreInputMethod,
        [this](fcitx::Event &event) {
            onFocusOut(static_cast<fcitx::InputContextEvent &>(event));
        });

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

            if (type == "finalized") {
                isFinalizing_ = false;
                return;
            }
            // ★ interrupt_done 不依赖 state：放行被暂扣的打断按键是引擎级操作
            if (type == "interrupt_done") {
                isFinalizing_ = false;
                releasePendingKey();
                return;
            }
            if (discardPendingResult_ && (type == "replace" || type == "commit" ||
                    type == "preedit" || type == "interrupt_commit")) {
                return;
            }

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

            auto extractField = jsonField;

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
                if (segments.empty() && isRecording_) {
                    state->showStatus("正在聆听…");
                } else {
                    state->updatePreedit(segments);
                }
            } else if (type == "intermediate") {
                state->updatePreedit(text);
            } else if (type == "final") {
                state->updatePreedit(text);
            } else if (type == "optimized") {
                state->updatePreedit(text);
            } else if (type == "commit") {
                state->commitSmart(text);
            } else if (type == "interrupt_commit") {
                // ★ 打断收尾：假上屏通道拼接 fakeCommitted + 剩余真上屏；
                //   真上屏通道直接上屏剩余。不做删除重推（光标即将移走）。
                state->interruptCommit(text);
            } else if (type == "replace") {
                // ★ 全文终审上屏：前端按应用能力自选真/假上屏通道
                std::string delStr = extractField(raw_msg, "delete_chars");
                std::string fallback = extractField(raw_msg, "fallback_text");
                int delChars = 0;
                if (!delStr.empty()) {
                    try {
                        delChars = std::stoi(delStr);
                    } catch (...) {
                        delChars = 0;
                    }
                }
                state->replaceSmart(delChars, text, fallback);
                std::cout << "[YuHuang] replace: delete=" << delChars
                          << " text=" << text.size() << "B"
                          << " preedit_channel=" << state->usePreeditChannel()
                          << std::endl;
            } else if (type == "reset") {
                // The backend acknowledges a new recording with reset. Keep
                // the immediate listening indicator until the first draft.
                if (isRecording_) {
                    state->showStatus("正在聆听…");
                } else {
                    state->resetSmart();
                }
            } else if (type == "error") {
                state->updatePreedit("[! " + text + "]");
            }
        });
    });

    if (backend_->connect()) {
        backend_->startReceiveLoop();
        std::cout << "[YuHuang] Backend connected, sending config..." << std::endl;
        sendConfigToBackend();
        std::cout << "[YuHuang] Ready for push-to-talk ("
                  << (triggerMode_ == PttMode::Toggle ? "toggle" : "hold")
                  << " " << triggerKeysToString() << " to speak)" << std::endl;
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

// ---- ★ 全局事件处理（addon 模式，PreInputMethod 阶段）----

namespace {
// Auto-repeat pairs share one timestamp and arrive back to back; 30 ms covers
// that with room to spare while adding no noticeable delay to a real release.
constexpr uint64_t kTriggerReleaseGraceUs = 30000;
// The two halves of a synthetic auto-repeat pair carry the same event time.
constexpr uint64_t kAutoRepeatPairSkewMs = 5;
} // namespace

void YuHuangEngine::onGlobalKey(fcitx::KeyEvent &keyEvent) {
    const fcitx::Key &key = keyEvent.key();
    bool isRelease = keyEvent.isRelease();

    // Release order changes modifiers and may turn Q into q. Track the
    // physical key, and finish as soon as any required chord key is released.
    if (triggerMode_ == PttMode::Hold && triggerHeld_) {
        const auto raw = keyEvent.rawKey();
        auto lower = [](uint32_t sym) { return sym >= 'A' && sym <= 'Z' ? sym + ('a' - 'A') : sym; };
        const bool mainKey = (heldTrigger_.code() && raw.code())
            ? heldTrigger_.code() == raw.code()
            : lower(heldTrigger_.sym()) == lower(raw.sym());
        if (isRelease && mainKey) {
            keyEvent.filterAndAccept();
            // Some clients deliver auto-repeat as release+press pairs. Acting
            // on the release at once ended the recording, and the paired
            // press started an empty one, over and over while the key stayed
            // down. Wait briefly for that press before trusting the release.
            triggerReleasePending_ = true;
            triggerReleaseTime_ = static_cast<uint64_t>(keyEvent.time());
            triggerReleaseTimer_ = instance_->eventLoop().addTimeEvent(
                CLOCK_MONOTONIC, fcitx::now(CLOCK_MONOTONIC) + kTriggerReleaseGraceUs, 1000,
                [this](fcitx::EventSourceTime *, uint64_t) {
                    if (!triggerReleasePending_) return true;
                    triggerReleasePending_ = false;
                    triggerHeld_ = false;
                    stopListening();
                    return true;
                });
            return;
        }
        if (isRelease && key.isModifier() &&
                (fcitx::Key::keySymToStates(key.sym()) & heldTrigger_.states())) {
            stopListening();
            return; // Let the application see its modifier release.
        }
        if (!isRelease && mainKey) {
            // Auto-repeat, including the press half of a release+press pair,
            // must never start a new session. Only a press stamped with (almost)
            // the release's time cancels it; a genuine release is still honoured
            // when the timer fires.
            const uint64_t t = static_cast<uint64_t>(keyEvent.time());
            const uint64_t gap = t > triggerReleaseTime_ ? t - triggerReleaseTime_ : triggerReleaseTime_ - t;
            if (triggerReleasePending_ && gap <= kAutoRepeatPairSkewMs) {
                triggerReleasePending_ = false;
            }
            keyEvent.filterAndAccept();
            return;
        }
    }

    // ★ 按键驱动的重连：定时器最多 2s 才轮一次，这里让"断连后立刻按键"
    // 也能马上恢复，不必等下一个 tick。
    tryReconnect();

    if (isTriggerKey(key)) {
        keyEvent.filterAndAccept();
        if (triggerMode_ == PttMode::Hold && !isRelease) {
            triggerHeld_ = true;
            heldTrigger_ = keyEvent.rawKey();
        }
        if (triggerMode_ == PttMode::Toggle) {
            // ★ Toggle 模式：按一下开始、再按一下结束，release 一律忽略。
            // 适用 Free3 等脉冲式蓝牙小键盘（press 后 ~62ms 伪造 release，
            // 物理上无法表达"按住"，所以松键信号无意义）。
            if (isRelease) return;
            // ★ 去抖：物理键盘按住会自动重复 press（~30ms 一次），
            // Free3 的脉冲也会紧跟前一次 press。<600ms 内的重复 press
            // 忽略——正常 toggle 停止至少要说话几百毫秒后才再按。
            uint64_t t = static_cast<uint64_t>(keyEvent.time());
            if (t > lastToggleTime_ && t - lastToggleTime_ < 600) {
                logPtt("PTT: toggle press ignored (debounce)");
                return;
            }
            lastToggleTime_ = t;
            if (isRecording_) {
                logPtt("PTT: toggle press while recording -> stop");
                stopListening();
            } else {
                startListening();
                recordingStartTime_ = t;  // ★ 打断去抖基准
            }
        } else {
            // ★ Hold 模式：按住说话、松开全文终审。组合键按住期间主键
            // 可能自动重复 press，用 isRecording_ 防重复开始。
            // 脉冲式设备（Free3）在此模式下按下即立刻松手会触发开始又
            // 立刻停止——脉冲设备请选 Toggle 模式。
            if (isRelease) {
                if (isRecording_) {
                    stopListening();
                }
            } else {
                if (isRecording_) {
                    return;  // 按住期间的自动重复 press，忽略
                }
                startListening();
                recordingStartTime_ = keyEvent.time();  // ★ 打断去抖基准
            }
        }
        return;
    }

    // 非 PTT 按键 + 录音中 + 按下 → 打断：暂扣按键 + 润色剩余收尾
    if (isRecording_ && !isRelease) {
        // ★ 去抖：PTT 按下后 200ms 内的按键多为 Pause 伴随噪声（Pause/Break
        // 物理键按下时会伴随产生假的 Shift/Control/Meta 修饰键事件），忽略。
        // 正常说话至少几百毫秒，这期间用户不可能已经完成说话又要打字。
        uint64_t t = static_cast<uint64_t>(keyEvent.time());
        if (t > recordingStartTime_ && t - recordingStartTime_ < 200) {
            logPtt(std::string("PTT: ignore key in debounce window ")
                   + key.toString());
            return;
        }
        if (key.isModifier()) {
            // ★ 纯修饰键分两小类（去抖窗口外的）：
            // 1) 属于触发组合键修饰集（如 TriggerKey=Ctrl+Alt+Shift+Y 时按下
            //    Ctrl/Alt/Shift）：大概率是用户正在按组合键去停止 PTT（toggle）
            //    或开始 PTT（hold）→ 忽略，不打断，等后面的主键匹配。
            // 2) 集合之外的修饰键（如 bare Super/Ctrl，当触发键是 Pause 时）
            //    → 用户有意按键，照常打断。
            auto modStates = key.states()
                             | fcitx::Key::keySymToStates(key.sym());
            if (!(modStates & ~triggerModifierUnion())) {
                logPtt(std::string("PTT: ignore modifier (trigger combo component) ")
                       + key.toString());
                return;
            }
            // 集合外修饰键：落入下面的打断流程
        }
        keyEvent.filterAndAccept();  // 暂扣，拼音收不到
        pendingKey_ = key;
        pendingKeyRelease_ = false;
        pendingKeyTime_ = keyEvent.time();
        hasPendingKey_ = true;
        logPtt(std::string("PTT: interrupted by key ") + key.toString() +
               " -> hold key, finalize remaining");
        interruptListening();
        return;
    }
    // 其余情况（非录音 / 纯 release）→ 放行给拼音，不处理
}

void YuHuangEngine::onFocusOut(fcitx::InputContextEvent &event) {
    // Fcitx has many input contexts; another window losing focus is unrelated.
    if (event.inputContext() != recordingIc_.get()) return;
    triggerHeld_ = false;
    triggerReleasePending_ = false;
    if (isRecording_) {
        logPtt("PTT: focus lost while recording -> interrupt");
        interruptListening();  // 焦点打断：无暂扣按键，只润色剩余收尾
    } else if (isFinalizing_) {
        discardPendingResult_ = true;
        if (auto *state = currentState()) state->resetSmart();
        logPtt("PTT: focus lost during final recognition -> discard pending output");
    }
}

// ---- ★ PTT 生命周期 ----

void YuHuangEngine::startListening() {
    if (isFinalizing_) {
        logPtt("PTT: waiting for the previous final result; release and press again");
        return;
    }
    auto *ic = instance_->mostRecentInputContext();
    if (!ic) {
        logPtt("PTT: no focused input context, ignore trigger");
        return;
    }
    // ★ 钉住本次录音的目标 IC：之后后端的所有 preedit/commit/replace
    // 都打到这个窗口，即使录音中用户用鼠标把焦点点走
    recordingIc_ = ic->watch();
    discardPendingResult_ = false;
    auto *state = ic->propertyFor(&factory_);
    state->resetSmart();
    if (!tryReconnect()) {
        state->showStatus("语音服务未连接，请稍后重试");
        return;
    }
    isRecording_ = true;
    // Paint in the key handler, before any microphone routing or ASR work.
    state->showStatus("正在聆听…");
    logPtt(std::string("PTT: trigger pressed -> start listening on ")
           + ic->program());
    if (backend_ && backend_->isConnected()) {
        backend_->sendCommand("{\"type\":\"start_listening\"}");
    }
    // ★ 组合键模式不启动物理看门狗：XQueryKeymap 对组合键的单个键位
    // （主键 Y）查询不可靠，且 PTT 是按住说话，release 事件直接可信。
    // 若实测 release 丢失（录音卡住），再考虑恢复看门狗。
}

void YuHuangEngine::stopListening() {
    // PTT 松开：全文终审（删除重推改开头错字，光标还在语音末尾）
    if (!isRecording_) return;
    isRecording_ = false;
    watchdogMisses_ = 0;
    x11WatchKeyEnd();  // 退订 raw 事件，防止在连接上无限堆积
    logPtt("PTT: stop listening (release)");
    if (backend_ && backend_->isConnected()) {
        isFinalizing_ = true;
        if (auto *state = currentState(); state && state->pendingText().empty()) {
            state->showStatus("正在识别…");
        }
        backend_->sendCommand("{\"type\":\"stop_listening\"}");
    }
}

void YuHuangEngine::interruptListening() {
    // 打断：只润色剩余收尾，不删除重推（光标即将移走，会误删用户输入）
    if (!isRecording_) return;
    isRecording_ = false;
    watchdogMisses_ = 0;
    x11WatchKeyEnd();
    logPtt("PTT: interrupt -> finalize remaining");
    if (backend_ && backend_->isConnected()) {
        isFinalizing_ = true;
        backend_->sendCommand("{\"type\":\"interrupt\"}");
    }
    // isRecording_=false 后，该键的 release 及后续按键穿透给拼音；
    // 暂扣的 press 等后端 interrupt_done 后由 releasePendingKey 放行
}

void YuHuangEngine::releasePendingKey() {
    if (!hasPendingKey_) return;
    hasPendingKey_ = false;
    // ★ 防护：打断收尾期间用户若又重新按下 PTT（isRecording_=true），
    // 旧打断按键已过时，丢弃不重注入——避免重注入的按键再次触发打断
    if (isRecording_) {
        logPtt("PTT: discard held key (new recording already started)");
        return;
    }
    auto *ic = recordingIc_.get();
    if (!ic) {
        ic = instance_->mostRecentInputContext();
    }
    if (!ic) return;
    // ★ postEvent 重新注入暂扣的按键 press，交回拼音处理。
    // 重注入会再过 PreInputMethod，但 isRecording_=false 不会被二次拦截，
    // 按键顺利到达 InputMethod 阶段的拼音。
    fcitx::KeyEvent keyEvent(ic, pendingKey_, pendingKeyRelease_, pendingKeyTime_);
    instance_->postEvent(keyEvent);
    logPtt(std::string("PTT: released held key ") + pendingKey_.toString());
}

void YuHuangEngine::startPttWatchdog() {
    // X11 不可用（Wayland 会话/无 DISPLAY）时不启用，保留另外两层防御
    // 看门狗一次只能盯一个 sym：用列表里第一个。多键时其余键少了这层
    // X11 兜底，另外两层防御（release 事件、rescue-press）仍然有效。
    if (triggerKeys_.empty() || x11WatchKeyBegin(triggerKeys_.front().sym()) < 0) {
        logPtt("PTT watchdog unavailable (no X11), "
               "rescue-press is the only defense");
        return;
    }

    watchdogMisses_ = 0;
    pttWatchdog_.reset();  // 销毁旧事件源（此时不在其回调内，安全）
    pttWatchdog_ = instance_->eventLoop().addTimeEvent(
        CLOCK_MONOTONIC, fcitx::now(CLOCK_MONOTONIC) + 200000, 10000,
        [this](fcitx::EventSourceTime *source, uint64_t) {
            if (!isRecording_) {
                return true;  // 已正常停止：不续期，自然停摆
            }
            int down = x11WatchKeyPoll();
            if (down == 0) {
                // 连续 2 次（~400ms）未按下才判定丢松键，防瞬时竞态
                if (++watchdogMisses_ >= 2) {
                    logPtt("PTT watchdog: physical key released "
                           "but no release event received "
                           "(grabbed by compositor?) -> force stop");
                    stopListening();
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

// ---- Current focused state ----
YuHuangState *YuHuangEngine::currentState() {
    // ★ 优先使用 startListening 钉住的 IC，不跟随当前焦点——否则录音中
    // 用户用鼠标点了别的窗口，后端收尾的 commit/replace 会落到新窗口。
    // 原窗口销毁后丢弃结果，不能把延迟返回的文字写入另一个窗口。
    auto *ic = recordingIc_.get();
    if (!ic) return nullptr;
    return ic->propertyFor(&factory_);
}

// ---- 自绘悬浮窗 ----
PanelWindow *YuHuangEngine::panel() {
#ifdef YUHUANG_HAVE_PANEL
    if (!panelTried_) {
        panelTried_ = true;   // 只试一次，连不上 X 就永远走候选栏回退

        // 自绘窗把自己摆在应用光标矩形的根窗口坐标上，这在 Wayland 会话里
        // 没有意义：Wayland 原生客户端的光标位置通过 text-input 上报，是
        // surface 局部坐标，映射不到 X 根窗口。XWayland 却总能开出 display，
        // 所以"能连上 X"不等于"处在 X11 会话"——照着连接是否成功来判断，
        // 草稿窗就会飘到某块屏幕的角落。改按会话类型判断，把 Wayland 交回
        // fcitx5 候选栏，由 fcitx5 通过输入法协议正确定位。
        if (const char *wl = getenv("WAYLAND_DISPLAY"); wl && *wl) {
            logPtt("panel: Wayland session, using the candidate bar "
                   "(the self-drawn panel can only position itself on X11)");
            return nullptr;
        }

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
