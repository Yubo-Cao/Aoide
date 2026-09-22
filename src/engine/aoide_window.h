#ifndef AOIDE_WINDOW_H
#define AOIDE_WINDOW_H

#include <fcitx/text.h>
#include <memory>

namespace aoide {

// ---- 自绘悬浮草稿窗（X11 + Cairo + Pango）----
//
// classicui 的格式标记只有下划线/加粗/高亮三种，颜色全部来自主题全局配置，
// 三区各自的淡染底色它画不出来。这里自己开一个 override-redirect 窗口自己画：
// 暗色圆角面板（#23272E）+ 柔和阴影，字色统一浅灰，三区用半透明色块垫底
// （绿/黄/红各 16%~18% 透明度），观感与商业输入法对齐。
//
// 输入区域用 XShape 置空，鼠标点击直接穿透到下层应用，不抢焦点。
// 定位数据来自应用上报的光标矩形（与 classicui 同源），不上报的应用
// 退到屏幕底部居中。所有调用都发生在 fcitx 主线程。
class PanelWindow {
public:
    PanelWindow();
    ~PanelWindow();

    PanelWindow(const PanelWindow &) = delete;
    PanelWindow &operator=(const PanelWindow &) = delete;

    // X 连接是否建立成功（失败时调用方应回退到候选栏渲染）
    bool available() const;

    // 显示草稿。content 为已折好行的带格式文本（\n 分行，格式标记映射
    // 色块：HighLight=绿区 Bold=黄区 Underline=红区 NoFlag=无色块）。
    // (anchorX, anchorY, anchorH) 是应用光标矩形，根窗口坐标。
    void show(const fcitx::Text &content, int anchorX, int anchorY,
              int anchorH, int fontPt);
    void moveToAnchor(int anchorX, int anchorY, int anchorH);

    void hide();

    // X 连接的 fd，供 fcitx 事件循环监听（Expose 重绘等）
    int fd() const;
    void processEvents();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace aoide

#endif // AOIDE_WINDOW_H
