#include "yuhuang_window.h"

#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/shape.h>
#include <cairo/cairo-xlib.h>
#include <cairo/cairo.h>
#include <pango/pangocairo.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace yuhuang {

namespace {

// ---- V2「淡染底色」暗色版配色 ----
// 字色统一浅灰，三区只用半透明色块垫底：饱和色文字是"警告色语言"，
// 红字会被本能读成"错误"，而红区只是最新草稿；低饱和底色既标出区界，
// 又让整句话在视觉上仍是完整的一句话。
constexpr double kTextR = 0xD7 / 255.0, kTextG = 0xDC / 255.0,
                 kTextB = 0xE2 / 255.0;                       // #D7DCE2
constexpr double kPanelR = 0x23 / 255.0, kPanelG = 0x27 / 255.0,
                 kPanelB = 0x2E / 255.0;                      // 面板底 #23272E
constexpr double kChipColors[4][4] = {                        // r,g,b,a
    {0, 0, 0, 0},                                              // 无色块
    {94 / 255.0, 190 / 255.0, 120 / 255.0, 0.18},              // 绿区
    {240 / 255.0, 205 / 255.0, 90 / 255.0, 0.16},              // 黄区
    {235 / 255.0, 110 / 255.0, 110 / 255.0, 0.16},             // 红区
};
constexpr double kDotR = 0xFF / 255.0, kDotG = 0x5F / 255.0,
                 kDotB = 0x56 / 255.0;                        // 录音红点

// 面板几何（96 DPI 基准，实际按 dpi 缩放）
constexpr int kPadX = 16, kPadY = 12;     // 面板内边距
constexpr int kChipPadX = 3, kChipPadY = 2;  // 色块相对文字的外扩
constexpr int kLineGap = 5;               // 行间距
constexpr int kRadius = 11;               // 面板圆角
constexpr int kChipRadius = 5;            // 色块圆角
constexpr int kMargin = 18;               // 透明外边（画阴影用）
constexpr int kDotSize = 8, kDotGap = 10; // 录音点直径与右侧间距
constexpr int kAnchorGap = 7;             // 面板与光标的间距

void roundedRect(cairo_t *cr, double x, double y, double w, double h,
                 double r) {
    r = std::min(r, std::min(w, h) / 2);
    cairo_new_sub_path(cr);
    cairo_arc(cr, x + w - r, y + r, r, -M_PI_2, 0);
    cairo_arc(cr, x + w - r, y + h - r, r, 0, M_PI_2);
    cairo_arc(cr, x + r, y + h - r, r, M_PI_2, M_PI);
    cairo_arc(cr, x + r, y + r, r, M_PI, 3 * M_PI_2);
    cairo_close_path(cr);
}

int zoneOf(fcitx::TextFormatFlags flags) {
    if (flags & fcitx::TextFormatFlag::HighLight) return 1;  // 绿区
    if (flags & fcitx::TextFormatFlag::Bold) return 2;       // 黄区
    if (flags & fcitx::TextFormatFlag::Underline) return 3;  // 红区
    return 0;
}

} // namespace

struct PanelWindow::Impl {
    Display *dpy = nullptr;
    int screen = 0;
    Window win = 0;
    Visual *visual = nullptr;
    Colormap colormap = 0;
    int depth = 24;
    bool argb = false;      // 有 32 位视觉才画阴影和真圆角
    bool mapped = false;
    cairo_surface_t *surface = nullptr;
    double dpi = 96.0;
    double scale = 1.0;     // dpi / 96

    // 缓存的排版结果（Expose 重绘直接用，不重新排）
    struct Piece {
        std::string text;
        int zone;
        int w;              // 文字像素宽（不含色块外扩）
    };
    std::vector<std::vector<Piece>> lines;
    int winW = 0, winH = 0;
    int lineH = 0;          // 单行高（含色块外扩）
    int fontPt = 14;

