"""
Qt5/Qt6 enum compatibility layer for QGIS plugins.

QGIS 4.0+ uses Qt6 where enums are scoped (e.g., Qt.ItemDataRole.UserRole
instead of Qt.UserRole). The qgis.PyQt shim does NOT fully backport flat
enum access, so we provide fallback wrappers here.

Usage:
    from .qt_compat import QtCompat
    role = QtCompat.UserRole
    alignment = QtCompat.AlignCenter
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QFrame,
    QMessageBox,
    QStyle,
    QTableWidget,
)
from qgis.PyQt.QtGui import QPainter


def _resolve(obj, *candidates):
    """Try each dotted path on obj, return the first that resolves."""
    for path in candidates:
        parts = path.split(".")
        current = obj
        for part in parts:
            current = getattr(current, part, None)
            if current is None:
                break
        if current is not None:
            return current
    raise AttributeError(f"Could not resolve any of {candidates} on {obj}")


# ---------------------------------------------------------------------------
# Qt namespace enums
# ---------------------------------------------------------------------------

class QtCompat:
    """Resolved Qt enum values that work on both Qt5 (QGIS 3.x) and Qt6 (QGIS 4.x)."""

    # Item data roles
    UserRole = _resolve(Qt, "UserRole", "ItemDataRole.UserRole")
    ToolTipRole = _resolve(Qt, "ToolTipRole", "ItemDataRole.ToolTipRole")

    # Alignment
    AlignCenter = _resolve(Qt, "AlignCenter", "AlignmentFlag.AlignCenter")
    AlignLeft = _resolve(Qt, "AlignLeft", "AlignmentFlag.AlignLeft")
    AlignTop = _resolve(Qt, "AlignTop", "AlignmentFlag.AlignTop")

    # Orientation
    Horizontal = _resolve(Qt, "Horizontal", "Orientation.Horizontal")
    Vertical = _resolve(Qt, "Vertical", "Orientation.Vertical")

    # Global colors
    transparent = _resolve(Qt, "transparent", "GlobalColor.transparent")
    white = _resolve(Qt, "white", "GlobalColor.white")

    # Mouse buttons
    LeftButton = _resolve(Qt, "LeftButton", "MouseButton.LeftButton")
    RightButton = _resolve(Qt, "RightButton", "MouseButton.RightButton")

    # Keys
    Key_Return = _resolve(Qt, "Key_Return", "Key.Key_Return")
    Key_Enter = _resolve(Qt, "Key_Enter", "Key.Key_Enter")
    Key_Escape = _resolve(Qt, "Key_Escape", "Key.Key_Escape")

    # Cursors
    ArrowCursor = _resolve(Qt, "ArrowCursor", "CursorShape.ArrowCursor")
    CrossCursor = _resolve(Qt, "CrossCursor", "CursorShape.CrossCursor")

    # Focus
    NoFocus = _resolve(Qt, "NoFocus", "FocusPolicy.NoFocus")
    OtherFocusReason = _resolve(Qt, "OtherFocusReason", "FocusReason.OtherFocusReason")

    # Scrollbar policy
    ScrollBarAlwaysOff = _resolve(Qt, "ScrollBarAlwaysOff", "ScrollBarPolicy.ScrollBarAlwaysOff")
    ScrollBarAsNeeded = _resolve(Qt, "ScrollBarAsNeeded", "ScrollBarPolicy.ScrollBarAsNeeded")

    # Item flags
    NoItemFlags = _resolve(Qt, "NoItemFlags", "ItemFlag.NoItemFlags")

    # Window flags / attributes
    WindowCloseButtonHint = _resolve(Qt, "WindowCloseButtonHint", "WindowType.WindowCloseButtonHint")
    WA_DeleteOnClose = _resolve(Qt, "WA_DeleteOnClose", "WidgetAttribute.WA_DeleteOnClose")

    # Dock widget areas
    NoDockWidgetArea = _resolve(Qt, "NoDockWidgetArea", "DockWidgetArea.NoDockWidgetArea")

    # Aspect ratio / transformation
    KeepAspectRatio = _resolve(Qt, "KeepAspectRatio", "AspectRatioMode.KeepAspectRatio")
    SmoothTransformation = _resolve(Qt, "SmoothTransformation", "TransformationMode.SmoothTransformation")

    # Text interaction
    TextSelectableByMouse = _resolve(Qt, "TextSelectableByMouse", "TextInteractionFlag.TextSelectableByMouse")

    # Date format
    ISODate = _resolve(Qt, "ISODate", "DateFormat.ISODate")

    # Pen style
    RoundCap = _resolve(Qt, "RoundCap", "PenCapStyle.RoundCap")
    RoundJoin = _resolve(Qt, "RoundJoin", "PenJoinStyle.RoundJoin")


# ---------------------------------------------------------------------------
# QMessageBox enums
# ---------------------------------------------------------------------------

class QMessageBoxCompat:
    Yes = _resolve(QMessageBox, "Yes", "StandardButton.Yes")
    No = _resolve(QMessageBox, "No", "StandardButton.No")
    Ok = _resolve(QMessageBox, "Ok", "StandardButton.Ok")
    Cancel = _resolve(QMessageBox, "Cancel", "StandardButton.Cancel")


# ---------------------------------------------------------------------------
# QFrame enums
# ---------------------------------------------------------------------------

class QFrameCompat:
    NoFrame = _resolve(QFrame, "NoFrame", "Shape.NoFrame")
    HLine = _resolve(QFrame, "HLine", "Shape.HLine")
    Plain = _resolve(QFrame, "Plain", "Shadow.Plain")


# ---------------------------------------------------------------------------
# QAbstractItemView enums
# ---------------------------------------------------------------------------

class QAbstractItemViewCompat:
    ScrollPerPixel = _resolve(QAbstractItemView, "ScrollPerPixel", "ScrollMode.ScrollPerPixel")
    SelectRows = _resolve(QAbstractItemView, "SelectRows", "SelectionBehavior.SelectRows")
    SingleSelection = _resolve(QAbstractItemView, "SingleSelection", "SelectionMode.SingleSelection")
    MultiSelection = _resolve(QAbstractItemView, "MultiSelection", "SelectionMode.MultiSelection")


# ---------------------------------------------------------------------------
# QPainter enums
# ---------------------------------------------------------------------------

class QPainterCompat:
    Antialiasing = _resolve(QPainter, "Antialiasing", "RenderHint.Antialiasing")


# ---------------------------------------------------------------------------
# QDockWidget enums
# ---------------------------------------------------------------------------

class QDockWidgetCompat:
    NoDockWidgetFeatures = _resolve(QDockWidget, "NoDockWidgetFeatures", "DockWidgetFeature.NoDockWidgetFeatures")


# ---------------------------------------------------------------------------
# QStyle enums
# ---------------------------------------------------------------------------

class QStyleCompat:
    SP_BrowserReload = _resolve(QStyle, "SP_BrowserReload", "StandardPixmap.SP_BrowserReload")
    SP_ComputerIcon = _resolve(QStyle, "SP_ComputerIcon", "StandardPixmap.SP_ComputerIcon")
    SP_ArrowDown = _resolve(QStyle, "SP_ArrowDown", "StandardPixmap.SP_ArrowDown")
    SP_DirIcon = _resolve(QStyle, "SP_DirIcon", "StandardPixmap.SP_DirIcon")
    SP_FileDialogListView = _resolve(QStyle, "SP_FileDialogListView", "StandardPixmap.SP_FileDialogListView")
    SP_FileDialogContentsView = _resolve(QStyle, "SP_FileDialogContentsView", "StandardPixmap.SP_FileDialogContentsView")
    SP_FileDialogDetailedView = _resolve(QStyle, "SP_FileDialogDetailedView", "StandardPixmap.SP_FileDialogDetailedView")
    SP_FileDialogInfoView = _resolve(QStyle, "SP_FileDialogInfoView", "StandardPixmap.SP_FileDialogInfoView")


# ---------------------------------------------------------------------------
# QTableWidget enums
# ---------------------------------------------------------------------------

class QTableWidgetCompat:
    SelectRows = _resolve(QTableWidget, "SelectRows", "SelectionBehavior.SelectRows")
    MultiSelection = _resolve(QTableWidget, "MultiSelection", "SelectionMode.MultiSelection")


# ---------------------------------------------------------------------------
# QDialogButtonBox enums
# ---------------------------------------------------------------------------

try:
    from qgis.PyQt.QtWidgets import QDialogButtonBox as _QDialogButtonBox

    class QDialogButtonBoxCompat:
        Close = _resolve(_QDialogButtonBox, "Close", "StandardButton.Close")
        Ok = _resolve(_QDialogButtonBox, "Ok", "StandardButton.Ok")
        Cancel = _resolve(_QDialogButtonBox, "Cancel", "StandardButton.Cancel")
except ImportError:
    class QDialogButtonBoxCompat:
        Close = 0x00200000
        Ok = 0x00000400
        Cancel = 0x00400000


# ---------------------------------------------------------------------------
# QSizePolicy enums
# ---------------------------------------------------------------------------

try:
    from qgis.PyQt.QtWidgets import QSizePolicy as _QSizePolicy

    class QSizePolicyCompat:
        Expanding = _resolve(_QSizePolicy, "Expanding", "Policy.Expanding")
        Preferred = _resolve(_QSizePolicy, "Preferred", "Policy.Preferred")
        Fixed = _resolve(_QSizePolicy, "Fixed", "Policy.Fixed")
        Minimum = _resolve(_QSizePolicy, "Minimum", "Policy.Minimum")
except ImportError:
    class QSizePolicyCompat:
        Expanding = 7
        Preferred = 5
        Fixed = 0
        Minimum = 1


# ---------------------------------------------------------------------------
# QgsWkbTypes compatibility (deprecated in QGIS 4.x)
# ---------------------------------------------------------------------------

def get_polygon_geometry_type():
    """Return the polygon geometry type constant compatible with both QGIS 3.x and 4.x."""
    try:
        from qgis.core import Qgis
        # QGIS 4.x uses Qgis.GeometryType
        return Qgis.GeometryType.Polygon
    except AttributeError:
        pass
    try:
        from qgis.core import QgsWkbTypes
        return QgsWkbTypes.PolygonGeometry
    except (ImportError, AttributeError):
        pass
    # Fallback: raw int value for PolygonGeometry
    return 2
