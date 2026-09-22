#ifndef AOIDE_X11_KEYCHECK_H
#define AOIDE_X11_KEYCHECK_H

namespace aoide {

// ★ PTT 看门狗底座：XInput2 raw 事件监听物理键盘。
//
// 背景：GNOME 键盘 grab（通知弹窗/Overview 等）会吞掉松键事件，
// 实录 5.5 分钟会话中物理松键+补按均未送达 fcitx，导致 PTT 卡死。
// 曾用 XQueryKeymap 轮询，但它返回的是"逻辑键位状态"——
// 同步 grab 冻结事件处理时逻辑状态同样滞留在"按下"（X11 规范原文：
// "the logical state may lag the physical state if device event
// processing is frozen"），探测器和事件流死在同一把刀下，实测失效。
//
// XI2 ≥ 2.1 的 raw 事件是设备层信号，grab/冻结期间照常向 root
// 监听者投递（XI2.1 引入的核心特性），这才是真正的物理键位通道。
// 实现上 raw 事件为主、XQueryKeymap 为辅：任一通道判"已松开"即停。

// 开始监视 keysym 对应按键（调用方刚收到该键按下事件时调用）。
// 返回 0 = 成功，-1 = 不可用（非 X11 会话/查询失败）。
int x11WatchKeyBegin(unsigned long keysym);

// 轮询被监视键状态：1 = 仍按下，0 = 已松开，-1 = 不可用。
int x11WatchKeyPoll();

// 结束监视（取消 raw 事件订阅，防止事件在连接上无限堆积）。
void x11WatchKeyEnd();

} // namespace aoide

#endif // AOIDE_X11_KEYCHECK_H
