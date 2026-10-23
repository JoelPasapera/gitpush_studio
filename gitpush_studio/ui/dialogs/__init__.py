# -*- coding: utf-8 -*-
"""Un diálogo por archivo. Todos heredan de ui.base."""

from .branches import BranchDialog
from .bulk_clone import BulkCloneDialog
from .clone import CloneDialog
from .consolidate import ConsolidateDialog
from .create_repo import CreateRepoDialog
from .history import HistoryDialog
from .pages import PagesDialog
from .preview import PreviewDialog
from .release import ReleaseDialog
from .review import ReviewDialog
from .sync import SyncDialog

__all__ = ["BranchDialog", "BulkCloneDialog", "CloneDialog",
           "ConsolidateDialog", "CreateRepoDialog", "HistoryDialog",
           "PagesDialog", "PreviewDialog", "ReleaseDialog", "ReviewDialog",
           "SyncDialog"]
