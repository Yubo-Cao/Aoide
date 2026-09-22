#!/bin/bash
# Aoide installer for Ubuntu 24.04 + fcitx5
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$HOME/.config/aoide/venv"
OLD_CONFIG_DIR="$HOME/.config/yuhuang"

usage() {
    echo "Usage: $0 {install|uninstall}"
    echo ""
    echo "  install   - Install/upgrade Aoide voice input (default)"
    echo "  uninstall - Remove Aoide completely"
    exit 1
}

ACTION="${1:-install}"

# ── 工具函数 ─────────────────────────────────────────

pkg_installed() {
    dpkg -s "$1" 2>/dev/null | grep -q "^Status.*installed"
}

detect_fcitx5_version() {
    if command -v fcitx5 &>/dev/null; then
        fcitx5 --version 2>&1 | grep -oP '\d+\.\d+\.\d+' | head -1
    else
        echo "0.0.0"
    fi
}

ver_ge() {
    # 比较版本号 v1 >= v2
    [ "$(printf '%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]
}

# ─────────────────────────────────────────────────────
#  UNINSTALL
# ─────────────────────────────────────────────────────
if [ "$ACTION" = "uninstall" ]; then
    echo "=========================================="
    echo "  聆序（Aoide）卸载"
    echo "=========================================="
    echo ""

    # 1. 停止受 systemd 管理的后端服务
    echo -e "${YELLOW}[1/6] 停止后端服务...${NC}"
    systemctl --user stop aoide-backend 2>/dev/null && \
        echo "  ✓ Backend service stopped" || true
    systemctl --user disable aoide-backend 2>/dev/null && \
        echo "  ✓ Backend autostart disabled" || true
    rm -f ~/.config/systemd/user/aoide-backend.service && \
        echo "  ✓ Removed systemd user service" || true
    systemctl --user daemon-reload 2>/dev/null || true
    if [ -S /tmp/yuhuang-backend.sock ]; then
        rm -f /tmp/yuhuang-backend.sock
        echo "  ✓ Unix socket removed"
    fi
    if [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -S "$XDG_RUNTIME_DIR/aoide/backend.sock" ]; then
        rm -f "$XDG_RUNTIME_DIR/aoide/backend.sock"
        rmdir "$XDG_RUNTIME_DIR/aoide" 2>/dev/null || true
    fi
    # 2. 删除 fcitx5 插件
    echo -e "${YELLOW}[2/6] 删除 fcitx5 插件文件...${NC}"
    REMOVED=0
    for f in /usr/lib/fcitx5/aoide.so \
             /usr/lib/x86_64-linux-gnu/fcitx5/aoide.so \
             /usr/local/lib/fcitx5/aoide.so \
             /usr/share/fcitx5/addon/aoide.conf \
             /usr/share/fcitx5/inputmethod/aoide-inputmethod.conf \
             /usr/share/fcitx5/config/aoide.conf \
             /usr/share/applications/aoide-settings.desktop; do
        if [ -f "$f" ]; then
            sudo rm -f "$f" && echo "  ✓ Removed $f" && REMOVED=$((REMOVED+1))
        fi
    done
    [ $REMOVED -eq 0 ] && echo "  - No plugin files found"
    echo "  $(hash fcitx5 2>/dev/null && echo '⟹  Run: fcitx5 -r  to reload')"

    # 3. 卸载 apt 包 (仅限安装清单中记录的)
    echo -e "${YELLOW}[3/6] 卸载 apt 依赖包...${NC}"
    MANIFEST_APT="$HOME/.config/aoide/manifest/apt.txt"
    if [ -f "$MANIFEST_APT" ]; then
        while IFS= read -r pkg; do
            [ -z "$pkg" ] && continue
            if ! pkg_installed "$pkg" 2>/dev/null; then
                echo "  - $pkg 已不在系统中，跳过"
                continue
            fi
            # 检查反向依赖：是否有其他已安装的包依赖它
            RDEP=$(apt-cache rdepends --installed "$pkg" 2>/dev/null | tail -n +2 | tr -d ' ' | grep -v "^$pkg$" | wc -l)
            if [ "$RDEP" -eq 0 ]; then
                sudo apt remove -y "$pkg" 2>/dev/null && \
                    echo "  ✓ Removed $pkg" || \
                    echo -e "  ${YELLOW}⚠ 无法卸载 $pkg${NC}"
            else
                echo -e "  ${YELLOW}⚠ $pkg 被 $RDEP 个其他包依赖，跳过卸载${NC}"
            fi
        done < "$MANIFEST_APT"
    else
        echo "  - 无安装清单，跳过"
    fi

    # 4. 卸载 Python 包
    echo -e "${YELLOW}[4/6] 卸载 Python 包...${NC}"
    # 优先从 venv 卸载
    if [ -f "$VENV_DIR/bin/pip" ]; then
        "$VENV_DIR/bin/pip" uninstall -y aoide-backend 2>/dev/null && \
            echo "  ✓ Uninstalled from venv" || echo "  - Not in venv"
    fi
    # 也检查系统 pip
    pip3 uninstall -y aoide-backend 2>/dev/null && \
        echo "  ✓ Uninstalled from system pip" || true

    # 5. 删除用户配置
    echo -e "${YELLOW}[5/6] 删除用户配置文件...${NC}"
    [ -d ~/.config/aoide ] && rm -rf ~/.config/aoide && echo "  ✓ Removed ~/.config/aoide/"
    [ -f ~/.config/fcitx5/conf/aoide.conf ] && rm -f ~/.config/fcitx5/conf/aoide.conf && \
        echo "  ✓ Removed fcitx5 user config"

    # 6. 清理项目目录
    echo -e "${YELLOW}[6/6] 清理项目目录...${NC}"
    [ -d "$SCRIPT_DIR/build" ] && rm -rf "$SCRIPT_DIR/build" && echo "  ✓ Removed build/"

    # 清理 PATH 软链接
    for cmd in aoide-ctl aoide-backend aoide-settings; do
        [ -L "$HOME/.local/bin/$cmd" ] && rm -f "$HOME/.local/bin/$cmd" && \
            echo "  ✓ Removed ~/.local/bin/$cmd"
    done

    # 刷新 fcitx5
    echo ""
    echo -e "${CYAN}═══ 刷新输入法 ═══${NC}"
    if command -v fcitx5 &>/dev/null; then
        fcitx5 -r -d 2>/dev/null && echo -e "  ${GREEN}✓${NC} fcitx5 已刷新" || \
            echo -e "  ${YELLOW}○${NC} fcitx5 未运行，跳过刷新${NC}"
    fi

    echo ""
    echo -e "${GREEN}卸载完成。${NC}"
    exit 0
fi

# ─────────────────────────────────────────────────────
#  INSTALL  —  环境检测 + 按需安装
# ─────────────────────────────────────────────────────
if [ "$ACTION" != "install" ]; then usage; fi
if [ "$EUID" -eq 0 ]; then
    echo -e "${RED}请不要使用 sudo 运行此脚本${NC}"
    exit 1
fi

# Upgrade a previous YuHuang installation before creating any new defaults.
# Keep existing YAML, vocabulary, model cache, and API environment file.
if [ -d "$OLD_CONFIG_DIR" ] && [ ! -e "$HOME/.config/aoide" ]; then
    systemctl --user stop yuhuang-backend.service 2>/dev/null || true
    mv "$OLD_CONFIG_DIR" "$HOME/.config/aoide"
    if [ -d "$VENV_DIR" ]; then
        python3 -m venv --upgrade "$VENV_DIR"
        OLD_VENV="$OLD_CONFIG_DIR/venv" NEW_VENV="$VENV_DIR" python3 - <<'PY'
import os
from pathlib import Path
for script in (Path(os.environ["NEW_VENV"]) / "bin").iterdir():
    if not script.is_file() or script.is_symlink():
        continue
    try:
        data = script.read_bytes()
    except OSError:
        continue
    old = os.environ["OLD_VENV"].encode()
    if b"\0" not in data and old in data:
        script.write_bytes(data.replace(old, os.environ["NEW_VENV"].encode()))
PY
    fi
fi
for base in "$HOME/.local/state" "$HOME/.local/lib" "$HOME/.local/share"; do
    if [ -d "$base/yuhuang" ] && [ ! -e "$base/aoide" ]; then
        mv "$base/yuhuang" "$base/aoide"
    fi
done
if [ -f "$HOME/.config/fcitx5/conf/yuhuang.conf" ] && \
   [ ! -e "$HOME/.config/fcitx5/conf/aoide.conf" ]; then
    mv "$HOME/.config/fcitx5/conf/yuhuang.conf" "$HOME/.config/fcitx5/conf/aoide.conf"
fi
if [ -f "$HOME/.config/fcitx5/conf/aoide.conf" ]; then
    sed -i -e 's|/tmp/yuhuang-backend.sock|auto|g' \
        -e 's|YUHUANG_OPENAI_API_KEY|AOIDE_OPENAI_API_KEY|g' \
        -e 's|YUHUANG_ELEVENLABS_API_KEY|AOIDE_ELEVENLABS_API_KEY|g' \
        -e 's|^0=Control+Shift+Q$|0=Control+Alt+Y|g' \
        "$HOME/.config/fcitx5/conf/aoide.conf"
fi
if [ -f "$HOME/.config/aoide/config.yaml" ]; then
    sed -i -e 's|/tmp/yuhuang-backend.sock|auto|g' \
        -e 's|YUHUANG_OPENAI_API_KEY|AOIDE_OPENAI_API_KEY|g' \
        -e 's|YUHUANG_ELEVENLABS_API_KEY|AOIDE_ELEVENLABS_API_KEY|g' \
        "$HOME/.config/aoide/config.yaml"
fi
if [ -f "$HOME/.config/aoide/cloud.env" ]; then
    sed -i -e 's|^YUHUANG_OPENAI_API_KEY=|AOIDE_OPENAI_API_KEY=|' \
        -e 's|^YUHUANG_ELEVENLABS_API_KEY=|AOIDE_ELEVENLABS_API_KEY=|' \
        "$HOME/.config/aoide/cloud.env"
fi
if [ -d "$HOME/.config/systemd/user/yuhuang-backend.service.d" ]; then
    mv "$HOME/.config/systemd/user/yuhuang-backend.service.d" \
       "$HOME/.config/systemd/user/aoide-backend.service.d"
fi
if [ -f "$HOME/.config/systemd/user/aoide-backend.service.d/cloud.conf" ]; then
    sed -i 's|/.config/yuhuang/|/.config/aoide/|g' \
        "$HOME/.config/systemd/user/aoide-backend.service.d/cloud.conf"
fi

echo "=========================================="
echo "  聆序（Aoide）语音输入 — 安装"
echo "  $(date '+%Y-%m-%d %H:%M')"
echo "=========================================="
echo ""

NEED_APT_INSTALL=""
NEED_REBOOT="no"
HAS_FCITX5=false
HAS_VENV=false
HAS_PLUGIN=false

# ── 1. 环境检测 ────────────────────────────────────

echo -e "${CYAN}═══ 环境检测 ═══${NC}"

# 系统版本
. /etc/os-release 2>/dev/null || true
echo -e "  系统: ${PRETTY_NAME:-$(uname -s)}"

# Python
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
    if [ "$(echo "$PY_VER >= 3.11" | bc -l 2>/dev/null)" = 1 ] || [ "$PY_VER" = "3.11" ] || [ "$PY_VER" = "3.12" ] || [ "$PY_VER" = "3.13" ]; then
        echo -e "  ${GREEN}✓${NC} Python ${PY_VER}"
    else
        echo -e "  ${RED}✗${NC} Python ${PY_VER} (需要 >= 3.11)"
        exit 1
    fi
else
    echo -e "  ${RED}✗${NC} python3 未安装"
    NEED_APT_INSTALL="$NEED_APT_INSTALL python3"
fi

# fcitx5
FCITX5_VER=$(detect_fcitx5_version)
if [ "$FCITX5_VER" != "0.0.0" ]; then
    echo -e "  ${GREEN}✓${NC} fcitx5 ${FCITX5_VER}"
    HAS_FCITX5=true
else
    echo -e "  ${YELLOW}○${NC} fcitx5 未安装 → 将安装"
    NEED_APT_INSTALL="$NEED_APT_INSTALL fcitx5"
fi

# 检查已有插件
if [ -f /usr/lib/fcitx5/aoide.so ]; then
    echo -e "  ${GREEN}✓${NC} 聆序插件已安装 (将升级)"
    HAS_PLUGIN=true
fi

# 检查 venv
if [ -d "$VENV_DIR" ]; then
    echo -e "  ${GREEN}✓${NC} Python 虚拟环境已存在"
    HAS_VENV=true
fi

# ── 逐项检查系统包 ─────────────────────────────────

PKGS_TO_INSTALL=""
check_pkg() {
    local pkg=$1 label=$2
    if pkg_installed "$pkg"; then
        echo -e "  ${GREEN}✓${NC} ${label:-$pkg}"
    else
        echo -e "  ${YELLOW}○${NC} ${label:-$pkg} → 将安装"
        PKGS_TO_INSTALL="$PKGS_TO_INSTALL $pkg"
    fi
}

echo ""
echo -e "${CYAN}═══ 软件包检测 ═══${NC}"

# fcitx5 本体 + 前端
if $HAS_FCITX5; then
    echo -e "  ${GREEN}✓${NC} fcitx5 (已安装)"
else
    PKGS_TO_INSTALL="$PKGS_TO_INSTALL fcitx5"
fi
check_pkg fcitx5-chinese-addons   "fcitx5-chinese-addons"
check_pkg fcitx5-frontend-gtk4    "fcitx5-gtk4 前端"
check_pkg fcitx5-frontend-qt5     "fcitx5-qt5  前端"
check_pkg fcitx5-config-qt        "fcitx5-config-qt (GUI 配置)"
check_pkg libfcitx5core-dev       "libfcitx5core-dev (编译头文件)"
check_pkg libfcitx5config-dev     "libfcitx5config-dev (配置 API)"
check_pkg extra-cmake-modules     "extra-cmake-modules"
check_pkg cmake                   "cmake"
check_pkg pkg-config              "pkg-config"
check_pkg gettext                 "gettext"
check_pkg libcairo2-dev           "libcairo2-dev (悬浮草稿窗)"
check_pkg libpango1.0-dev         "libpango1.0-dev (悬浮草稿窗)"
check_pkg libxext-dev             "libxext-dev (悬浮窗点击穿透)"
check_pkg python3-venv            "python3-venv"
check_pkg portaudio19-dev         "portaudio19-dev (音频)"
check_pkg pulseaudio-utils        "pulseaudio-utils"
check_pkg libsecret-tools         "Secret Service API key storage"

# ── 可选：检查 FunASR ──────────────────────────────
HAS_FUNASR=false
if "$VENV_DIR/bin/python3" -c "import funasr" 2>/dev/null || \
   python3 -c "import funasr" 2>/dev/null; then
    HAS_FUNASR=true
    echo -e "  ${GREEN}✓${NC} FunASR (已安装)"
fi

# ── 保存安装清单 (记录本次新装的包，供卸载时精确移除) ──
MANIFEST_DIR="$HOME/.config/aoide/manifest"
mkdir -p "$MANIFEST_DIR"
if [ -n "$PKGS_TO_INSTALL" ]; then
    for pkg in $PKGS_TO_INSTALL; do
        echo "$pkg" >> "$MANIFEST_DIR/apt.txt"
    done
    sort -u "$MANIFEST_DIR/apt.txt" -o "$MANIFEST_DIR/apt.txt"
fi

echo ""

# ── 2. 安装缺失的系统包 ────────────────────────────

if [ -n "$PKGS_TO_INSTALL" ]; then
    echo -e "${YELLOW}[安装] 以下软件包需要安装:${NC}"
    for p in $PKGS_TO_INSTALL; do echo "   • $p"; done
    echo ""
    echo -e "${YELLOW}正在安装...${NC}"
    sudo apt update -qq
    sudo apt install -y $PKGS_TO_INSTALL
    echo -e "${GREEN}✓ 系统包安装完成${NC}"
else
    echo -e "${GREEN}✓ 所有系统包已就绪，跳过安装${NC}"
fi

# ── 3. 配置环境变量 (如缺失) ──────────────────────

echo ""
echo -e "${CYAN}═══ 环境变量检查 ═══${NC}"
if [ ! -f /etc/profile.d/fcitx5.sh ]; then
    echo -e "  ${YELLOW}○${NC} fcitx5 环境变量未配置 → 写入"
    sudo tee /etc/profile.d/fcitx5.sh > /dev/null << 'EOF'
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
export SDL_IM_MODULE=fcitx
export GLFW_IM_MODULE=ibus
EOF
    NEED_REBOOT="yes"
else
    echo -e "  ${GREEN}✓${NC} fcitx5 环境变量已配置"
fi

# ── 4. 编译/升级 fcitx5 插件 ──────────────────────

echo ""
echo -e "${CYAN}═══ 编译 fcitx5 插件 ═══${NC}"

# 检查编译环境
if ! command -v cmake &>/dev/null; then
    echo -e "${RED}✗ cmake 不可用，请先安装 cmake${NC}"
    exit 1
fi

# 检查 fcitx5 头文件
if ! pkg-config --cflags Fcitx5Core &>/dev/null 2>&1 && \
   [ ! -d /usr/include/Fcitx5/Core ]; then
    echo -e "${RED}✗ fcitx5 开发头文件缺失，请安装 libfcitx5core-dev${NC}"
    exit 1
fi

cd "$SCRIPT_DIR"
mkdir -p build && cd build

cmake .. -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_SYSCONFDIR=/etc
make -j"$(nproc)"
sudo make install
if [ -f /usr/share/applications/aoide-settings.desktop ]; then
    echo -e "  ${GREEN}✓${NC} KDE 设置入口已安装"
fi
echo -e "${GREEN}✓ 聆序 fcitx5 插件已安装 (aoide.so)${NC}"

# ── 5. Python 虚拟环境 + 依赖 ─────────────────────

echo ""
echo -e "${CYAN}═══ Python 后端 ═══${NC}"

if $HAS_VENV; then
    echo -e "  ${GREEN}✓${NC} 虚拟环境已存在，更新依赖..."
else
    echo -e "  ${YELLOW}○${NC} 创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# 只升级 pip 本身，不升级已安装的包
pip install --upgrade pip -q 2>/dev/null || true

# 检查并安装 Python 依赖
MISSING_PIPS=""
for pkg in sounddevice httpx pyyaml; do
    if python3 -c "import $pkg" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $pkg"
    else
        echo -e "  ${YELLOW}○${NC} $pkg → 安装"
        MISSING_PIPS="$MISSING_PIPS $pkg"
    fi
done

if [ -n "$MISSING_PIPS" ]; then
    pip install $MISSING_PIPS
fi

# 安装 Aoide 命令
pip uninstall -y aoide-backend -q 2>/dev/null || true
pip install -e "$SCRIPT_DIR[gui]" -q 2>/dev/null || pip install -e "$SCRIPT_DIR[gui]"
echo -e "  ${GREEN}✓${NC} aoide-backend / aoide-ctl"

# 记录 pip 安装清单
if [ -n "$MISSING_PIPS" ]; then
    for pkg in $MISSING_PIPS; do
        echo "$pkg" >> "$MANIFEST_DIR/pip.txt"
    done
fi
echo "aoide-backend" >> "$MANIFEST_DIR/pip.txt"
sort -u "$MANIFEST_DIR/pip.txt" -o "$MANIFEST_DIR/pip.txt"

# 创建 Aoide 命令和兼容别名的 PATH 软链接
echo ""
echo -e "${CYAN}═══ 系统 PATH 集成 ═══${NC}"
mkdir -p "$HOME/.local/bin"
for cmd in aoide-ctl aoide-backend aoide-settings; do
    if [ -f "$VENV_DIR/bin/$cmd" ]; then
        ln -sf "$VENV_DIR/bin/$cmd" "$HOME/.local/bin/$cmd"
        echo -e "  ${GREEN}✓${NC} ~/.local/bin/$cmd"
    fi
done

# 检查 ~/.local/bin 是否在 PATH 中
if ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
    echo -e "  ${YELLOW}⚠ ~/.local/bin 不在 PATH 中，请在 ~/.bashrc 添加:${NC}"
    echo '    export PATH="$HOME/.local/bin:$PATH"'
fi

# ── 6. FunASR (可选) ──────────────────────────────

echo ""
echo -e "${CYAN}═══ 语音识别引擎 (可选) ═══${NC}"
if $HAS_FUNASR; then
    echo -e "  ${GREEN}✓${NC} FunASR 已安装"
else
    echo -e "  ${YELLOW}○${NC} FunASR 未安装"
    echo ""
    # 循环验证输入，直到 y/n 或 Ctrl+C
    while true; do
        read -p "  是否安装 FunASR (~1GB 模型文件)? (y/n) " -n 1 -r
        echo
        case "$REPLY" in
            [Yy])
                pip install funasr torch torchaudio modelscope
                for pkg in funasr torch torchaudio modelscope; do
                    echo "$pkg" >> "$MANIFEST_DIR/pip.txt"
                done
                sort -u "$MANIFEST_DIR/pip.txt" -o "$MANIFEST_DIR/pip.txt"
                echo -e "${GREEN}✓ FunASR 安装完成${NC}"
                break
                ;;
            [Nn])
                echo -e "  ${YELLOW}跳过 (可稍后: ${VENV_DIR}/bin/pip install funasr torch torchaudio modelscope)${NC}"
                break
                ;;
            *)
                echo -e "  ${RED}请输入 y 或 n${NC}"
                ;;
        esac
    done
