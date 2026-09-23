cmake_minimum_required(VERSION 3.21)
project(Aoide VERSION 0.2.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 查找 fcitx5 依赖
find_package(Fcitx5Core 5.1.0 REQUIRED)
find_package(Fcitx5Config QUIET)
find_package(PkgConfig REQUIRED)

# fcitx5 编译设置
include("${FCITX_INSTALL_CMAKECONFIG_DIR}/Fcitx5Utils/Fcitx5CompilerSettings.cmake")

add_subdirectory(src/engine)

find_program(AOIDE_MSGFMT_EXECUTABLE msgfmt REQUIRED)
file(GLOB AOIDE_PO_FILES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/po/*.po")
foreach(po_file IN LISTS AOIDE_PO_FILES)
    get_filename_component(locale "${po_file}" NAME_WE)
    set(mo_file "${CMAKE_CURRENT_BINARY_DIR}/locale/${locale}/LC_MESSAGES/aoide.mo")
    add_custom_command(OUTPUT "${mo_file}"
        COMMAND "${CMAKE_COMMAND}" -E make_directory "${CMAKE_CURRENT_BINARY_DIR}/locale/${locale}/LC_MESSAGES"
        COMMAND "${AOIDE_MSGFMT_EXECUTABLE}" --check -o "${mo_file}" "${po_file}"
        DEPENDS "${po_file}"
        VERBATIM)
    add_custom_target("aoide-${locale}-translations" ALL DEPENDS "${mo_file}")
    install(FILES "${mo_file}" DESTINATION "share/locale/${locale}/LC_MESSAGES")
endforeach()
