#ifndef AOIDE_ENGINE_H
#define AOIDE_ENGINE_H

#include <fcitx/addoninstance.h>
#include <fcitx/addonfactory.h>
#include <fcitx/addonmanager.h>
#include <fcitx/inputcontextproperty.h>
#include <fcitx/inputcontext.h>
#include <fcitx/instance.h>
#include <fcitx/event.h>
#include <fcitx-config/configuration.h>
#include <fcitx-config/option.h>
#include <fcitx-config/enum.h>
#include <fcitx-config/iniparser.h>
#include <string>
#include <vector>
#include <memory>
#include <thread>
#include <mutex>
#include <atomic>
#include <functional>
#include <fcitx-utils/eventdispatcher.h>
#include <fcitx-utils/event.h>

namespace aoide {

// ===== 按键符号常量 =====
namespace vk {
    constexpr uint32_t Escape = 0xFF1B;
    constexpr uint32_t Return = 0xFF0D;
    constexpr uint32_t F5     = 0xFFC5;
    constexpr uint32_t F6     = 0xFFC6;
}

// ===== PTT 触发模式 =====
// Hold   = 按住说话、松手上屏（普通键盘按住式触发）
// Toggle = 按一下开始、再按一下结束（Free3 等脉冲式蓝牙小键盘：
//          按下即发 press+release 脉冲，物理上无法表达"按住"）
FCITX_CONFIG_ENUM(PttMode, Hold, Toggle);
FCITX_CONFIG_ENUM(CloudProvider, OpenAI, OpenAIRealtime, ElevenLabs, ElevenLabsRealtime);
FCITX_CONFIG_ENUM(CloudDraft, Local, Cloud);

// ===== 配置类 (fcitx5-configtool GUI 可编辑) =====
// 注意: double/float 非 fcitx5 原生支持, 时间值用 int (毫秒) 存储
FCITX_CONFIGURATION(AoideConfig,

    // ---- 触发键 (PTT) ----
    // 可以列多个键，任意一个都能触发。修饰键组合在 fcitx5 里是有顺序的
    // ——先按 Ctrl 再按 Alt 是 Control+Alt_L，反过来是 Alt+Control_L，
    // 是两个不同的 Key——所以"不管先按哪个都行"必须靠列表表达。
    fcitx::KeyListOption triggerKey{
        this, "TriggerKey", "Trigger key",
        fcitx::KeyList{fcitx::Key("Control+Alt+Y")},
        fcitx::KeyListConstrain(
            fcitx::KeyConstrainFlags{}
            | fcitx::KeyConstrainFlag::AllowModifierOnly
            | fcitx::KeyConstrainFlag::AllowModifierLess)
    };

    // ---- 触发模式 (PTT) ----
    fcitx::Option<PttMode> triggerMode{
        this, "TriggerMode", "Trigger mode", PttMode::Hold
    };

    fcitx::Option<bool> checkConflicts{
        this, "CheckConflicts",
        "Warn if trigger key conflicts with system shortcuts", true
    };

    // ---- 后端连接 ----
    fcitx::Option<std::string> backendSocket{
        this, "BackendSocket",
        "Unix Domain Socket path for backend service",
        "auto"
    };

    // ---- 音频设备 ----
    fcitx::Option<std::string> audioDevice{
        this, "AudioDevice",
        "Microphone device name (leave empty for default, run 'aoide-ctl mic' to list)",
        ""
    };

    // ---- 语音参数 (毫秒存储, 使用时转换) ----
    fcitx::Option<int, fcitx::IntConstrain> vadSilenceTimeoutMs{
        this, "VADSilenceTimeout",
        "Silence timeout in milliseconds before segment finalization",
        800, fcitx::IntConstrain(100, 5000)
    };

    fcitx::Option<int, fcitx::IntConstrain> asrIntermediateInterval{
        this, "ASRIntermediateInterval",
        "Intermediate ASR result refresh interval (ms)",
        300, fcitx::IntConstrain(50, 2000)
    };

    // ---- 悬浮面板显示 ----
    // 三区文本统一由 fcitx 面板渲染，折行由引擎自己算（面板不会自动折行）
    fcitx::Option<int, fcitx::IntConstrain> panelLineWidth{
        this, "PanelLineWidth",
        "Floating panel line width in display columns (a CJK char takes 2)",
        48, fcitx::IntConstrain(10, 200)
    };

    fcitx::Option<int, fcitx::IntConstrain> panelMaxLines{
        this, "PanelMaxLines",
        "Max preview lines (beginning and end retained; middle folded)",
        10, fcitx::IntConstrain(4, 30)
    };

    fcitx::Option<int, fcitx::IntConstrain> panelFontSize{
        this, "PanelFontSize",
        "Floating panel font size in points (self-drawn panel only)",
        14, fcitx::IntConstrain(8, 40)
    };

    // ---- LLM 优化 ----
    fcitx::Option<bool> llmEnabled{
        this, "LLMEnabled",
        "Enable LLM optimization (requires LLM backend)", false
    };

    fcitx::Option<std::string> llmBaseUrl{
        this, "LLMBaseUrl",
        "LLM API base URL (OpenAI-compatible)",
        "http://localhost:8000/v1"
    };

    fcitx::Option<std::string> llmApiKey{
        this, "LLMApiKey", "Legacy LLM key reference (store new keys in Aoide Settings)", ""
    };

    fcitx::Option<std::string> llmModel{
        this, "LLMModel", "LLM model name", "qwen2.5-7b-instruct"
    };

    fcitx::Option<int, fcitx::IntConstrain> llmOptimizeDelayMs{
        this, "LLMOptimizeDelay",
        "Delay before LLM optimization (milliseconds)",
        500, fcitx::IntConstrain(0, 5000)
    };

    fcitx::Option<int, fcitx::IntConstrain> llmAutoCommitDelayMs{
        this, "LLMAutoCommitDelay",
        "Delay before auto-commit after LLM (milliseconds)",
        200, fcitx::IntConstrain(0, 3000)
    };

    fcitx::Option<int, fcitx::IntConstrain> llmTemperature{
        this, "LLMTemperature",
        "LLM temperature (0-100, e.g. 30 = 0.30)",
        30, fcitx::IntConstrain(0, 100)
    };

    fcitx::Option<int, fcitx::IntConstrain> llmMaxTokens{
        this, "LLMMaxTokens", "LLM max output tokens", 2000,
        fcitx::IntConstrain(64, 8192)
    };

    // Cloud recognition is normally configured in config.yaml. Keep that file
    // authoritative until the user explicitly opts into managing it here.
    fcitx::Option<bool> cloudASROverride{
        this, "CloudASROverride", "Use cloud settings below (off: backend config.yaml)", false
    };
    fcitx::Option<bool> cloudASREnabled{
        this, "CloudASREnabled", "Enable cloud recognition", false
    };
    fcitx::Option<CloudProvider> cloudASRProvider{
        this, "CloudASRProvider", "Final transcript provider", CloudProvider::OpenAI
    };
    fcitx::Option<CloudDraft> cloudASRDraft{
        this, "CloudASRDraft", "Live draft source", CloudDraft::Local
    };
    fcitx::Option<std::string> cloudASRApiKey{
        this, "CloudASRApiKey", "Legacy cloud key reference (store new keys in Aoide Settings)", ""
    };
    fcitx::Option<std::string> cloudASRModel{
        this, "CloudASRModel", "Batch transcription model (empty keeps YAML value)", ""
    };
    fcitx::Option<std::string> cloudASRRealtimeModel{
        this, "CloudASRRealtimeModel", "Realtime model (empty keeps YAML value)", ""
    };
)

// ===== Unix Socket 客户端 (与后端通信) =====
class BackendClient {
public:
    explicit BackendClient(const std::string &socketPath);
    ~BackendClient();

