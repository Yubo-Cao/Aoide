# 生成 aoide.so (不带 lib 前缀)
find_package(nlohmann_json REQUIRED)
add_library(aoide SHARED
    aoide_engine.cpp
    x11_keycheck.cpp
)

target_link_libraries(aoide PRIVATE
    Fcitx5::Core
    Fcitx5::Config
    Fcitx5::Utils
    nlohmann_json::nlohmann_json
)

# ★ PTT 看门狗依赖 Xlib + XInput2（raw 事件监听物理键盘，
# grab 冻结期间照常投递；XQueryKeymap 作辅助通道）。
# 找不到时逐级降级：无 Xi 则只用键位图，无 X11 则看门狗不启用。
find_package(X11)
if(X11_FOUND)
    target_include_directories(aoide PRIVATE ${X11_INCLUDE_DIR})
    target_link_libraries(aoide PRIVATE ${X11_LIBRARIES})
    target_compile_definitions(aoide PRIVATE AOIDE_HAVE_X11)
    if(X11_Xi_FOUND)
        target_link_libraries(aoide PRIVATE ${X11_Xi_LIB})
        target_compile_definitions(aoide PRIVATE AOIDE_HAVE_XI2)
        message(STATUS "Aoide: X11+Xi found, PTT watchdog with XI2 raw events")
    else()
        message(STATUS "Aoide: X11 found (no Xi), PTT watchdog keymap-only")
    endif()
else()
    message(STATUS "Aoide: X11 not found, PTT watchdog disabled")
endif()

# ★ 自绘悬浮草稿窗依赖 cairo + pango（三区淡染底色 classicui 画不出来）
# 和 Xext（XShape 点击穿透）。找不到则不编这一路，运行时回退候选栏。
find_package(PkgConfig QUIET)
if(PKG_CONFIG_FOUND AND X11_FOUND AND X11_Xext_FOUND)
    pkg_check_modules(PANEL IMPORTED_TARGET cairo cairo-xlib pangocairo)
    if(PANEL_FOUND)
        target_sources(aoide PRIVATE aoide_window.cpp)
        target_link_libraries(aoide PRIVATE PkgConfig::PANEL ${X11_Xext_LIB})
        target_compile_definitions(aoide PRIVATE AOIDE_HAVE_PANEL)
        message(STATUS "Aoide: cairo/pango found, self-drawn floating panel enabled")
    else()
        message(STATUS "Aoide: cairo/pango not found, floating panel falls back to candidate bar")
    endif()
else()
    message(STATUS "Aoide: pkg-config/Xext missing, floating panel falls back to candidate bar")
endif()

target_include_directories(aoide PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
)

set_target_properties(aoide PROPERTIES PREFIX "")

# 安装共享库
install(TARGETS aoide DESTINATION "${FCITX_INSTALL_LIBDIR}/fcitx5")

# 安装 addon 注册文件
configure_file(aoide-addon.conf.in aoide-addon.conf)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/aoide-addon.conf"
        RENAME aoide.conf
        DESTINATION "${FCITX_INSTALL_PKGDATADIR}/addon")

# ★ 纯 addon（Category=Module）不再需要 inputmethod 注册文件。
# 旧版若已安装过 inputmethod/aoide.conf，需手动删除以免 Aoide
# 仍出现在输入法列表里：sudo rm /usr/share/fcitx5/inputmethod/aoide.conf

# 安装配置文件 (带 GUI 描述，供 fcitx5-configtool 使用)
install(FILES aoide.conf
        DESTINATION "${FCITX_INSTALL_PKGDATADIR}/config")

# KDE's Input Method KCM hosts the native addon configuration page. Give it a
# searchable application-launcher entry so users can find it by product name.
install(FILES aoide-settings.desktop
        DESTINATION "share/applications")
