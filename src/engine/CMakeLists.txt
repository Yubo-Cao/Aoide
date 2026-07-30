# 生成 yuhuang.so (不带 lib 前缀)
add_library(yuhuang SHARED
    yuhuang_engine.cpp
    x11_keycheck.cpp
)

target_link_libraries(yuhuang PRIVATE
    Fcitx5::Core
    Fcitx5::Config
    Fcitx5::Utils
)

# ★ v3.8.11 PTT 看门狗依赖 Xlib + XInput2（raw 事件监听物理键盘，
# grab 冻结期间照常投递；XQueryKeymap 作辅助通道）。
# 找不到时逐级降级：无 Xi 则只用键位图，无 X11 则看门狗不启用。
find_package(X11)
if(X11_FOUND)
    target_include_directories(yuhuang PRIVATE ${X11_INCLUDE_DIR})
    target_link_libraries(yuhuang PRIVATE ${X11_LIBRARIES})
    target_compile_definitions(yuhuang PRIVATE YUHUANG_HAVE_X11)
    if(X11_Xi_FOUND)
        target_link_libraries(yuhuang PRIVATE ${X11_Xi_LIB})
        target_compile_definitions(yuhuang PRIVATE YUHUANG_HAVE_XI2)
        message(STATUS "YuHuang: X11+Xi found, PTT watchdog with XI2 raw events")
    else()
        message(STATUS "YuHuang: X11 found (no Xi), PTT watchdog keymap-only")
    endif()
else()
    message(STATUS "YuHuang: X11 not found, PTT watchdog disabled")
endif()

target_include_directories(yuhuang PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
)

set_target_properties(yuhuang PROPERTIES PREFIX "")

# 安装共享库
install(TARGETS yuhuang DESTINATION "${FCITX_INSTALL_LIBDIR}/fcitx5")

# 安装 addon 注册文件
configure_file(yuhuang-addon.conf.in yuhuang-addon.conf)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/yuhuang-addon.conf"
        RENAME yuhuang.conf
        DESTINATION "${FCITX_INSTALL_PKGDATADIR}/addon")

# 安装输入法注册文件 (文件名必须与 addon 名称一致)
install(FILES yuhuang-inputmethod.conf
        RENAME yuhuang.conf
        DESTINATION "${FCITX_INSTALL_PKGDATADIR}/inputmethod")

# 安装配置文件 (带 GUI 描述，供 fcitx5-configtool 使用)
install(FILES yuhuang.conf
        DESTINATION "${FCITX_INSTALL_PKGDATADIR}/config")
