import os
import traceback

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon

from .dialog import ForestGeoStudioDialog


class ForestGeoStudio:

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.toolbar = None

    def log(self, msg):
        print("[ForestGeoStudio]", msg)

    def initGui(self):
        try:
            plugin_dir = os.path.dirname(__file__)
            icon_path = os.path.join(plugin_dir, "icon.png")
            icon = QIcon(icon_path)
            if icon.isNull():
                self.log(f"Icon not loaded: {icon_path}")
            else:
                self.log(f"Icon loaded: {icon_path}")

            self.action = QAction(icon, "ForestGeo Studio", self.iface.mainWindow())
            self.action.setObjectName("forestGeoStudioAction")
            self.action.setToolTip("ForestGeo Studio")
            self.action.setStatusTip("ForestGeo Studio")
            self.action.setWhatsThis("ForestGeo Studio")
            self.action.triggered.connect(self.run)

            self.iface.addPluginToMenu("&ForestGeo Studio", self.action)

            if not self.toolbar:
                self.toolbar = self.iface.addToolBar("ForestGeo Studio")
                self.toolbar.setObjectName("ForestGeoStudioToolbar")
                self.toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)

            self.toolbar.addAction(self.action)

            button = self.toolbar.widgetForAction(self.action)
            if button:
                button.setToolButtonStyle(Qt.ToolButtonIconOnly)
                button.setToolTip("ForestGeo Studio")
                button.setStatusTip("ForestGeo Studio")

            self.log("Plugin initialized")

        except Exception:
            self.log("initGui ERROR\n" + traceback.format_exc())

    def unload(self):
        try:
            if self.action:
                self.iface.removePluginMenu("&ForestGeo Studio", self.action)
            if self.toolbar:
                self.iface.mainWindow().removeToolBar(self.toolbar)
                self.toolbar = None
            self.log("Plugin unloaded")
        except Exception:
            self.log("unload ERROR\n" + traceback.format_exc())

    def run(self):
        try:
            self.log("===== OPEN DIALOG =====")
            dlg = ForestGeoStudioDialog(self.iface, parent=self.iface.mainWindow())
            dlg.exec_()
        except Exception:
            self.log("run ERROR\n" + traceback.format_exc())
