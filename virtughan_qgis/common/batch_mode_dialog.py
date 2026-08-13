from __future__ import annotations

from qgis.PyQt.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


def choose_multi_polygon_mode(parent, *, action_label: str, total_count: int, selected_count: int = 0):
    dialog = QDialog(parent)
    dialog.setWindowTitle("VirtuGhan")
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)

    scope_text = (
        f"{selected_count} selected polygon feature(s)"
        if selected_count
        else f"all {total_count} polygon feature(s) in the layer"
    )
    label = QLabel(
        f"The selected AOI layer contains multiple polygon features.\n\n"
        f"Choose how to {action_label}:\n\n"
        f"Combined AOI: use one bounding area for {scope_text}. This creates one output for the whole area.\n\n"
        f"Separate polygons: choose polygons in a preview dialog, then run one output per polygon feature."
    )
    label.setWordWrap(True)
    layout.addWidget(label)

    button_row = QHBoxLayout()
    button_row.addStretch(1)
    combined_button = QPushButton("Use Combined AOI", dialog)
    batch_button = QPushButton("Separate Polygons", dialog)
    cancel_button = QPushButton("Cancel", dialog)
    button_row.addWidget(combined_button)
    button_row.addWidget(batch_button)
    button_row.addWidget(cancel_button)
    layout.addLayout(button_row)

    result = {"mode": None}
    combined_button.clicked.connect(lambda: (result.update(mode="combined"), dialog.accept()))
    batch_button.clicked.connect(lambda: (result.update(mode="batch"), dialog.accept()))
    cancel_button.clicked.connect(dialog.reject)

    _exec = getattr(dialog, "exec", None) or getattr(dialog, "exec_")
    if not _exec():
        return None
    return result["mode"]
