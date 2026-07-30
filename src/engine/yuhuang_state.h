#ifndef YUHUANG_STATE_H
#define YUHUANG_STATE_H

#include "yuhuang_engine.h"
#include <fcitx/inputpanel.h>
#include <fcitx/text.h>
#include <fcitx-utils/color.h>

namespace yuhuang {

inline YuHuangState::YuHuangState(YuHuangEngine *engine,
                                   fcitx::InputContext *ic)
    : engine_(engine), ic_(ic) {}

inline YuHuangState::~YuHuangState() {}

inline void YuHuangState::updatePreedit(const std::string &text) {
    if (!ic_) return;

    auto &inputPanel = ic_->inputPanel();

    if (text.empty()) {
        inputPanel.setClientPreedit(fcitx::Text());
        inputPanel.setPreedit(fcitx::Text());
        ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
        ic_->updatePreedit();
        return;
    }

    fcitx::Text preedit(text);
    // ★ 光标定位到最新输入的位置（文本末尾）
    preedit.setCursor(static_cast<int>(text.size()));

    // ★ 应用内嵌 preedit（光标处直接显示，类似手机输入法）
    inputPanel.setClientPreedit(preedit);
    // ★ 清空 panel preedit（不需要候选框弹窗）
    inputPanel.setPreedit(fcitx::Text());

    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();
}

inline void YuHuangState::commitText(const std::string &text) {
    if (!ic_ || text.empty()) return;

    auto &inputPanel = ic_->inputPanel();

    // ★ 先清空 client preedit，防止残留文本被自动 flush 上屏
    inputPanel.setClientPreedit(fcitx::Text());
    inputPanel.setPreedit(fcitx::Text());
    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();

    ic_->commitString(text);
}

inline void YuHuangState::reset() {
    if (!ic_) return;
    auto &inputPanel = ic_->inputPanel();
    inputPanel.setClientPreedit(fcitx::Text());
    inputPanel.setPreedit(fcitx::Text());
    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();
}

// ---- 分段预编辑（应用内嵌 + 格式标记 + 光标跟踪）----

inline void YuHuangState::updatePreedit(const std::vector<TextSegment> &segments) {
    if (!ic_) return;

    if (segments.empty()) {
        reset();
        return;
    }

    auto &inputPanel = ic_->inputPanel();

    // ★ 构建带格式标记的 preedit（下划线=红区，加粗=黄区，高亮=绿区）
    // gedit 等应用即使不支持颜色显示，也能渲染下划线和加粗
    fcitx::Text preedit;
    int cursorPos = 0;
    for (const auto &seg : segments) {
        fcitx::TextFormatFlags flags = fcitx::TextFormatFlag::NoFlag;
        if (seg.style == "red") {
            flags |= fcitx::TextFormatFlag::Underline;
        } else if (seg.style == "yellow") {
            flags |= fcitx::TextFormatFlag::Bold;
        } else if (seg.style == "green") {
            flags |= fcitx::TextFormatFlag::HighLight;
        }
        preedit.append(seg.text, flags);
        cursorPos += static_cast<int>(seg.text.size());
    }

    // ★ 光标定位到最新输入的位置（preedit 末尾）
    // 这样用户每次说话时，光标始终在最新字符后面，而非停留在 commit 位置
    preedit.setCursor(cursorPos);

    // ★ 应用内嵌 preedit（光标处直接显示，类似手机输入法）
    inputPanel.setClientPreedit(preedit);
    // ★ 清空 panel preedit（不需要候选框弹窗）
    inputPanel.setPreedit(fcitx::Text());

    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();
}

} // namespace yuhuang

#endif // YUHUANG_STATE_H
