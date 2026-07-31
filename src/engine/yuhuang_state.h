#ifndef YUHUANG_STATE_H
#define YUHUANG_STATE_H

#include "yuhuang_engine.h"
#include "yuhuang_panel.h"
#include <fcitx/inputpanel.h>
#include <fcitx/text.h>
#include <fcitx-utils/color.h>

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
    ic_->inputPanel().reset();
    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();

    ic_->commitString(text);
}

inline void YuHuangState::reset() {
    if (!ic_) return;
    pendingText_.clear();
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
    // ★ 应用内嵌 preedit 一律留空：各应用对格式的支持参差不齐，VSCode
    // 只画下划线、WezTerm 压根不画，统一交给 fcitx 面板保证四处一致
    inputPanel.setClientPreedit(fcitx::Text());
    // ★ 面板的 preedit 那一栏被写死了单行（无法折行），所以改走候选栏
    inputPanel.setPreedit(fcitx::Text());
    inputPanel.setCandidateList(
        std::make_unique<PanelTextList>(std::move(block)));

    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();
}

} // namespace yuhuang

#endif // YUHUANG_STATE_H