    bool connect();
    void disconnect();
    bool isConnected() const;

    bool sendAudio(const std::vector<int16_t> &pcmData);
    bool sendCommand(const std::string &command);

    using ResultCallback = std::function<void(const std::string &type,
                                              const std::string &text,
                                              const std::string &raw_msg)>;
    void setResultCallback(ResultCallback callback);

    void startReceiveLoop();
    void stopReceiveLoop();

    int fd() const { return fd_; }

private:
    std::string socketPath_;
    int fd_ = -1;
    std::atomic<bool> connected_{false};
    std::atomic<bool> receiving_{false};
    std::thread receiveThread_;
    std::mutex mutex_;
    ResultCallback callback_;

    void receiveLoop();
    bool sendRaw(const uint8_t *data, size_t len);
};

// ===== 候选框分段（三色渲染）=====
struct TextSegment {
    std::string text;
    std::string style;  // "green" | "yellow" | "red" | "gray"
};

class PanelWindow;

// ===== 每个 InputContext 的状态 =====
class AoideState : public fcitx::InputContextProperty {
public:
    AoideState(class AoideEngine *engine, fcitx::InputContext *ic);
    ~AoideState();

    void updatePreedit(const std::string &text);
    void updatePreedit(const std::vector<TextSegment> &segments);
    void showStatus(const std::string &text);
    void commitText(const std::string &text);
    void reset();

