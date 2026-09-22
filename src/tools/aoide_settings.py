"""Native Aoide settings for credentials and personal vocabulary."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from backend import secret_store
from backend.socket_path import resolve_socket_path


DICTIONARY = Path.home() / ".config/aoide/dictionary.yaml"


class SettingsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Aoide 设置")
        self.resize(720, 520)
        self._dictionary_stamp = None
        self._dictionary_data = {}

        tabs = QTabWidget()
        tabs.addTab(self._keys_tab(), "API 密钥")
        tabs.addTab(self._dictionary_tab(), "个人词典")
        tabs.addTab(self._input_tab(), "输入与预览")
        self.setCentralWidget(tabs)
        self._load_dictionary()

    def _keys_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)
        intro = QLabel(
            "密钥保存在桌面密码库（Secret Service）中。留空不会改动已保存的密钥。"
            "保存后，下次请求会读取新密钥；如果此前鉴权失败，请重启后端。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self._key_statuses = {}
        for service, label in (
            ("openai", "OpenAI 语音识别"),
            ("elevenlabs", "ElevenLabs 语音识别"),
            ("llm", "LLM 文本整理"),
        ):
            status = QLabel()
            self._key_statuses[service] = (status, label)
            self._refresh_key_status(service)
            layout.addWidget(status)
            row = QHBoxLayout()
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setPlaceholderText("输入新密钥")
            field.setAccessibleName(label + " API 密钥")
            row.addWidget(field, 1)
            save = QPushButton("保存")
            save.clicked.connect(lambda _=False, s=service, f=field: self._save_key(s, f))
            row.addWidget(save)
            remove = QPushButton("删除")
            remove.clicked.connect(lambda _=False, s=service: self._remove_key(s))
            row.addWidget(remove)
            layout.addLayout(row)
        restart = QPushButton("重启 Aoide 后端")
        restart.clicked.connect(self._restart_backend)
        layout.addWidget(restart)
        layout.addStretch(1)
        return page

    def _refresh_key_status(self, service):
        label, title = self._key_statuses[service]
        label.setText(title + ("  ·  已保存" if secret_store.present(service) else "  ·  未保存"))

    def _restart_backend(self):
        try:
            subprocess.run(["systemctl", "--user", "restart", "aoide-backend"],
                           check=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            QMessageBox.warning(self, "无法重启", "请运行 aoide-ctl restart，然后重试语音输入。")
            return
        self.statusBar().showMessage("后端正在重新加载识别模型。", 7000)

    def _save_key(self, service, field):
        value = field.text().strip()
        if not value:
            QMessageBox.information(self, "未保存", "请先输入 API 密钥。")
            return
        try:
            secret_store.store(service, value)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            QMessageBox.warning(self, "无法保存密钥", f"密码库未能保存密钥。请解锁密码库后重试。\n{type(exc).__name__}")
            return
        field.clear()
        self._refresh_key_status(service)
        self.statusBar().showMessage("密钥已保存到桌面密码库。", 5000)

    def _remove_key(self, service):
        if QMessageBox.question(self, "删除密钥", "从桌面密码库删除此密钥？") != QMessageBox.StandardButton.Yes:
            return
        try:
            secret_store.clear(service)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            QMessageBox.warning(self, "无法删除密钥", f"密码库未能删除密钥。\n{type(exc).__name__}")
            return
        self.statusBar().showMessage("密钥已从桌面密码库删除。", 5000)
        self._refresh_key_status(service)

    def _dictionary_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(
            "标准写法会作为识别提示；别名会在整理时替换为标准写法。"
            "多个别名用逗号或换行分隔。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索关键词或别名")
        self.search.textChanged.connect(self._filter_dictionary)
        layout.addWidget(self.search)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["标准写法", "别名"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 220)
        self.table.setAlternatingRowColors(True)
        self.empty_label = QLabel("词典还是空的。点击“添加词条”写入常用名称。")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("添加词条")
        add.clicked.connect(lambda _checked=False: self._add_term())
        row.addWidget(add)
        remove = QPushButton("删除选中")
        remove.clicked.connect(self._remove_term)
        row.addWidget(remove)
        row.addStretch(1)
        save = QPushButton("保存词典")
        save.setDefault(True)
        save.clicked.connect(self._save_dictionary)
        row.addWidget(save)
        layout.addLayout(row)
        return page

    def _input_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(
            "触发键、麦克风、预览窗尺寸、识别提供方和整理模型由 fcitx5 管理。"
            "点击下方按钮，再进入“附加组件 → Aoide → 配置”。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        button = QPushButton("打开 KDE 输入法设置")
        button.clicked.connect(self._open_fcitx_settings)
        layout.addWidget(button)
        try:
            config = yaml.safe_load((Path.home() / ".config/aoide/config.yaml").read_text()) or {}
            configured_socket = config.get("backend", {}).get("socket_path")
        except (OSError, AttributeError, yaml.YAMLError):
            configured_socket = None
        socket_label = QLabel("本机 socket：" + resolve_socket_path(configured_socket))
        socket_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(socket_label)
        layout.addStretch(1)
        return page

    def _open_fcitx_settings(self):
        try:
            subprocess.Popen(["systemsettings", "kcm_fcitx5"], start_new_session=True)
        except OSError:
            QMessageBox.warning(self, "无法打开设置", "请安装 KDE 系统设置，或运行 fcitx5-configtool。")

    def _load_dictionary(self):
        try:
            if DICTIONARY.exists() and DICTIONARY.stat().st_size > 65536:
                raise ValueError("词典超过 64 KiB，请先备份并精简文件。")
            data = yaml.safe_load(DICTIONARY.read_text()) if DICTIONARY.exists() else {}
            if data is None:
                data = {}
            if not isinstance(data, dict) or not isinstance(data.get("terms", []), list):
                raise ValueError("词典需要包含 terms 列表。")
            self._dictionary_data = data
            self._dictionary_stamp = DICTIONARY.stat().st_mtime_ns if DICTIONARY.exists() else None
            self.table.setRowCount(0)
            for entry in data.get("terms", []):
                item = {"term": entry, "aliases": []} if isinstance(entry, str) else entry
                if not isinstance(item, dict):
                    continue
                self._add_term(item.get("term", ""), ", ".join(item.get("aliases") or []))
            self._update_empty_state()
        except (OSError, ValueError, yaml.YAMLError) as exc:
            QMessageBox.warning(self, "无法读取词典", str(exc))

    def _add_term(self, term="", aliases=""):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(term)))
        self.table.setItem(row, 1, QTableWidgetItem(str(aliases)))
        self.table.scrollToBottom()
        self._update_empty_state()
        if not term:
            self.table.setCurrentCell(row, 0)
            self.table.editItem(self.table.item(row, 0))

    def _remove_term(self):
        for row in sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)
        self._update_empty_state()

    def _update_empty_state(self):
        self.empty_label.setVisible(self.table.rowCount() == 0)

    def _filter_dictionary(self, query):
        query = query.casefold().strip()
        for row in range(self.table.rowCount()):
            text = " ".join(self.table.item(row, col).text() for col in (0, 1))
            self.table.setRowHidden(row, query not in text.casefold())

    def _save_dictionary(self):
        if self.table.rowCount() > 200:
            QMessageBox.warning(self, "词条过多", "最多保存 200 条词条。")
            return
        entries = []
        for row in range(self.table.rowCount()):
            term = self.table.item(row, 0).text().strip()
            aliases = [a.strip() for a in self.table.item(row, 1).text().replace("\n", ",").split(",") if a.strip()]
            if not term or len(term) > 100 or len(aliases) > 20 or any(len(a) > 100 for a in aliases):
                QMessageBox.warning(self, "词条无效", f"第 {row + 1} 行需要 1–100 字的标准写法，最多 20 个别名。")
                return
            entries.append({"term": term, "aliases": aliases})
        stamp = DICTIONARY.stat().st_mtime_ns if DICTIONARY.exists() else None
        if stamp != self._dictionary_stamp:
            QMessageBox.warning(self, "词典已变更", "词典已被其他程序修改。请重新打开设置后再保存。")
            return
        data = dict(self._dictionary_data)
        data["terms"] = entries
        content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        if len(content.encode()) > 65536:
            QMessageBox.warning(self, "词典过大", "词典最多 64 KiB。")
            return
        DICTIONARY.parent.mkdir(parents=True, exist_ok=True)
        path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=DICTIONARY.parent,
                                             prefix=".dictionary-", delete=False) as out:
                path = Path(out.name)
                os.fchmod(out.fileno(), 0o600)
                out.write(content)
            os.replace(path, DICTIONARY)
            self._dictionary_stamp = DICTIONARY.stat().st_mtime_ns
            self._dictionary_data = data
            self.statusBar().showMessage("词典已保存，下次录音时生效。", 5000)
        except OSError as exc:
            QMessageBox.warning(self, "无法保存词典", str(exc))
        finally:
            if path and path.exists():
                path.unlink()


def main():
    app = QApplication(sys.argv)
    window = SettingsWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
