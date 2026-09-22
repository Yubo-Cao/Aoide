<div align="center">

<br/>
<br/>

# 语皇 (YuHuang)

**Linux 原生语音输入法 · 真正的输入法体验**

</div>

<div align="center">

<img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
<img src="https://img.shields.io/badge/platform-Linux-lightgrey" alt="Platform">
<img src="https://img.shields.io/badge/framework-fcitx5-orange" alt="fcitx5">
<img src="https://img.shields.io/badge/ASR-FunASR%20%2B%20SenseVoiceSmall-green" alt="FunASR">
<img src="https://img.shields.io/badge/release-v0.2.0-brightgreen" alt="Release">

</div>

<br/>

> **你还在用那种"录音→识别→复制→粘贴"的语音工具吗？试试真正的输入法。**

语皇 (YuHuang) 是一个基于 **fcitx5** 输入法框架的 Linux 语音输入法。它不是那种打开一个独立窗口、识别完了还得自己 Ctrl+C / Ctrl+V 的工具——它是**真正的输入法**。按住按键说话，悬浮草稿窗就在光标下方逐字浮现，松手即上屏，就像你用搜狗拼音打字一样自然。

---

##  为什么选择语皇？

市面上绝大多数语音输入工具都做成**独立 GUI 应用**——弹一个窗口让你说话，识别完后文字出现在那个与世隔绝的框里，然后你需要自己复制、切回目标窗口、粘贴。每用一次，就打断一次心流。那体验，就好像你用输入法打字，字先出现在一个独立的「输入法窗口」里，你再手动搬过去——这还叫输入法吗？

**语皇不一样。** 它跑在 fcitx5 输入法框架之内，是操作系统输入法列表里的一员。按住触发键说话时，文字直接以 preedit 候选框的形式出现在**当前光标位置**，松手自动 commit 上屏。全程不离开你的输入焦点，不需要复制、不需要粘贴、不需要切换窗口。

| 核心对比 |  语皇 (YuHuang) | 独立 GUI 语音工具 |
|---------|------------------|-------------------|
| **工作方式** | ✅ **真正的输入法**，注册在 fcitx5 中 | ❌ 独立窗口，与输入焦点割裂 |
| **文字落点** | ✅ **光标处直接浮现**，逐字候选 | ❌ 识别完在工具自己的框里 |
| **上屏流程** | ✅ 松键即自动 commit，无需额外操作 | ❌ 手动复制粘贴，打断心流 |
| **候选框** | ✅ fcitx5 原生 preedit，支持撤销/重输 | ❌ 无输入法级别的候选交互 |
| **隐私** | ✅ **本地识别**，数据不出设备 | ⚠️ 大多依赖云端服务 |
| **价格** | ✅ **完全开源免费** | ⚠️ 付费订阅或广告 |

---

##  核心特性

###  ️ 真正的原生输入法体验

语皇以 fcitx5 插件形式运行，是 Linux 输入法生态的原住民。你在系统输入法列表里就能看到它，就像看到「中文(拼音)」或「搜狗输入法」一样。触发键、麦克风设备、LLM 配置——全部可以在 fcitx5-configtool 的 GUI 中图形化配置，无需编辑配置文件。

###   精确、快速的本地语音识别

双模型流水线，兼顾实时性和准确率：

- **流式模型** `paraformer-zh-streaming`：边说边出，~300ms 延迟，实时预览文字
- **离线修正模型** `SenseVoiceSmall`：持续对全文进行离线校对，精度远超流式模型

核心创新在于 **LCP（最长公共前缀）增量提交机制**：离线模型每次修正后，与上一轮结果对比 LCP，只有连续两次都稳定不变的前缀才会被逐段提交上屏。既保证了快速反馈，又绝不会把流式模型的草稿错误锁死。

###   三区悬浮草稿窗

自绘的暗色圆角悬浮窗跟随光标，草稿按可信度分成三区，用半透明底色标出区界——字色统一，整句话读起来仍是完整的一句话：

- **绿区**：已稳定、即将上屏的文字
- **黄区**：离线修正模型射程内、还可能微调的文字
- **红区**：流式模型的最新草稿，随时会被重写

窗口用 Cairo + Pango 自绘（X11），鼠标点击直接穿透到下层应用、不抢焦点；折行由引擎按显示列宽自算，行宽、行数、字号均可在 GUI 中配置。非 X11 会话或编译时缺 cairo/pango 会自动回退到 fcitx5 候选栏渲染。

###   LLM 智能润色

可选的 LLM 后端（支持任何 OpenAI 兼容 API，如 vLLM / Ollama / 通义千问等），在语音识别完成后自动优化文本：

- 修正中英混读识别误差（如 "泛 AR" → "FunASR"）
- 去除口语填充词和重复
- 智能断句和标点

整理模型由 `llm.base_url` 决定：OpenAI 兼容地址（如 `https://api.openai.com/v1` 配 `gpt-6-luna` 或 `gpt-4.1`），或 Amazon Bedrock（`bedrock:us-east-1` 配 `global.anthropic.claude-haiku-4-5-20251001-v1:0`，凭据取自 `llm.aws_profile` 指定的 AWS profile，需要 `pip install -e '.[bedrock]'`）。推理模型可用 `llm.reasoning_effort: none` 保持低延迟。整理结果若删字、丢英文或改动数字，会保留识别原文。