    bool init();
    void layout(const fcitx::Text &content, int fontPt);
    void place(int anchorX, int anchorY, int anchorH);
    void draw();
    PangoLayout *makeLayout(cairo_t *cr) const;

    ~Impl() {
        if (surface) cairo_surface_destroy(surface);
        if (dpy) {
            if (win) XDestroyWindow(dpy, win);
            if (colormap) XFreeColormap(dpy, colormap);
            XCloseDisplay(dpy);
        }
    }
};

bool PanelWindow::Impl::init() {
    dpy = XOpenDisplay(nullptr);
    if (!dpy) return false;
    screen = DefaultScreen(dpy);

    // Xft.dpi 存在则跟随（GNOME 缩放走这里），否则 96
    if (const char *s = XGetDefault(dpy, "Xft", "dpi")) {
        double v = atof(s);
        if (v >= 48 && v <= 480) dpi = v;
    }
    scale = dpi / 96.0;

    // 优先 32 位 ARGB 视觉：透明外边画阴影、面板圆角外真透明。
    // 拿不到（无合成器等）退 24 位，阴影和透明角退化为实底。
    XVisualInfo vinfo;
    if (XMatchVisualInfo(dpy, screen, 32, TrueColor, &vinfo)) {
        visual = vinfo.visual;
        depth = 32;
        argb = true;
    } else {
        visual = DefaultVisual(dpy, screen);
        depth = DefaultDepth(dpy, screen);
    }
    colormap = XCreateColormap(dpy, RootWindow(dpy, screen), visual, AllocNone);

    XSetWindowAttributes attrs;
    std::memset(&attrs, 0, sizeof(attrs));
    attrs.override_redirect = True;   // 不受窗口管理器摆布，不出现在任务栏
    attrs.save_under = True;
    attrs.colormap = colormap;
    attrs.border_pixel = 0;
    attrs.background_pixel = 0;
    attrs.event_mask = ExposureMask;
    win = XCreateWindow(dpy, RootWindow(dpy, screen), 0, 0, 1, 1, 0, depth,
                        InputOutput, visual,
                        CWOverrideRedirect | CWSaveUnder | CWColormap |
                            CWBorderPixel | CWBackPixel | CWEventMask,
                        &attrs);
    if (!win) return false;
    XStoreName(dpy, win, "yuhuang-panel");

    // 输入区域置空：鼠标点击穿透到下层应用，绝不抢焦点
    XShapeCombineRectangles(dpy, win, ShapeInput, 0, 0, nullptr, 0, ShapeSet,
                            Unsorted);

    surface = cairo_xlib_surface_create(dpy, win, visual, 1, 1);
    return surface != nullptr;
}

PangoLayout *PanelWindow::Impl::makeLayout(cairo_t *cr) const {
    PangoLayout *layout = pango_cairo_create_layout(cr);
    pango_cairo_context_set_resolution(pango_layout_get_context(layout), dpi);
    PangoFontDescription *desc = pango_font_description_from_string("Sans");
    pango_font_description_set_size(desc, fontPt * PANGO_SCALE);
    pango_layout_set_font_description(layout, desc);
    pango_font_description_free(desc);
    pango_layout_set_single_paragraph_mode(layout, TRUE);
    return layout;
}

