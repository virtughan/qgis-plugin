from __future__ import annotations

from qgis.PyQt.QtCore import QSize, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..qt_compat import QSizePolicyCompat


PRIMARY_BUTTON_STYLE = """
QPushButton {
    background-color: #1976d2;
    color: white;
    border: 1px solid #145ea8;
    border-radius: 4px;
    padding: 6px 14px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #1e88e5;
}
QPushButton:pressed {
    background-color: #0d47a1;
}
QPushButton:disabled {
    background-color: #9e9e9e;
    border-color: #8a8a8a;
    color: #eeeeee;
}
"""


def apply_primary_button_style(button: QPushButton | None):
    if button is None:
        return
    button.setMinimumHeight(max(32, button.minimumHeight()))
    button.setDefault(True)
    button.setStyleSheet(PRIMARY_BUTTON_STYLE)


def hide_single_tab_bar(root: QWidget | None, tab_name: str = "tabWidget"):
    if root is None:
        return
    tab_widget = root.findChild(QTabWidget, tab_name)
    if tab_widget is None or tab_widget.count() != 1:
        return
    try:
        tab_widget.tabBar().hide()
        tab_widget.setContentsMargins(0, 0, 0, 0)
    except Exception:  # nosec B110 - defensive QGIS cleanup or optional API fallback.
        pass


class DynamicBandSelector(QWidget):
    """Compact plus/dropdown selector for one or more backend band names."""

    changed = pyqtSignal()

    def __init__(self, parent=None, *, add_text: str = "+"):
        super().__init__(parent)
        self._row_height = 28
        self._footer_height = 24
        self._spacing = 4
        self._bands: list[str] = []
        self._rows: list[tuple[QWidget, QComboBox, QToolButton]] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(self._spacing)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch()
        self.addButton = QToolButton(self)
        self.addButton.setText(add_text)
        self.addButton.setToolTip("Add band")
        self.addButton.setFixedSize(24, 24)
        self.addButton.clicked.connect(lambda: self.add_band())
        footer.addWidget(self.addButton)
        self._layout.addLayout(footer)
        self._refresh_minimum_height()

    def _preferred_height(self) -> int:
        row_count = max(1, len(self._rows))
        return (
            row_count * self._row_height
            + self._footer_height
            + row_count * self._spacing
        )

    def _refresh_minimum_height(self):
        self.setMinimumHeight(self._preferred_height())
        self.updateGeometry()

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(max(hint.width(), 160), max(hint.height(), self._preferred_height()))

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(max(hint.width(), 220), max(hint.height(), self._preferred_height()))

    def set_bands(self, bands, selected=None, *, min_rows: int = 1):
        self._bands = [str(b).strip() for b in (bands or []) if str(b).strip()]
        selected_values = [str(b).strip() for b in (selected or []) if str(b).strip()]
        if not selected_values:
            selected_values = self._bands[:max(1, int(min_rows or 1))]
        while self._rows:
            self._remove_row(0, emit=False)
        for value in selected_values:
            self.add_band(value, emit=False)
        while len(self._rows) < max(1, int(min_rows or 1)):
            self.add_band(emit=False)
        self._refresh_minimum_height()
        self.changed.emit()

    def add_band(self, value: str | None = None, *, emit: bool = True):
        row = QWidget(self)
        row.setMinimumHeight(28)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        combo = QComboBox(row)
        combo.setSizePolicy(QSizePolicyCompat.Expanding, QSizePolicyCompat.Fixed)
        combo.setMinimumHeight(24)
        combo.addItems(self._bands)
        if value:
            combo.setCurrentText(value)
        combo.currentTextChanged.connect(lambda *_: self.changed.emit())
        layout.addWidget(combo)

        remove_btn = QToolButton(row)
        remove_btn.setText("x")
        remove_btn.setToolTip("Remove band")
        remove_btn.setFixedSize(24, 24)
        layout.addWidget(remove_btn)

        insert_at = max(0, self._layout.count() - 1)
        self._layout.insertWidget(insert_at, row)
        self._rows.append((row, combo, remove_btn))
        remove_btn.clicked.connect(lambda *_: self._remove_row_by_widget(row))
        self._refresh_remove_buttons()
        row.updateGeometry()
        self._refresh_minimum_height()
        try:
            self.parentWidget().updateGeometry()
        except Exception:  # nosec B110 - defensive QGIS cleanup or optional API fallback.
            pass
        if emit:
            self.changed.emit()

    def selected_bands(self) -> list[str]:
        values: list[str] = []
        for _row, combo, _remove in self._rows:
            value = combo.currentText().strip()
            if value and value not in values:
                values.append(value)
        return values

    def _remove_row_by_widget(self, row: QWidget):
        for idx, (candidate, _combo, _remove) in enumerate(list(self._rows)):
            if candidate is row:
                self._remove_row(idx, emit=True)
                break

    def _remove_row(self, idx: int, *, emit: bool):
        if len(self._rows) <= 1 and emit:
            return
        row, _combo, _remove = self._rows.pop(idx)
        self._layout.removeWidget(row)
        row.deleteLater()
        self._refresh_remove_buttons()
        self._refresh_minimum_height()
        try:
            self.parentWidget().updateGeometry()
        except Exception:  # nosec B110 - defensive QGIS cleanup or optional API fallback.
            pass
        if emit:
            self.changed.emit()

    def _refresh_remove_buttons(self):
        can_remove = len(self._rows) > 1
        for _row, _combo, remove_btn in self._rows:
            remove_btn.setEnabled(can_remove)


def describe_bands_for_formula(formula: str, bands: list[str]) -> str:
    text = (formula or "").strip()
    if not text:
        return "(formula will display here)"
    return f"{text}, bands={', '.join(bands) if bands else '-'}"
