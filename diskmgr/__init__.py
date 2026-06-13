"""diskmgr — lightweight Windows disk/partition management core.

Pure-stdlib package that wraps the Windows Storage Management PowerShell
cmdlets (Get-Disk / Get-Partition / Get-Volume / Get-PartitionSupportedSize /
Resize-Partition). No third-party dependencies.
"""

__all__ = ["model", "storage", "admin", "logging_util"]