void PanelWindow::Impl::layout(const fcitx::Text &content, int pt) {
    fontPt = pt;
    lines.assign(1, {});

    cairo_t *cr = cairo_create(surface);
    PangoLayout *pl = makeLayout(cr);

    // 行高用字体度量而非逐段实测，保证各行等高
    PangoContext *pctx = pango_layout_get_context(pl);
    PangoFontMetrics *metrics = pango_context_get_metrics(
        pctx, pango_layout_get_font_description(pl), nullptr);
    const int fontH = PANGO_PIXELS(pango_font_metrics_get_ascent(metrics) +
                                   pango_font_metrics_get_descent(metrics));
    pango_font_metrics_unref(metrics);
    const int chipPadY = std::lround(kChipPadY * scale);
    lineH = fontH + 2 * chipPadY;

    for (size_t i = 0; i < content.size(); ++i) {
        const std::string &s = content.stringAt(i);
        if (s == "\n") {
            lines.emplace_back();
            continue;
        }
        if (s.empty()) continue;
        pango_layout_set_text(pl, s.c_str(), static_cast<int>(s.size()));
        int w = 0, h = 0;
        pango_layout_get_pixel_size(pl, &w, &h);
        lines.back().push_back({s, zoneOf(content.formatAt(i)), w});
    }

    g_object_unref(pl);
    cairo_destroy(cr);

    // 面板尺寸
    const int chipPadX = std::lround(kChipPadX * scale);
    const int dotSpace = std::lround((kDotSize + kDotGap) * scale);
    int contentW = 0;
    for (size_t li = 0; li < lines.size(); ++li) {
        int w = (li == 0) ? dotSpace : 0;
        for (const auto &p : lines[li])
            w += p.w + (p.zone ? 2 * chipPadX : 0);
        contentW = std::max(contentW, w);
    }
    const int n = static_cast<int>(lines.size());
    const int contentH =
        n * lineH + (n - 1) * std::lround(kLineGap * scale);
    const int margin = argb ? std::lround(kMargin * scale) : 0;
    winW = contentW + 2 * std::lround(kPadX * scale) + 2 * margin;
    winH = contentH + 2 * std::lround(kPadY * scale) + 2 * margin;
}

void PanelWindow::Impl::place(int anchorX, int anchorY, int anchorH) {
    const int screenW = DisplayWidth(dpy, screen);
    const int screenH = DisplayHeight(dpy, screen);
    const int margin = argb ? std::lround(kMargin * scale) : 0;
    const int gap = std::lround(kAnchorGap * scale);

    int x, y;
    if (anchorX == 0 && anchorY == 0 && anchorH == 0) {
        // 应用没上报光标位置：退到屏幕底部居中（终端最常见）
        x = (screenW - winW) / 2;
        y = screenH - winH - std::lround(60 * scale);
    } else {
        x = anchorX - margin - std::lround(kPadX * scale);
        y = anchorY + anchorH + gap - margin;
        // 下方放不下就翻到光标上方
        if (y + winH - margin > screenH)
            y = anchorY - gap - winH + margin;
    }
    x = std::clamp(x, -margin, screenW - winW + margin);
    y = std::clamp(y, -margin, screenH - winH + margin);

    XMoveResizeWindow(dpy, win, x, y, winW, winH);
    cairo_xlib_surface_set_size(surface, winW, winH);
}