###   云端识别（可选）

`cloud_asr.enabled: true` 后，松键时把原始音频（非降噪副本）发送到所选服务识别，失败自动回退本地模型，音频不落盘：

| `cloud_asr.provider` | 最终结果 |
|---|---|
| `openai` | 松键后按 VAD 分段并行调用 GPT Transcribe |
| `openai-realtime` | OpenAI Realtime 流式会话（`gpt-live-transcribe`）的最终结果，松键后约 0.6 秒 |
| `elevenlabs` | ElevenLabs Scribe v2 分段识别 |
| `elevenlabs-realtime` | Scribe v2 Realtime 流式会话的最终结果，松键后约 0.2 秒 |

`cloud_asr.draft: cloud` 让按住期间的草稿来自该厂商的流式识别（替代本地 FunASR 草稿）；流式连接中途失败会立即切回本地草稿，不丢失本次录音。流式会话不会自动重连，连续失败后暂停一段时间，鉴权错误后停止到重启。个人词典词条默认作为 OpenAI 识别提示；`dictionary_keywords: true` 会把词条作为强关键词偏置（实测会在噪声中把相近发音误识为词典词，默认关闭）。密钥写成 `env:YUHUANG_OPENAI_API_KEY` / `env:YUHUANG_ELEVENLABS_API_KEY`，由服务环境注入。全部选项见 `conf/config.yaml`。

###  ⚡ Push-to-Talk，像对讲机一样简单

- 按住触发键（默认**右 Ctrl**）→ 开始说话，候选框实时显示
- 松开触发键 → 离线修正 + 自动上屏
- 全程不碰鼠标，不切换窗口

###  中文优化，本地运行

- FunASR + SenseVoiceSmall 双模型在本地 GPU/CPU 运行
- 专为中文语音场景调优
- 支持最长数分钟的连续语音输入
- 敏感语音数据从不离开你的设备

---

##  工作原理

```
按住触发键 (如右 Ctrl)
    │
    ├─→ 麦克风采集音频 (16kHz)
    │       │
    │       ├─→ 流式模型 (paraformer-zh-streaming)
    │       │       每 ~300ms 更新悬浮窗草稿（红区）
    │       │
    │       └─→ 离线修正模型 (SenseVoiceSmall)
    │               每新增 25 字或 2.5 秒触发一次
    │               │
    │               ├─→ LCP 稳定性判定
    │               │      连续两次 LCP 超过已提交边界 → commit 到应用
    │               │
    │               └─→ 悬浮窗黄区（未稳定的尾巴）
    │
    ▼  松开触发键
SenseVoiceSmall 最终识别 → 提交剩余全部文字
    │
    ▼  可选
LLM 润色 → 修正外来词、优化标点
    │
    ▼
文字上屏到光标位置 ✓
```

---

##  系统要求

- Ubuntu 24.04（GNOME / KDE 桌面）
- fcitx5 ≥ 5.1.0
- Python ≥ 3.11
- NVIDIA GPU（推荐，4GB+ 显存）或 CPU 回退
- 可用的麦克风

---

##  快速安装

```bash
# 一键安装（含编译 fcitx5 插件 + 创建 Python venv）
./install.sh install

# 一键卸载
./install.sh uninstall
```

### 手动安装

<details>
<summary>展开详细步骤</summary>

**1. 安装系统依赖**

```bash
sudo apt install fcitx5 fcitx5-chinese-addons fcitx5-frontend-gtk4 \
    fcitx5-frontend-qt5 fcitx5-config-qt cmake extra-cmake-modules \
    pkg-config libfcitx5core-dev libfcitx5config-dev gettext \
    libcairo2-dev libpango1.0-dev libxext-dev \
    python3-pip python3-venv portaudio19-dev
```

**2. 编译 fcitx5 插件**

```bash
mkdir build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
sudo make install
```

**3. 安装 Python 后端**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install sounddevice httpx pyyaml funasr torch torchaudio modelscope
pip install -e .
```

**4. 配置并启动**

```bash
mkdir -p ~/.config/yuhuang
cp conf/config.yaml ~/.config/yuhuang/

# 启动后端（首次会下载模型，约 1-2 分钟）
yuhuang-backend

# 或后台运行
yuhuang-ctl start

