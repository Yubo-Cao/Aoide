#include "x11_keycheck.h"

#ifdef YUHUANG_HAVE_X11
#include <X11/Xlib.h>
#ifdef YUHUANG_HAVE_XI2
#include <X11/extensions/XInput2.h>
#endif
#include <cstdlib>
#include <cstring>

namespace yuhuang {
namespace {

// 懒初始化独立 X 连接（只在 fcitx 主线程调用，无需 XInitThreads）
Display *dpy = nullptr;
bool tried = false;
int xiOpcode = -1;
bool xiRawOk = false;   // XI ≥ 2.1 raw 事件可用
KeyCode watchKc = 0;
int keyDownState = -1;  // raw 事件追踪的键状态：1 按下 / 0 松开

// Xlib 默认错误处理器会直接 exit 进程——在 fcitx 里绝不可接受。
// 只吞掉自己连接上的错误，其他连接交还原处理器。
using XErrHandler = int (*)(Display *, XErrorEvent *);
XErrHandler prevHandler = nullptr;

int errorHandler(Display *d, XErrorEvent *e) {
    if (d == dpy) return 0;
    return prevHandler ? prevHandler(d, e) : 0;
}

bool ensureDisplay() {
    if (!tried) {
        tried = true;
        if (std::getenv("DISPLAY")) {
            dpy = XOpenDisplay(nullptr);
        }
        if (dpy) {
            prevHandler = XSetErrorHandler(errorHandler);
#ifdef YUHUANG_HAVE_XI2
            int event = 0, error = 0;
            if (XQueryExtension(dpy, "XInputExtension",
                                &xiOpcode, &event, &error)) {
                // 必须声明 2.1+：raw 事件在 grab 期间照常投递是
                // XI2.1 引入的行为，2.0 客户端在 grab 时收不到
                int major = 2, minor = 1;
                if (XIQueryVersion(dpy, &major, &minor) == Success &&
                    (major > 2 || (major == 2 && minor >= 1))) {
                    xiRawOk = true;
                }
            }
#endif
        }
    }
    return dpy != nullptr;
}

#ifdef YUHUANG_HAVE_XI2
// 订阅/退订 root 窗口上的 raw 键盘事件。
// 只在监视期间订阅：常驻订阅会让事件在无人读取的连接上无限堆积。
void selectRaw(bool enable) {
    if (!xiRawOk) return;
    unsigned char bits[XIMaskLen(XI_LASTEVENT)] = {0};
    XIEventMask mask;
    mask.deviceid = XIAllMasterDevices;
    mask.mask_len = sizeof(bits);
    mask.mask = bits;
    if (enable) {
        XISetMask(bits, XI_RawKeyPress);
        XISetMask(bits, XI_RawKeyRelease);
    }
    XISelectEvents(dpy, DefaultRootWindow(dpy), &mask, 1);
    XFlush(dpy);
}
#endif

// 排空连接上的待处理事件；track 时按时序更新被监视键的状态
// （松开→按下→松开也能正确落在最终状态上）
void drainEvents(bool track) {
    while (XPending(dpy)) {
        XEvent ev;
        XNextEvent(dpy, &ev);
#ifdef YUHUANG_HAVE_XI2
        if (track && ev.xcookie.type == GenericEvent &&
            ev.xcookie.extension == xiOpcode &&
            XGetEventData(dpy, &ev.xcookie)) {
            auto *raw = static_cast<XIRawEvent *>(ev.xcookie.data);
            if (raw->detail == watchKc) {
                if (ev.xcookie.evtype == XI_RawKeyRelease) {
                    keyDownState = 0;
                } else if (ev.xcookie.evtype == XI_RawKeyPress) {
                    keyDownState = 1;
                }
            }
            XFreeEventData(dpy, &ev.xcookie);
        }
#else
        (void)track;
#endif
    }
}

// 辅助通道：逻辑键位图。grab 冻结时会滞留"按下"（失效方向是安全的：
// 只会漏报松开，不会误报），未冻结时读数可靠。
int keymapDown(KeyCode kc) {
    char keys[32];
    XQueryKeymap(dpy, keys);
    return (keys[kc / 8] & (1 << (kc % 8))) ? 1 : 0;
}

} // namespace

int x11WatchKeyBegin(unsigned long keysym) {
    if (!ensureDisplay()) return -1;
    watchKc = XKeysymToKeycode(dpy, static_cast<KeySym>(keysym));
    if (watchKc == 0) return -1;
    keyDownState = 1;  // 调用方刚收到按下事件，起始状态必为按下
#ifdef YUHUANG_HAVE_XI2
    if (xiRawOk) {
        selectRaw(true);
        XSync(dpy, False);
        // 带追踪排空：若订阅瞬间用户已松键，raw release 立即生效
        drainEvents(true);
    }
#endif
    return 0;
}

int x11WatchKeyPoll() {
    if (!dpy || watchKc == 0) return -1;
    drainEvents(true);
    // 双通道任一判"已松开"即松开：
    // raw 事件不受 grab 冻结影响（主）；键位图覆盖 raw 不可用时（辅）
    if (keyDownState == 0) return 0;
    if (!keymapDown(watchKc)) return 0;
    return 1;
}

void x11WatchKeyEnd() {
    if (!dpy) return;
#ifdef YUHUANG_HAVE_XI2
    if (xiRawOk) {
        selectRaw(false);
        XSync(dpy, False);
        drainEvents(false);
    }
#endif
    watchKc = 0;
    keyDownState = -1;
}

} // namespace yuhuang

#else  // 无 X11 开发环境（纯 Wayland 构建）：看门狗降级为不可用

namespace yuhuang {
int x11WatchKeyBegin(unsigned long) { return -1; }
int x11WatchKeyPoll() { return -1; }
void x11WatchKeyEnd() {}
} // namespace yuhuang

#endif
