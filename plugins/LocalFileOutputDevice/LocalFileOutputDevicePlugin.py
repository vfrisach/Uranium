# Copyright (c) 2016 Ultimaker B.V.
# Uranium is released under the terms of the AGPLv3 or higher.

import os
import os.path
import sys

from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QFileDialog, QMessageBox

from UM.Application import Application
from UM.Preferences import Preferences
from UM.Logger import Logger
from UM.Mesh.MeshWriter import MeshWriter
from UM.FileHandler.WriteFileJob import WriteFileJob
from UM.Message import Message
from UM.MimeTypeDatabase import MimeType
from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin
from UM.OutputDevice.OutputDevice import OutputDevice
from UM.OutputDevice import OutputDeviceError
from UM.Platform import Platform

from UM.i18n import i18nCatalog
catalog = i18nCatalog("uranium")

##  Implements an OutputDevicePlugin that provides a single instance of LocalFileOutputDevice
class LocalFileOutputDevicePlugin(OutputDevicePlugin):
    def __init__(self):
        super().__init__()

        Preferences.getInstance().addPreference("local_file/last_used_type", "")
        Preferences.getInstance().addPreference("local_file/dialog_save_path", "")

    def start(self):
        self.getOutputDeviceManager().addOutputDevice(LocalFileOutputDevice())

    def stop(self):
        self.getOutputDeviceManager().removeOutputDevice("local_file")

