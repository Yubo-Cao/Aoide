#ifndef YUHUANG_STATE_H
#define YUHUANG_STATE_H

#include "yuhuang_engine.h"
#include "yuhuang_panel.h"
#ifdef YUHUANG_HAVE_PANEL
#include "yuhuang_window.h"
#endif
#include <fcitx/inputcontext.h>
#include <fcitx/inputpanel.h>
#include <fcitx/text.h>
#include <fcitx-utils/color.h>
#include <fcitx-utils/capabilityflags.h>

namespace yuhuang {

inline YuHuangState::YuHuangState(YuHuangEngine *engine,
                                   fcitx::InputContext *ic)
    : engine_(engine), ic_(ic) {}

inline YuHuangState::~YuHuangState() {}

inline void YuHuangState::updatePreedit(const std::string &text) {
    if (text.empty()) {
        reset();
        return;
    }
    // 单色文本（如 [Listening...] 提示）当作一个无样式分段走同一条路
    updatePreedit(std::vector<TextSegment>{{text, ""}});
}

inline void YuHuangState::commitText(const std::string &text) {
    if (!ic_ || text.empty()) return;

    // ★ 先清空面板和 client preedit，防止残留文本被自动 flush 上屏
    pendingText_.clear();
#ifdef YUHUANG_HAVE_PANEL
    if (auto *p = engine_->panelIfCreated()) p->hide();
#endif
    ic_->inputPanel().reset();
    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();

    ic_->commitString(text);
}

inline void YuHuangState::reset() {
    if (!ic_) return;
    pendingText_.clear();
#ifdef YUHUANG_HAVE_PANEL
    if (auto *p = engine_->panelIfCreated()) p->hide();
#endif
    // preedit、aux、候选列表一并清掉，面板随之消失
    ic_->inputPanel().reset();
    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();
}

// ---- 分段草稿（fcitx 悬浮面板 + 格式标记 + 自动折行）----

inline void YuHuangState::updatePreedit(const std::vector<TextSegment> &segments) {
    if (!ic_) return;

    if (segments.empty()) {
        reset();
        return;
    }

    pendingText_.clear();
    for (const auto &seg : segments) pendingText_ += seg.text;

    // ★ 三区文本拼成一块带格式的多行文本（下划线=红区，加粗=黄区，
    // 高亮=绿区），折行位置自己算，面板只负责画
    fcitx::Text block = buildWrappedText(segments, engine_->panelLineWidth(),
                                         engine_->panelMaxLines());

    auto &inputPanel = ic_->inputPanel();
    // ★ client preedit 分通道：
    //   假上屏通道(Preedit=1)：显示已假上屏的绿区内容（下划线标记本次输入）
    //   真上屏通道：留空（各应用格式支持不一，统一交给悬浮面板）
    if (usePreeditChannel()) {
        fcitx::Text t;
        if (!fakeCommitted_.empty()) {
            auto flags = supportFormattedPreedit()
                ? fcitx::TextFormatFlag::Underline
                : fcitx::TextFormatFlag::NoFlag;
            t.append(fakeCommitted_, flags);
            t.setCursor(static_cast<int>(fakeCommitted_.size()));
        }
        inputPanel.setClientPreedit(t);
    } else {
        inputPanel.setClientPreedit(fcitx::Text());
    }
    inputPanel.setPreedit(fcitx::Text());

#ifdef YUHUANG_HAVE_PANEL
    if (auto *panel = engine_->panel()) {
        // ★ 自绘悬浮窗：三区淡染底色自己画，定位用应用上报的光标矩形
        // （与 classicui 同源），候选栏完全不用
        const auto &r = ic_->cursorRect();
        panel->show(block, r.left(), r.top(), r.height(),
                    engine_->panelFontSize());
        inputPanel.setCandidateList(nullptr);
    } else
#endif
    {
        // 回退：classicui 候选栏渲染（下划线=红区，加粗=黄区，高亮=绿区）
        inputPanel.setCandidateList(
            std::make_unique<PanelTextList>(std::move(block)));
    }

    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();
}

// ---- 三级通道路上屏（按应用能力区分真上屏 / 假上屏到候选区）----

inline bool YuHuangState::usePreeditChannel() const {
    // 应用支持候选区（client preedit）→ 绿区"提交"放假上屏，松开一次性真 commit
    return ic_ && ic_->capabilityFlags().test(fcitx::CapabilityFlag::Preedit);
}

inline bool YuHuangState::supportFormattedPreedit() const {
    // 应用支持格式化候选区（gedit）→ 下划线标记；VSCode 等靠默认渲染
    return ic_ && ic_->capabilityFlags().test(fcitx::CapabilityFlag::FormattedPreedit);
}

inline void YuHuangState::fakeCommit(const std::string &text) {
    if (!ic_ || text.empty()) return;
    fakeCommitted_ += text;
    updateFakePreedit();
}

inline void YuHuangState::updateFakePreedit() {
    if (!ic_) return;
    fcitx::Text t;
    if (!fakeCommitted_.empty()) {
        auto flags = supportFormattedPreedit()
            ? fcitx::TextFormatFlag::Underline
            : fcitx::TextFormatFlag::NoFlag;
        t.append(fakeCommitted_, flags);
        // ★ 光标定位到假上屏文本末尾（最新输入处）
        t.setCursor(static_cast<int>(fakeCommitted_.size()));
    }
    ic_->inputPanel().setClientPreedit(t);
    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();
}

inline void YuHuangState::clearFakePreedit() {
    fakeCommitted_.clear();
    if (ic_) {
        ic_->inputPanel().setClientPreedit(fcitx::Text());
        ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
        ic_->updatePreedit();
    }
}

inline void YuHuangState::commitSmart(const std::string &text) {
    if (usePreeditChannel()) {
        fakeCommit(text);   // 假上屏：累积到应用候选区，不真 commit
    } else {
        commitText(text);   // 真上屏（WezTerm 等不支持候选区）
    }
}

inline void YuHuangState::replaceSmart(int delChars, const std::string &text,
                                        const std::string &fallback) {
    if (usePreeditChannel()) {
        // 假上屏通道：之前未真上屏，直接 commit 全文（text 含已假上屏+剩余）。
        // 先清假上屏 preedit 防内容被确认后又插入新文本导致重复。
        clearFakePreedit();
        if (!text.empty()) commitText(text);
        return;
    }
    // 真上屏通道：有改进且应用支持删除 → 删除重推；否则只上屏剩余
    bool canDelete = ic_ && ic_->capabilityFlags().test(
        fcitx::CapabilityFlag::SurroundingText);
    if (canDelete && delChars > 0 && !text.empty()) {
        ic_->deleteSurroundingText(-delChars, delChars);
        commitText(text);
    } else if (!fallback.empty()) {
        commitText(fallback);
    }
}

inline void YuHuangState::resetSmart() {
    clearFakePreedit();
    reset();
}

// ★ 打断收尾提交（不删除重推，光标即将移走）
inline void YuHuangState::interruptCommit(const std::string &text) {
    if (usePreeditChannel()) {
        // 假上屏通道：把已假上屏的 fakeCommitted_ + 剩余拼接，一次性真上屏。
        // 之前假上屏的内容在应用候选区（preedit），commit 前必须先清掉防重复。
        std::string full = fakeCommitted_ + text;
        clearFakePreedit();
        if (!full.empty()) commitText(full);
    } else {
        // 真上屏通道：已上屏的保持原样，直接上屏剩余
        if (!text.empty()) commitText(text);
    }
}

} // namespace yuhuang

#endif // YUHUANG_STATE_H
