#ifndef AOIDE_PANEL_H
#define AOIDE_PANEL_H

#include "aoide_engine.h"
#include <fcitx/candidatelist.h>
#include <fcitx/text.h>
#include <memory>
#include <algorithm>
#include <string>
#include <utility>
#include <vector>

namespace aoide {

// ---- fcitx 悬浮面板里的三区渲染 ----
//
// 三区文本不走应用内嵌 preedit，改由 fcitx 自己的面板渲染，因为各应用对内嵌
// 格式的支持参差不齐：GTK 系三种格式都画，VSCode 只画下划线，VTE 终端时有时
// 无，WezTerm 走 XIM 压根不画。统一走面板，四处观感一致。
//
// 但面板的 preedit 那一栏（classicui 里的 upperLayout）被写死了
// pango_layout_set_single_paragraph_mode(true) 且从不设置 layout width：文本
// 里塞 \n 会被压成一行，也没有自动折行，长句会把窗口顶出屏幕——这就是当初
// 放弃悬浮框的原因。候选栏走的是另一条路（MultilineLayout 按 \n 拆行，宽度取
// 各行最大、高度按行数累加，每行照样应用格式标记），所以这里把整块草稿当作
// 一个"只能看、不能选"的候选项塞进候选栏，折行位置由我们自己算。
class PanelTextCandidate : public fcitx::CandidateWord {
public:
    explicit PanelTextCandidate(fcitx::Text text)
        : fcitx::CandidateWord(std::move(text)) {}

    // 纯展示，鼠标点中也不做任何事
    void select(fcitx::InputContext *) const override {}
};

class PanelTextList : public fcitx::CandidateList {
public:
    explicit PanelTextList(fcitx::Text text)
        : candidate_(std::make_unique<PanelTextCandidate>(std::move(text))) {}

    // 返回空标签，面板里就不会画出 "1." 这样的候选序号
    const fcitx::Text &label(int) const override { return emptyLabel_; }
    const fcitx::CandidateWord &candidate(int) const override {
        return *candidate_;
    }
    int size() const override { return 1; }
    // -1 = 没有选中项：否则主题会给整块刷上高亮底色，把绿区标记盖掉
    int cursorIndex() const override { return -1; }
    fcitx::CandidateLayoutHint layoutHint() const override {
        return fcitx::CandidateLayoutHint::Vertical;
    }

private:
    std::unique_ptr<PanelTextCandidate> candidate_;
    fcitx::Text emptyLabel_;
};

inline fcitx::TextFormatFlags styleToFlags(const std::string &style) {
    if (style == "red") return fcitx::TextFormatFlag::Underline;
    if (style == "yellow") return fcitx::TextFormatFlag::Bold;
    if (style == "green") return fcitx::TextFormatFlag::HighLight;
    return fcitx::TextFormatFlag::NoFlag;
}

// 按显示列宽折行：ASCII 与拉丁字母算 1 列，汉字/全角/emoji 算 2 列。像素级
// 宽度取决于字体，这里只求近似，够把窗口宽度约束住即可。
// 只折叠显示：保留开头与最新内容，并明确说明中间内容仍然保留。
inline fcitx::Text buildWrappedText(const std::vector<TextSegment> &segments,
                                    int maxColumns, int maxLines) {
    struct Piece {
        std::string text;
        fcitx::TextFormatFlags flags;
    };

    if (maxColumns < 2) maxColumns = 2;

    std::vector<std::vector<Piece>> lines(1);
    int col = 0;

    for (const auto &seg : segments) {
        const fcitx::TextFormatFlags flags = styleToFlags(seg.style);
        const std::string &s = seg.text;
        std::string cur;
        for (size_t i = 0; i < s.size();) {
            const auto c = static_cast<unsigned char>(s[i]);
            size_t len = 1;
            int width = 1;
            if (c >= 0xF0)      { len = 4; width = 2; }  // emoji 等四字节
            else if (c >= 0xE0) { len = 3; width = 2; }  // 汉字、全角标点
            else if (c >= 0xC0) { len = 2; width = 1; }  // 带音调的拉丁字母等
            if (i + len > s.size()) len = s.size() - i;  // 截断的序列，整段吃掉

            if (c == '\n') {
                if (!cur.empty()) lines.back().push_back({cur, flags});
                cur.clear();
                lines.emplace_back();
                col = 0;
                ++i;
                continue;
            }

            if (col + width > maxColumns && col > 0) {
                if (!cur.empty()) {
                    lines.back().push_back({cur, flags});
                    cur.clear();
                }
                lines.emplace_back();
                col = 0;
            }
            cur.append(s, i, len);
            col += width;
            i += len;
        }
        if (!cur.empty()) lines.back().push_back({cur, flags});
    }

    if (maxLines > 0 && static_cast<int>(lines.size()) > maxLines) {
        maxLines = std::max(4, maxLines);
        if (static_cast<int>(lines.size()) > maxLines) {
            const int head = std::min(3, maxLines - 2);
            const int tail = maxLines - head - 1;
            const int folded = static_cast<int>(lines.size()) - head - tail;
            lines.erase(lines.begin() + head, lines.end() - tail);
            lines.insert(lines.begin() + head,
                {{"（中间 " + std::to_string(folded) + " 行已保留）", fcitx::TextFormatFlag::NoFlag}});
        }
    }

    fcitx::Text text;
    for (size_t i = 0; i < lines.size(); ++i) {
        if (i) text.append("\n");
        for (const auto &piece : lines[i]) {
            text.append(piece.text, piece.flags);
        }
    }
    return text;
}

} // namespace aoide

#endif // AOIDE_PANEL_H