##  Implements an OutputDevice that supports saving to arbitrary local files.
class LocalFileOutputDevice(OutputDevice):
    def __init__(self):
        super().__init__("local_file")

        self.setName(catalog.i18nc("@item:inmenu", "Local File"))
        self.setShortDescription(catalog.i18nc("@action:button Preceded by 'Ready to'.", "Save to File"))
        self.setDescription(catalog.i18nc("@info:tooltip", "Save to File"))
        self.setIconName("save")

        self._writing = False

    ##  Request the specified nodes to be written to a file.
    #
    #   \param nodes A collection of scene nodes that should be written to the
    #   file.
    #   \param file_name \type{string} A suggestion for the file name to write
    #   to. Can be freely ignored if providing a file name makes no sense.
    #   \param limit_mimetypes Should we limit the available MIME types to the
    #   MIME types available to the currently active machine?
    #   \param kwargs Keyword arguments.
    def requestWrite(self, nodes, file_name = None, limit_mimetypes = None, file_handler = None, **kwargs):
        if self._writing:
            raise OutputDeviceError.DeviceBusyError()

        # Set up and display file dialog
        dialog = QFileDialog()

        dialog.setWindowTitle(catalog.i18nc("@title:window", "Save to File"))
        dialog.setFileMode(QFileDialog.AnyFile)
        dialog.setAcceptMode(QFileDialog.AcceptSave)

        # Ensure platform never ask for overwrite confirmation since we do this ourselves
        dialog.setOption(QFileDialog.DontConfirmOverwrite)

        if sys.platform == "linux" and "KDE_FULL_SESSION" in os.environ:
            dialog.setOption(QFileDialog.DontUseNativeDialog)

        filters = []
        mime_types = []
        selected_filter = None
        last_used_type = Preferences.getInstance().getValue("local_file/last_used_type")

        if not file_handler:
            file_handler = Application.getInstance().getMeshFileHandler()

        file_types = file_handler.getSupportedFileTypesWrite()

        file_types.sort(key = lambda k: k["description"])
        if limit_mimetypes:
            file_types = list(filter(lambda i: i["mime_type"] in limit_mimetypes, file_types))

        if len(file_types) == 0:
            Logger.log("e", "There are no file types available to write with!")
            raise OutputDeviceError.WriteRequestFailedError()

        for item in file_types:
            type_filter = "{0} (*.{1})".format(item["description"], item["extension"])
            filters.append(type_filter)
            mime_types.append(item["mime_type"])
            if last_used_type == item["mime_type"]:
                selected_filter = type_filter
                if file_name:
                    file_name += "." + item["extension"]

        dialog.setNameFilters(filters)
        if selected_filter is not None:
            dialog.selectNameFilter(selected_filter)

        if file_name is not None:
            dialog.selectFile(file_name)

        stored_directory = Preferences.getInstance().getValue("local_file/dialog_save_path")
        dialog.setDirectory(stored_directory)

        if not dialog.exec_():
            raise OutputDeviceError.UserCanceledError()

        save_path = dialog.directory().absolutePath()
        Preferences.getInstance().setValue("local_file/dialog_save_path", save_path)

        selected_type = file_types[filters.index(dialog.selectedNameFilter())]
        Preferences.getInstance().setValue("local_file/last_used_type", selected_type["mime_type"])

        # Get file name from file dialog
        file_name = dialog.selectedFiles()[0]
        Logger.log("d", "Writing to [%s]..." % file_name)
        # OSX does not handle extensions with multiple periods correctly.
        if Platform.isOSX():
            Logger.log("d", "OS X: checking for double extension...")
            mime_types = MimeType("", "", [t['extension'] for t in file_types])
            # Check if an extension is added multiple times.
            while mime_types.stripExtension(file_name) != mime_types.stripExtension(mime_types.stripExtension(file_name)):
                file_name = mime_types.stripExtension(file_name)
                Logger.log("d", "Multi extension detected, setting filename to [%s]" % file_name)

        if os.path.exists(file_name):
            result = QMessageBox.question(None, catalog.i18nc("@title:window", "File Already Exists"), catalog.i18nc("@label", "The file <filename>{0}</filename> already exists. Are you sure you want to overwrite it?").format(file_name))
            if result == QMessageBox.No:
                raise OutputDeviceError.UserCanceledError()

        self.writeStarted.emit(self)

        # Actually writing file
        if file_handler:
            file_writer = file_handler.getWriter(selected_type["id"])
        else:
            file_writer = Application.getInstance().getMeshFileHandler().getWriter(selected_type["id"])

        try:
            mode = selected_type["mode"]
            if mode == MeshWriter.OutputMode.TextMode:
                Logger.log("d", "Writing to Local File %s in text mode", file_name)
                stream = open(file_name, "wt", encoding = "utf-8")
            elif mode == MeshWriter.OutputMode.BinaryMode:
                Logger.log("d", "Writing to Local File %s in binary mode", file_name)
                stream = open(file_name, "wb")

            job = WriteFileJob(file_writer, stream, nodes, mode)
            job.setFileName(file_name)
            job.progress.connect(self._onJobProgress)
            job.finished.connect(self._onWriteJobFinished)

            message = Message(catalog.i18nc("@info:progress", "Saving to <filename>{0}</filename>").format(file_name), 0, False, -1)
            message.show()

            job._message = message
            self._writing = True
            job.start()
        except PermissionError as e:
            Logger.log("e", "Permission denied when trying to write to %s: %s", file_name, str(e))
            raise OutputDeviceError.PermissionDeniedError(catalog.i18nc("@info:status", "Permission denied when trying to save <filename>{0}</filename>").format(file_name)) from e
        except OSError as e:
            Logger.log("e", "Operating system would not let us write to %s: %s", file_name, str(e))
            raise OutputDeviceError.WriteRequestFailedError(catalog.i18nc("@info:status", "Could not save to <filename>{0}</filename>: <message>{1}</message>").format()) from e

    def _onJobProgress(self, job, progress):
        if hasattr(job, "_message"):
            job._message.setProgress(progress)
        self.writeProgress.emit(self, progress)

    def _onWriteJobFinished(self, job):
        if hasattr(job, "_message"):
            job._message.hide()
            job._message = None

        self._writing = False
        self.writeFinished.emit(self)
        if job.getResult():
            self.writeSuccess.emit(self)
            message = Message(catalog.i18nc("@info:status", "Saved to <filename>{0}</filename>").format(job.getFileName()))
            message.addAction("open_folder", catalog.i18nc("@action:button", "Open Folder"), "open-folder", catalog.i18nc("@info:tooltip","Open the folder containing the file"))
            message._folder = os.path.dirname(job.getFileName())
            message.actionTriggered.connect(self._onMessageActionTriggered)
            message.show()
        else:
            message = Message(catalog.i18nc("@info:status", "Could not save to <filename>{0}</filename>: <message>{1}</message>").format(job.getFileName(), str(job.getError())), lifetime = 0)
            message.show()
            self.writeError.emit(self)
        job.getStream().close()

    def _onMessageActionTriggered(self, message, action):
        if action == "open_folder" and hasattr(message, "_folder"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(message._folder))