void PanelWindow::Impl::draw() {
    if (lines.empty() || winW <= 0) return;

    const int margin = argb ? std::lround(kMargin * scale) : 0;
    const double panelX = margin, panelY = margin;
    const double panelW = winW - 2 * margin, panelH = winH - 2 * margin;
    const double radius = kRadius * scale;

    cairo_t *cr = cairo_create(surface);

    // 清底
    cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
    if (argb)
        cairo_set_source_rgba(cr, 0, 0, 0, 0);
    else
        cairo_set_source_rgb(cr, kPanelR, kPanelG, kPanelB);
    cairo_paint(cr);
    cairo_set_operator(cr, CAIRO_OPERATOR_OVER);

    // 柔和阴影：同心圆角矩形逐层加深的廉价近似（真高斯模糊不值得为
    // 这一个窗口引依赖）
    if (argb) {
        const int layers = 8;
        for (int i = layers; i >= 1; --i) {
            const double grow = i * 1.6 * scale;
            roundedRect(cr, panelX - grow, panelY - grow + 2.5 * scale,
                        panelW + 2 * grow, panelH + 2 * grow, radius + grow);
            cairo_set_source_rgba(cr, 0, 0, 0, 0.038);
            cairo_fill(cr);
        }
    }

    // 面板本体 + 一圈淡白描边（暗色面板在深色桌面上靠它分得清边界）
    roundedRect(cr, panelX, panelY, panelW, panelH, radius);
    cairo_set_source_rgb(cr, kPanelR, kPanelG, kPanelB);
    cairo_fill_preserve(cr);
    cairo_set_source_rgba(cr, 1, 1, 1, 0.06);
    cairo_set_line_width(cr, 1.0 * scale);
    cairo_stroke(cr);

    // 内容
    const int padX = std::lround(kPadX * scale);
    const int padY = std::lround(kPadY * scale);
    const int chipPadX = std::lround(kChipPadX * scale);
    const int chipPadY = std::lround(kChipPadY * scale);
    const int lineGap = std::lround(kLineGap * scale);
    const double dotD = kDotSize * scale;

    PangoLayout *pl = makeLayout(cr);

    double y = panelY + padY;
    for (size_t li = 0; li < lines.size(); ++li) {
        double x = panelX + padX;
        if (li == 0) {
            // 录音红点，第一行行高居中
            cairo_set_source_rgb(cr, kDotR, kDotG, kDotB);
            cairo_arc(cr, x + dotD / 2, y + lineH / 2.0, dotD / 2, 0,
                      2 * M_PI);
            cairo_fill(cr);
            x += std::lround((kDotSize + kDotGap) * scale);
        }
        for (const auto &p : lines[li]) {
            const double chipW = p.w + (p.zone ? 2 * chipPadX : 0);
            if (p.zone) {
                roundedRect(cr, x, y, chipW, lineH, kChipRadius * scale);
                const double *c = kChipColors[p.zone];
                cairo_set_source_rgba(cr, c[0], c[1], c[2], c[3]);
                cairo_fill(cr);
            }
            pango_layout_set_text(pl, p.text.c_str(),
                                  static_cast<int>(p.text.size()));
            cairo_set_source_rgb(cr, kTextR, kTextG, kTextB);
            cairo_move_to(cr, x + (p.zone ? chipPadX : 0), y + chipPadY);
            pango_cairo_show_layout(cr, pl);
            x += chipW;
        }
        y += lineH + lineGap;
    }

    g_object_unref(pl);
    cairo_destroy(cr);
    cairo_surface_flush(surface);
    XFlush(dpy);
}

PanelWindow::PanelWindow() : impl_(std::make_unique<Impl>()) {
    if (!impl_->init()) impl_.reset();
}

PanelWindow::~PanelWindow() = default;

bool PanelWindow::available() const { return impl_ != nullptr; }

void PanelWindow::show(const fcitx::Text &content, int anchorX, int anchorY,
                       int anchorH, int fontPt) {
    if (!impl_) return;
    if (content.empty()) {
        hide();
        return;
    }
    impl_->layout(content, fontPt);
    impl_->place(anchorX, anchorY, anchorH);
    if (!impl_->mapped) {
        XMapRaised(impl_->dpy, impl_->win);
        impl_->mapped = true;
    }
    impl_->draw();
}

void PanelWindow::hide() {
    if (!impl_ || !impl_->mapped) return;
    XUnmapWindow(impl_->dpy, impl_->win);
    XFlush(impl_->dpy);
    impl_->mapped = false;
}

int PanelWindow::fd() const {
    return impl_ ? ConnectionNumber(impl_->dpy) : -1;
}

void PanelWindow::processEvents() {
    if (!impl_) return;
    XEvent ev;
    bool needRedraw = false;
    while (XPending(impl_->dpy)) {
        XNextEvent(impl_->dpy, &ev);
        if (ev.type == Expose && ev.xexpose.count == 0) needRedraw = true;
    }
    if (needRedraw && impl_->mapped) impl_->draw();
}

} // namespace yuhuang
