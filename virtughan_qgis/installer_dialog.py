"""First-time dependency installer dialog with real-time progress."""

from __future__ import annotations

import threading
from typing import Callable

from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QObject, QTimer
from qgis.PyQt.QtGui import QFont


class InstallProgress(QObject):
    """Signal emitter for installation progress."""

    log_message = pyqtSignal(str)
    install_complete = pyqtSignal(bool, str)  # success, final_message


class FirstTimeInstallerDialog(QDialog):
    """Dialog for first-time dependency installation with real-time progress."""

    def __init__(
        self,
        parent=None,
        install_callback: Callable[..., bool] | None = None,
    ):
        """
        Args:
            parent: Parent widget
            install_callback: Async function that performs installation.
                            Should call progress_callback(log_line) for each line.
        """
        super().__init__(parent)
        self.install_callback = install_callback
        self.progress_callback = None
        self._install_thread = None
        self._success = False
        self._network_error_detected = False
        self._can_retry = False

        self.setWindowTitle("VirtuGhan • First-Time Setup")
        self.setMinimumSize(700, 500)
        self.setModal(True)

        # Prevent closing while installing
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowCloseButtonHint)

        self._init_ui()
        self._setup_signals()

    def _init_ui(self):
        """Initialize UI components."""
        layout = QVBoxLayout()

        # Title
        title = QLabel("Installing Dependencies")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        # Description
        description = QLabel(
            "Please wait while required dependencies are downloaded and installed.\n"
            "This can take 5-10 minutes depending on your internet speed.\n"
            "Please do not close this window. It will close automatically when setup is complete.\n"
            "This is a one-time installation for this QGIS and you will no longer need to wait long to open it after successfull installation."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        # Progress bar (indeterminate)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(0)  # Indeterminate mode
        layout.addWidget(self.progress_bar)

        # Log output
        log_label = QLabel("Installation Log:")
        layout.addWidget(log_label)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        log_font = QFont("Courier")
        log_font.setPointSize(9)
        self.log_text.setFont(log_font)
        self.log_text.setMinimumHeight(250)
        layout.addWidget(self.log_text)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.retry_button = QPushButton("Retry")
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(self._on_retry_clicked)
        button_layout.addWidget(self.retry_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        button_layout.addWidget(self.cancel_button)

        layout.addLayout(button_layout)
        self.setLayout(layout)

    def _setup_signals(self):
        """Setup signal handlers."""
        self.progress = InstallProgress()
        self.progress.log_message.connect(self._on_log_message)
        self.progress.install_complete.connect(self._on_install_complete)

    def _on_log_message(self, message: str):
        """Handle log message from installation thread."""
        self.log_text.appendPlainText(message)
        # Auto-scroll to bottom
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

        # Detect network errors for retry UI
        if any(
            keyword in message.lower()
            for keyword in [
                "connection",
                "timeout",
                "network",
                "failed to establish",
                "unable to locate",
            ]
        ):
            self._network_error_detected = True

    def _on_install_complete(self, success: bool, message: str):
        """Handle installation completion."""
        self._success = success
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(100 if success else 0)
        self.cancel_button.setText("OK" if success else "Close")

        if not success:
            # Show retry button only if network error was detected
            if self._network_error_detected:
                self.retry_button.setVisible(True)

            final_msg = f"\n[ERROR] {message}"
        else:
            final_msg = "\n[SUCCESS] Installation complete! Closing..."

        self.log_text.appendPlainText(final_msg)

        # Auto-close after 2 seconds if successful
        if success:
            self.setWindowTitle("VirtuGhan • Setup Complete")
            from qgis.PyQt.QtCore import QTimer

            QTimer.singleShot(2000, self.accept)
        else:
            self.setWindowTitle("VirtuGhan • Setup Failed")

    def _on_retry_clicked(self):
        """Retry installation."""
        self._network_error_detected = False
        self.log_text.clear()
        self.retry_button.setVisible(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setMaximum(0)  # Back to indeterminate
        self.progress_bar.setValue(0)
        self._start_install()

    def _on_cancel_clicked(self):
        """Cancel or close dialog."""
        if self._success:
            self.accept()
        else:
            self.reject()

    def exec_with_install(self) -> bool:
        """
        Show dialog and run installation.
        Returns True if installation succeeded, False otherwise.
        """
        self.log_text.appendPlainText(
            "Please wait while required dependencies are downloaded and installed."
        )
        self.log_text.appendPlainText(
            "This can take 5-10 minutes depending on your internet speed."
        )
        self.log_text.appendPlainText(
            "Please do not close this window. It will close automatically when setup is complete."
        )
        self.log_text.appendPlainText(
            "This is a one-time installation for this QGIS profile."
        )
        self.log_text.appendPlainText("")
        self.log_text.appendPlainText("Preparing setup... installation will start in 1 second.")
        QTimer.singleShot(1000, self._start_install)
        result = self.exec_()
        return self._success

    def _start_install(self):
        """Start installation in background thread."""
        if not self.install_callback:
            self.progress.install_complete.emit(
                False, "No installation callback provided"
            )
            return

        def _install_worker():
            try:
                success = self.install_callback(
                    progress_callback=lambda msg: self.progress.log_message.emit(msg)
                )
                if success:
                    self.progress.install_complete.emit(
                        True, "All dependencies installed successfully"
                    )
                else:
                    detail = None
                    try:
                        from .bootstrap import get_last_bootstrap_error

                        detail = get_last_bootstrap_error()
                    except Exception:
                        detail = None

                    self.progress.install_complete.emit(
                        False,
                        detail or "Installation failed. Check log above for details.",
                    )
            except Exception as exc:
                self.progress.install_complete.emit(
                    False, f"Installation exception: {exc}"
                )

        self._install_thread = threading.Thread(target=_install_worker, daemon=True)
        self._install_thread.start()