fi

# ── 7. 生成用户配置 ───────────────────────────────

echo ""
echo -e "${CYAN}═══ 用户配置 ═══${NC}"

mkdir -p ~/.config/aoide
if [ ! -f ~/.config/aoide/config.yaml ]; then
    cp "$SCRIPT_DIR/conf/config.yaml" ~/.config/aoide/config.yaml
    echo -e "  ${GREEN}✓${NC} 配置文件: ~/.config/aoide/config.yaml"
else
    echo -e "  ${YELLOW}○${NC} 配置文件已存在，跳过"
    echo "    如需重置: rm ~/.config/aoide/config.yaml && $0"
fi

# 复制 fcitx5 GUI 配置（仅在用户没有配置时提供默认，避免覆盖用户已调好的设置）
mkdir -p ~/.config/fcitx5/conf
if [ -f "$SCRIPT_DIR/src/engine/aoide.conf" ]; then
    if [ ! -f ~/.config/fcitx5/conf/aoide.conf ]; then
        cp "$SCRIPT_DIR/src/engine/aoide.conf" ~/.config/fcitx5/conf/aoide.conf
        echo -e "  ${GREEN}✓${NC} fcitx5 GUI 配置: ~/.config/fcitx5/conf/aoide.conf"
    else
        echo -e "  ${YELLOW}○${NC} fcitx5 GUI 配置已存在，跳过（如需重置: rm ~/.config/fcitx5/conf/aoide.conf && $0）"
    fi
