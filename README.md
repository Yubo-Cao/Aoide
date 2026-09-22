# Aoide

Aoide 是 Linux 上的 fcitx5 语音输入附加组件。按住触发键说话，文字在光标附近预览；松键后识别终稿并输入到原输入框。它与拼音等输入法共存，无需切换输入法。

项目从 [Homio/YuHuang](https://github.com/Homio/YuHuang) 派生，保留原项目的 MIT 版权声明。Aoide 由 Yubo-Cao 独立维护。

## 功能

- **实时预览**：录音时显示语音草稿。X11 下可使用 Cairo/Pango 悬浮窗；其他环境回退到 fcitx5 候选栏。行宽、行数和字号可在图形设置中调整。
- **本地或云端识别**：本地使用 FunASR；可选 OpenAI 或 ElevenLabs 的批量、实时识别。云端失败时可回退本地。启用云端识别会将音频发送到所选服务。
- **可选 LLM 整理**：整理断句、标点和口语重复，尽量保留专有名词、数字及已有省略号。可通过个人词典指定术语；如果整理结果丢失内容或词典标准词，使用原识别文本。
- **焦点切换保护**：录音期间切换窗口会结束录音，并把结果提交到开始录音时的输入框。
- **KDE 配置入口**：应用菜单中的“Aoide 设置”打开输入法设置；进入“附加组件 → Aoide → 配置”调整触发键、麦克风、预览、LLM 和常用云端识别选项。

默认触发键为 **Pause**。按住说话，松开后等待终稿。整理模型只能依据识别文本判断停顿；原文没有停顿信息时，它无法准确补出省略号。

## 安装

目前安装脚本面向 Ubuntu 24.04 和 fcitx5，要求 Python 3.11+、麦克风以及构建 C++ 插件所需的开发包。脚本会安装缺失的 apt 依赖、创建 Python 虚拟环境、编译插件并启动用户服务。首次启动本地模型可能需要下载较大的模型文件。

```bash
./install.sh install
```

安装后在 KDE 应用菜单搜索 **Aoide 设置**，或运行 `fcitx5-configtool`，进入 **附加组件 → Aoide → 配置**。这里可设置录音键、预览窗、LLM 和云端识别。云端设置默认沿用 YAML；勾选 **Use cloud settings below** 后才用图形设置覆盖对应选项。

高级设置在 `~/.config/yuhuang/config.yaml`。例如可配置降噪、个人词典、识别模型和云端服务的超时。配置模板见 [conf/config.yaml](conf/config.yaml)。密钥可通过 `env:VARIABLE` 从服务环境读取。

```bash
aoide-ctl status
aoide-ctl mic
aoide-ctl restart
```

`aoide-backend` 可用于前台运行后端。`aoide-ctl` 与 `aoide-backend` 是新的命令入口；原有 `yuhuang-ctl` / `yuhuang-backend` 保留为兼容别名。升级现有安装时，插件标识、用户服务 `yuhuang-backend.service` 和 `~/.config/yuhuang/` 配置目录继续沿用，原有设置无需迁移。

## 工作流程

1. 在文本框中按住 Pause，预览框随录音更新。
2. 松键后，本地或所选云端识别器生成终稿。
3. 如果启用了 LLM 整理，后端根据识别文本与个人词典处理标点和术语。
4. 文字通过 fcitx5 输入到开始录音时的文本框。录音中途切换焦点会提前执行同一收尾流程。

默认在松键后一次性提交。可在后端配置中启用增量提交；稳定前缀会先上屏，尾部继续修正。

## 手动构建

```bash
cmake -S . -B build -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4
sudo cmake --install build
python3 -m venv .venv
.venv/bin/pip install -e .
```

安装脚本还会配置用户服务、默认配置和命令入口。手动安装需要自行完成这些步骤。

## 卸载

```bash
./install.sh uninstall
```

卸载会移除用户配置与该脚本记录的依赖；先备份需要保留的词典、密钥和 YAML 配置。

## 许可证与致谢

[MIT License](LICENSE)。感谢原项目 [Homio/YuHuang](https://github.com/Homio/YuHuang)，以及 [fcitx5](https://github.com/fcitx/fcitx5)、[FunASR](https://github.com/modelscope/FunASR) 和 [SenseVoice](https://github.com/FunAudioLLM/SenseVoice)。