    // ★ 三级通道路上屏（按应用能力区分真上屏 / 假上屏到候选区）
    bool usePreeditChannel() const;        // Preedit=1：假上屏通道
    bool supportFormattedPreedit() const;  // FormattedPreedit=1：preedit 可带格式
    void commitSmart(const std::string &text);    // commit 分通道
    void replaceSmart(int delChars, const std::string &text,
                      const std::string &fallback);  // replace 分通道
    void commitAfterFocusLoss(const std::string &text, const std::string &fallback);
    void resetSmart();                     // reset + 清空假上屏

    // ★ 打断收尾提交：假上屏通道把 fakeCommitted + 剩余拼接真上屏；
    //   真上屏通道直接上屏剩余。不做删除重推（光标即将移走）。
    void interruptCommit(const std::string &text);

    fcitx::InputContext *inputContext() const { return ic_; }

    // 面板里正在显示的草稿全文。三区文本现在画在 fcitx 面板上，应用内嵌
    // preedit 一律留空，Enter 键要上屏的内容只能从这里取。
    const std::string &pendingText() const { return pendingText_; }
    void relocatePreview();

private:
    void fakeCommit(const std::string &text);  // 假上屏累积到候选区
    void updateFakePreedit();                   // 刷新假上屏 preedit 显示
    void clearFakePreedit();                    // 清空假上屏

    AoideEngine *engine_;
    fcitx::InputContext *ic_;
    std::string pendingText_;
    bool previewVisible_ = false;
    std::string fakeCommitted_;  // ★ 假上屏累积文本（Preedit 通道）
};

// ===== 语音输入主类（纯 addon 全局监听，不再继承 InputMethodEngine）=====
// 拼音等其他输入法始终激活，本 addon 只在 PreInputMethod 阶段监听 PTT 专用
// 键与打断信号，语音上屏到当前焦点应用的光标处，实现语音拼音共存。
class AoideEngine : public fcitx::AddonInstance {
public:
    // 相对 StandardPath 的 PkgConfig 根，即 ~/.config/fcitx5/conf/aoide.conf
    static constexpr char kConfigPath[] = "conf/aoide.conf";

    explicit AoideEngine(fcitx::Instance *instance);
    ~AoideEngine();

    // 配置 (GUI 集成) — AddonInstance 虚函数
    const fcitx::Configuration *getConfig() const override {
        return &config_;
    }
    void setConfig(const fcitx::RawConfig &rawConfig) override {
        config_.load(rawConfig, true);
        fcitx::safeSaveAsIni(config_, kConfigPath);
        applyConfig();
    }
    // 从磁盘读回配置。基类的实现是空的，不覆盖它的话构造函数里那次
    // reloadConfig() 什么也不做，addon 永远跑在编译期默认值上——
    // fcitx5-configtool 里存的触发键、麦克风、LLM 设置全都读不回来。
    void reloadConfig() override {
        fcitx::readAsIni(config_, kConfigPath);
        applyConfig();
    }

    auto factory() const { return &factory_; }
    auto instance() const { return instance_; }

    // 便捷配置访问
    std::string backendSocket() const;
    double vadSilenceTimeout() const {
        return config_.vadSilenceTimeoutMs.value() / 1000.0;
    }
    int asrIntermediateInterval() const {
        return config_.asrIntermediateInterval.value();
    }
    int panelLineWidth() const { return config_.panelLineWidth.value(); }
    int panelMaxLines() const { return config_.panelMaxLines.value(); }
    int panelFontSize() const { return config_.panelFontSize.value(); }

    // 自绘悬浮窗：懒创建，首次调用时连 X 并注册事件监听。
    PanelWindow *panel();
    PanelWindow *panelIfCreated();

    BackendClient &backend() { return *backend_; }
    AoideState *currentState();

    // PTT 状态
    bool isListening() const { return isRecording_; }

private:
    void applyConfig();
    void checkSystemConflict(const fcitx::Key &key);
    // 触发键里出现过的全部修饰键，取并集。录音中判断"这个修饰键是不是
    // 触发组合的一部分"时用它，否则列表里第二个键的修饰键会被当成打断。
    fcitx::KeyStates triggerModifierUnion() const;
    std::string triggerKeysToString() const;
    bool isTriggerKey(const fcitx::Key &k) const;
    void sendConfigToBackend();