fi

# ── 8. 安装并启动后端服务（systemd 用户服务，开机自启）────────

echo ""
echo -e "${CYAN}═══ 安装并启动后端服务 ═══${NC}"

# 安装 systemd 用户服务：开机自启 + 崩溃自动重启(Restart=on-failure)
mkdir -p ~/.config/systemd/user
sed -e "s|@VENV_DIR@|$VENV_DIR|g" \
    -e "s|@LOG_FILE@|$HOME/.config/aoide/backend.log|g" \
    "$SCRIPT_DIR/systemd/aoide-backend.service.in" \
    > ~/.config/systemd/user/aoide-backend.service
systemctl --user daemon-reload
systemctl --user enable aoide-backend 2>&1
systemctl --user restart aoide-backend

# 等几秒确认启动成功
sleep 3
if systemctl --user is-active --quiet aoide-backend; then
    systemctl --user disable --now yuhuang-backend.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/yuhuang-backend.service"
    systemctl --user daemon-reload
    for old in /usr/lib/fcitx5/yuhuang.so \
               /usr/share/fcitx5/addon/yuhuang.conf \
               /usr/share/fcitx5/config/yuhuang.conf \
               /usr/share/fcitx5/inputmethod/yuhuang.conf; do
        if [ -f "$old" ]; then sudo rm -f "$old"; fi
    done
    echo -e "  ${GREEN}✓${NC} 后端服务已启动 (systemd user service)"
    echo -e "  ${GREEN}✓${NC} 已设置开机自启: systemctl --user enable aoide-backend"
    echo -e "  ${GREEN}✓${NC} 日志: ~/.config/aoide/backend.log"
    echo -e "  ${GREEN}✓${NC} 管理: systemctl --user status/restart aoide-backend"
