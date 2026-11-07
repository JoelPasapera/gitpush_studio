# -*- coding: utf-8 -*-
"""Un diálogo por archivo. Todos heredan de ui.base."""

from .analytics import AnalyticsDialog
from .apply_txt import ApplyTxtDialog
from .batch import BatchPublishDialog
from .branches import BranchDialog
from .bulk_clone import BulkCloneDialog
from .clone import CloneDialog
from .consolidate import ConsolidateDialog
from .create_repo import CreateRepoDialog
from .history import HistoryDialog
from .history_scan import HistoryScanDialog
from .issues_txt import IssuesTxtDialog
from .pages import PagesDialog
from .preview import PreviewDialog
from .release import ReleaseDialog
from .remote import RemoteDialog
from .review import ReviewDialog
from .sync import SyncDialog
from .why import WhyDialog

__all__ = ["AnalyticsDialog", "ApplyTxtDialog", "BatchPublishDialog", "BranchDialog",
           "BulkCloneDialog", "CloneDialog", "ConsolidateDialog",
           "CreateRepoDialog", "HistoryDialog", "HistoryScanDialog",
           "IssuesTxtDialog",
           "PagesDialog", "PreviewDialog", "ReleaseDialog", "RemoteDialog",
           "ReviewDialog",
           "SyncDialog", "WhyDialog"]