    // ★ 全局事件处理（addon 模式，PreInputMethod 阶段）
    void onGlobalKey(fcitx::KeyEvent &key);
    void onFocusOut(fcitx::InputContextEvent &event);
    void onCursorRectChanged(fcitx::InputContextEvent &event);

    // ★ PTT 生命周期
    void startListening();        // PTT 按下：开始录音
    void stopListening();         // PTT 松开：全文终审（删除重推）
    void interruptListening();    // 打断：暂扣按键 + 润色剩余收尾
    void releasePendingKey();     // interrupt_done 后放行暂扣按键

    // ★ PTT 停止统一入口 + 物理键盘看门狗
    // 背景：GNOME 合成器键盘 grab 会吞掉松键事件（实录 5.5 分钟卡麦），
    // 引擎侧永远等不到 release，须主动向 X server 轮询物理键位状态。
    void startPttWatchdog();
    void stopPttWatchdog();

    fcitx::Instance *instance_;
    fcitx::FactoryFor<AoideState> factory_;
    AoideConfig config_;

    // PTT 触发键与录音状态
    fcitx::KeyList triggerKeys_;
    PttMode triggerMode_ = PttMode::Hold;
    bool isRecording_ = false;
    bool isFinalizing_ = false;
    bool focusMovedDuringRecording_ = false;
    bool triggerHeld_ = false;
    fcitx::Key heldTrigger_;
    // X11-style key auto-repeat arrives as a release immediately followed by a
    // press with the same timestamp. A trigger release is only acted on once
    // this short timer expires without such a press.
    std::unique_ptr<fcitx::EventSourceTime> triggerReleaseTimer_;
    bool triggerReleasePending_ = false;
    uint64_t triggerReleaseTime_ = 0;
    uint64_t recordingStartTime_ = 0;  // ★ PTT 按下的事件时间（打断去抖基准，同 keyEvent.time() 的 int 语义，用无符号避免回绕）
    uint64_t lastToggleTime_ = 0;  // ★ Toggle 模式去抖：上次 toggle 动作时间（防键盘自动重复 / Free3 脉冲连发误触发）

    // ★ 本次录音钉住的输入上下文（startListening 时捕获）。
    // 后续所有上屏/预编辑都打到这个 IC，而不是跟随当前焦点——否则录音中
    // 用鼠标点了别的窗口，后端收尾的 commit/replace 会落到新窗口里。
    // IC 销毁（窗口关闭）时引用自动失效，回退到 mostRecentInputContext。
    fcitx::TrackableObjectReference<fcitx::InputContext> recordingIc_;

    // ★ 打断暂扣的按键（等后端 interrupt_done 后放行给拼音）
    fcitx::Key pendingKey_;
    bool pendingKeyRelease_ = false;
    int pendingKeyTime_ = 0;
    bool hasPendingKey_ = false;

    // ★ 全局事件监听句柄（PreInputMethod 阶段）
    std::unique_ptr<fcitx::HandlerTableEntry<fcitx::EventHandler>> keyWatcher_;
    std::unique_ptr<fcitx::HandlerTableEntry<fcitx::EventHandler>> focusWatcher_;
    std::unique_ptr<fcitx::HandlerTableEntry<fcitx::EventHandler>> cursorWatcher_;

    // ★ 看门狗：200ms 轮询物理键位，连续 2 次未按下判定丢松键
    std::unique_ptr<fcitx::EventSourceTime> pttWatchdog_;
    // 断连后的后台重连。只靠按键驱动重连的话，后端重启（升级、换模型、
    // 崩溃自愈）之后第一次按触发键会被重连本身吃掉，不会开始录音。
    std::unique_ptr<fcitx::EventSourceTime> reconnectTimer_;
    bool tryReconnect();
    int watchdogMisses_ = 0;

    // 后端客户端
    std::unique_ptr<BackendClient> backend_;

#ifdef AOIDE_HAVE_PANEL
    // 自绘悬浮窗（panelIO_ 声明在后，析构时先摘事件源再关窗口）
    std::unique_ptr<PanelWindow> panelWindow_;
    std::unique_ptr<fcitx::EventSourceIO> panelIO_;
    bool panelTried_ = false;
#endif

    // 跨线程事件调度 (receiveLoop → fcitx5 主线程)
    fcitx::EventDispatcher eventDispatcher_;
};

// ===== Addon 工厂 =====
class AoideEngineFactory : public fcitx::AddonFactory {
    fcitx::AddonInstance *create(fcitx::AddonManager *manager) override {
        return new AoideEngine(manager->instance());
    }
};

} // namespace aoide

#endif // AOIDE_ENGINE_H