else
    echo -e "  ${YELLOW}⚠ 后端启动失败，请手动检查:${NC}"
    echo "     systemctl --user status aoide-backend"
    echo "     tail -50 ~/.config/aoide/backend.log"
fi

# ── 9. 刷新 fcitx5 ──────────────────────────────

echo ""
echo -e "${CYAN}═══ 刷新输入法 ═══${NC}"
if systemctl --user cat fcitx5.service &>/dev/null; then
    systemctl --user restart fcitx5.service && echo -e "  ${GREEN}✓${NC} fcitx5 已刷新，聆序已就绪" || \
        echo -e "  ${YELLOW}○${NC} fcitx5 用户服务未能启动，请检查 systemctl --user status fcitx5${NC}"
elif command -v fcitx5 &>/dev/null; then
    fcitx5 -r -d 2>/dev/null && echo -e "  ${GREEN}✓${NC} fcitx5 已刷新，聆序已就绪" || \
        echo -e "  ${YELLOW}○${NC} fcitx5 未运行，启动后生效${NC}"
fi

# ── 完成 ──────────────────────────────────────────

echo ""
echo "=========================================="
echo -e "${GREEN}  聆序（Aoide）安装完成！${NC}"
echo "=========================================="
echo ""

if [ "$NEED_REBOOT" = "yes" ]; then
    echo -e "${YELLOW}⚠ 需要重新登录使 fcitx5 环境变量生效${NC}"
    echo ""
fi

echo "快速使用:"
echo ""
echo "  1. 在任意输入框中使用现有输入法："
echo "     按住 Ctrl+Alt+Y 开始录音，松开后输入文本"
echo ""
echo "  2. 配置密钥、词典和输入选项:"
echo "     KDE 应用菜单 → 聆序设置"
echo "     输入选项在窗口中打开 KDE 输入法设置 → 附加组件 → 聆序 → 配置"
echo "     或编辑 ~/.config/aoide/config.yaml"
echo ""
echo "  3. 使用: 按住 Ctrl+Alt+Y → 说话 → 松开 → 文字上屏"
echo ""
echo "  📋 后端日志: tail -f ~/.config/aoide/backend.log"
echo "  🎤 查看麦克风: aoide-ctl mic"
echo "  🔄 重启后端: aoide-ctl restart"
echo "  ⏹  停止后端: aoide-ctl stop"
echo "  ⚙  fcitx5 配置: fcitx5-configtool → 附加组件 → 聆序 → 配置"
echo ""
