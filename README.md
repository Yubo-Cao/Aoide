# 聆序 · Aoide

**按住说话，松手成文。**

聆序是 Linux 上的 fcitx5 语音输入附加组件。它与拼音等输入法共存：在支持 fcitx5 的文本框按住快捷键说话，松开后将识别结果输入到原输入框。录音时可预览草稿；预览位置由应用提供的光标坐标和 fcitx5 面板决定。

**聆序**是中文显示名，**Aoide** 是英文名，也是仓库、命令和配置路径使用的名称。项目由 Yubo-Cao 独立维护，基于 [Homio/YuHuang](https://github.com/Homio/YuHuang) 的 MIT 授权代码继续开发。

## 快速上手

目前 `install.sh` 面向 **Ubuntu 24.04 + fcitx5**，需要 Python 3.11+、麦克风和编译 C++ 插件所需的开发包。脚本会安装缺失的 apt 依赖、建立 Python 虚拟环境、安装插件并启动用户服务。首次使用本地识别时，模型可能需要下载。

```bash
git clone https://github.com/Yubo-Cao/Aoide.git
cd Aoide
./install.sh install
```

安装后：

1. 在文本框中**按住 Ctrl+Alt+Y** 说话，松开后等待文字上屏。这个快捷键可在 KDE 输入法设置中修改。
2. 在 KDE 应用菜单打开 **聆序设置**，管理 API 密钥和个人词典。
3. 如需调整快捷键、麦克风、预览、云端识别或大模型整理，点击窗口中的 **打开 KDE 输入法设置**，再进入 **附加组件 → 聆序 → 配置**。

设置窗口提供简体中文和英文界面，按系统语言选择；也可用 `AOIDE_UI_LANG=zh` 或 `AOIDE_UI_LANG=en` 指定语言。fcitx5 的聆序配置项同时显示中英文名称。

## 能做什么

- **实时预览**：录音时显示识别草稿。Wayland 使用 fcitx5 候选面板；X11 可使用 Cairo/Pango 悬浮窗。预览行宽、行数和字号可以调整。
- **本地与云端识别**：本地使用 FunASR；可选 OpenAI 或 ElevenLabs 的批量或实时识别。云端识别失败时可配置回退到本地。
- **可选的大模型整理**：去掉口头禅和重复，理顺句间逻辑；确有并列事项或步骤时可整理为 Markdown 列表。个人词典可固定专有名词和别名。数字、既有省略号或词典标准词被错误改动时，原识别文本会作为回退。
- **焦点切换保护**：说话期间切换窗口会结束本次录音，并尝试把结果提交到开始录音时的输入框。

默认在松键后一次性提交。高级配置也支持增量提交，让稳定的前缀先上屏。模型只能依据识别文本处理停顿；原文没有停顿信息时，无法准确补出省略号。

## 配置与数据

| 位置 | 用途 |
| --- | --- |
| **聆序设置**（英文界面：**Aoide Settings**） | 将 OpenAI、ElevenLabs 和整理模型的 API 密钥存入桌面密码库（Secret Service）；查看、编辑个人词典。 |
| **KDE 输入法设置 → 附加组件 → 聆序** | 设置触发键、预览、识别服务和大模型。云端设置只有勾选 **覆盖 YAML 云端配置** 后才覆盖后端 YAML。 |
| `~/.config/aoide/config.yaml` | 设置本地模型、降噪、超时等高级参数；模板见 [conf/config.yaml](conf/config.yaml)。 |
| `~/.config/aoide/dictionary.yaml` | 保存词典中的标准写法和别名；下次录音时自动读取。 |

KDE 设置中的“识别模型与模式”按默认模型显示四个选项：OpenAI 的 **GPT-Transcribe**（批量）和 **GPT-Live-Transcribe**（实时），以及 ElevenLabs 的 **Scribe v2**（批量）和 **Scribe v2 Realtime**（实时）。这里选择服务商及处理方式；“批量识别模型”和“实时识别模型”字段可以覆盖具体模型 ID。原有配置值继续有效。

本地识别在本机运行。启用云端识别后，音频会发给所选服务；启用远程大模型整理后，识别文本及用于衔接的上下文会发给所配置的模型服务。密钥可通过设置窗口存入密码库，旧的 `env:VARIABLE` 配置仍可兼容。

后端 socket 默认位于 `$XDG_RUNTIME_DIR/aoide/backend.sock`。从旧版升级时，安装脚本会迁移用户配置和词典；旧配置里的 `/tmp/yuhuang-backend.sock` 会映射到新地址。命令入口和用户服务分别为 `aoide-ctl`、`aoide-backend` 和 `aoide-backend.service`。

```bash
aoide-ctl status   # 检查后端和插件
aoide-ctl mic      # 查看麦克风
aoide-ctl restart  # 重启后端
```

## 手动构建

其他发行版需要自行安装 fcitx5 开发库、CMake、音频库与 Python 依赖，并配置用户服务。插件和 Python 包可分别构建：

```bash
cmake -S . -B build -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4
sudo cmake --install build
python3 -m venv .venv
.venv/bin/pip install -e '.[gui]'
```

手动构建还需自行安装 `systemd/aoide-backend.service.in` 对应的用户服务、创建配置文件，并将命令入口加入 `PATH`；上面的命令本身不会完成这些步骤。

## 卸载

```bash
./install.sh uninstall
```

卸载脚本会移除用户配置及其记录的依赖。需要保留词典或 YAML 配置时，请先备份；桌面密码库中的密钥不会随配置目录删除，可在卸载前从聆序设置窗口移除。

## 许可证与致谢

本项目采用 [MIT License](LICENSE)，保留原项目的版权声明。感谢 [Homio/YuHuang](https://github.com/Homio/YuHuang)、[fcitx5](https://github.com/fcitx/fcitx5)、[FunASR](https://github.com/modelscope/FunASR) 和 [SenseVoice](https://github.com/FunAudioLLM/SenseVoice)。
