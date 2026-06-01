"""Global application stylesheet.

Light surface, rounded corners, blue accent. The Rail keeps its own
dark palette via its inline stylesheet — that takes precedence.
"""
from __future__ import annotations


APP_STYLESHEET = """
QMainWindow, QWidget#root {
    background-color: #f8fafc;
}

QFrame#card {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
}

QLabel {
    color: #1f2937;
}

QPushButton {
    background-color: #ffffff;
    color: #1f2937;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 6px 14px;
}
QPushButton:hover {
    background-color: #f3f4f6;
    border-color: #9ca3af;
}
QPushButton:pressed {
    background-color: #e5e7eb;
}
QPushButton:disabled {
    color: #9ca3af;
    background-color: #f9fafb;
    border-color: #e5e7eb;
}
QPushButton:default {
    background-color: #3b82f6;
    color: #ffffff;
    border-color: #2563eb;
}
QPushButton:default:hover {
    background-color: #2563eb;
}

QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {
    background-color: #ffffff;
    color: #1f2937;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: #bfdbfe;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border-color: #3b82f6;
}
QLineEdit:read-only {
    background-color: #f9fafb;
    color: #6b7280;
}

QComboBox::drop-down {
    border: none;
    width: 18px;
}

QCheckBox {
    color: #1f2937;
    spacing: 6px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    background-color: #ffffff;
}
QCheckBox::indicator:checked {
    background-color: #3b82f6;
    border-color: #2563eb;
}

QTabWidget::pane {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    top: -1px;
}
QTabBar::tab {
    background-color: transparent;
    color: #6b7280;
    padding: 8px 16px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:selected {
    color: #2563eb;
    border-bottom: 2px solid #3b82f6;
}
QTabBar::tab:hover:!selected {
    color: #1f2937;
}

QListWidget {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    outline: 0;
}
QListWidget::item {
    padding: 4px 8px;
    border-bottom: 1px solid #f3f4f6;
}
QListWidget::item:selected {
    background-color: #eff6ff;
    color: #1f2937;
}

QTreeView {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    outline: 0;
}
QTreeView::item:selected {
    background-color: #eff6ff;
    color: #1f2937;
}

QHeaderView::section {
    background-color: #f3f4f6;
    color: #4b5563;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #e5e7eb;
    border-bottom: 1px solid #e5e7eb;
}

QSplitter::handle {
    background-color: #e5e7eb;
}
QSplitter::handle:horizontal {
    width: 1px;
}
QSplitter::handle:vertical {
    height: 1px;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
}
QScrollBar::handle:vertical {
    background-color: #d1d5db;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #9ca3af;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
}
QScrollBar::handle:horizontal {
    background-color: #d1d5db;
    border-radius: 4px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #9ca3af;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
"""