# 切换到语皇输入法
yuhuang-ctl switch
```

</details>

---

##  使用方法

### 基本流程

1. 切换到语皇输入法（`yuhuang-ctl switch`）
2. 把光标放到任意输入框
3. **按住右 Ctrl** 开始说话，候选框实时显示文字
4. **松开右 Ctrl** 自动上屏

### 快捷键

| 按键 | 功能 |
|------|------|
| `触发键（按住）` | Push-to-Talk 语音输入 |
| `Esc` | 取消当前候选文字 |
| `Enter` | 手动上屏当前候选 |
| `F5` | 强制触发 LLM 润色 |
| `F6` | 切换监听模式 |

### CLI 工具

```bash
yuhuang-ctl status     # 查看服务状态
yuhuang-ctl start      # 启动后端（含模型加载等待）
yuhuang-ctl stop       # 停止后端
yuhuang-ctl restart    # 重启（含日志轮转）
yuhuang-ctl mic        # 列出/设置麦克风设备
yuhuang-ctl switch     # 切换到语皇输入法
yuhuang-ctl gpu        # 检测 GPU 并配置 CUDA 设备
```

### GUI 配置

在 fcitx5-configtool 中可图形化编辑所有配置：

- **系统设置 → 输入法 → 附加组件 → YuHuang → 配置**
- 可配置项：触发键、麦克风设备、LLM API 地址/密钥/模型、温度参数等

---

##  项目结构

```
YuHuang/
├── CMakeLists.txt                  # 顶层 CMake（C++ 插件）
├── pyproject.toml                  # Python 包配置
├── install.sh                      # 一键安装/卸载脚本
├── conf/
│   └── config.yaml                 # 后端默认配置模板
├── src/
│   ├── engine/                     # fcitx5 C++ 插件
│   │   ├── CMakeLists.txt
│   │   ├── yuhuang_engine.h/cpp    # 引擎主类（含 PTT 按键逻辑）
│   │   ├── yuhuang_state.h         # InputContext 状态（草稿更新/commit）
│   │   ├── yuhuang_window.h/cpp    # 自绘悬浮草稿窗（X11 + Cairo + Pango）
│   │   ├── yuhuang_panel.h         # 三区折行 + 候选栏回退渲染
│   │   ├── x11_keycheck.h/cpp      # PTT 物理键盘看门狗（XI2 raw 事件）
│   │   ├── yuhuang_socket.h        # Unix Socket 客户端（IPC 通信）
│   │   ├── yuhuang-addon.conf.in   # fcitx5 插件注册
│   │   ├── yuhuang-inputmethod.conf # 输入法注册
│   │   └── yuhuang.conf            # fcitx5 GUI 配置项描述
│   ├── backend/                    # Python 后端服务
│   │   ├── main.py                 # 服务入口（PTT 流水线编排、LCP 提交逻辑）
│   │   ├── asr_engine.py           # ASR 引擎（双模型 + 离线修正轮询）
│   │   ├── llm_optimizer.py        # LLM 文本润色（OpenAI 兼容 / Bedrock）
│   │   ├── cloud_asr.py            # 云端识别提供方选择与回退链（OpenAI / ElevenLabs 批量）
│   │   ├── cloud_stream.py         # 流式识别会话（OpenAI Realtime / Scribe Realtime）
│   │   ├── speech_frontend.py      # WebRTC 降噪 + Silero VAD 分段
│   │   ├── personal_dictionary.py  # 个人词典
│   │   ├── audio_capture.py        # 音频采集（PyAudio）
│   │   └── unix_server.py          # Unix Domain Socket 服务端
│   └── tools/
│       └── yuhuang_ctl.py          # CLI 控制工具
└── README.md
```

---

##  常见问题

### Q: 编译报错找不到 Fcitx5Config？

```bash
sudo apt install libfcitx5config-dev
```

### Q: fcitx5 中看不到语皇输入法？

```bash
# 确认插件文件已安装
ls /usr/lib/fcitx5/yuhuang.so
ls /usr/share/fcitx5/addon/yuhuang.conf

# 重启 fcitx5
fcitx5 -r

# 在 fcitx5-configtool 中添加
fcitx5-configtool → 输入法 → 添加 → YuHuang
```

### Q: 触发键与系统快捷键冲突？

在 fcitx5-configtool 的语皇配置页面中修改触发键。启动时也会自动检测已知的 GNOME/KDE 快捷键冲突并警告。

### Q: 模型下载慢或失败？

首次启动会自动从 ModelScope 下载模型文件（约 2GB）。如果下载慢，可设置代理：

```bash
export HTTP_PROXY=http://your-proxy:port
```

### Q: 音频采集失败？

```bash
yuhuang-ctl mic        # 查看可用麦克风设备
yuhuang-ctl mic "关键词" # 模糊匹配并设置设备
```

---

##  技术栈

- **输入法框架**: fcitx5 (C++ 插件)
- **草稿窗渲染**: X11 + Cairo + Pango（自绘悬浮窗，候选栏回退）
- **IPC 通信**: Unix Domain Socket
- **语音识别**: FunASR (paraformer-zh-streaming + SenseVoiceSmall)
- **VAD**: FSMN-VAD
- **LLM 集成**: OpenAI 兼容 API（vLLM / Ollama / 通义千问 等）
- **音频采集**: PyAudio / PortAudio

---

##  致谢

本项目离不开以下优秀开源项目的支持：

- [fcitx5](https://github.com/fcitx/fcitx5) — Linux 通用输入法框架
- [FunASR](https://github.com/modelscope/FunASR) — 阿里巴巴开源的工业级语音识别工具包
- [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) — 阿里通义实验室多语言语音理解模型

---

##  许可证

[MIT License](LICENSE)
