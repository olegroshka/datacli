# Write a datacli run outcome to the Windows Application event log.
# Works from a logged-off (S4U) task, unlike a toast. One-time setup from an
# elevated shell:  New-EventLog -LogName Application -Source datacli
#
#   [scheduler]
#   notify_on = "always"
#   notify_command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
#                     "-File", "C:/Users/<you>/PycharmProjects/datacli/scripts/notify_eventlog.ps1",
#                     "-Outcome", "{outcome}", "-Message", "{summary}"]
#
# Event IDs: 1000 = succeeded/no_op (Information), 1001 = anything else (Error).
# Filter in Event Viewer: Application log, source "datacli".
param(
    [string]$Outcome = "failed",
    [string]$Message = ""
)

try {
    $ok = @("succeeded", "no_op") -contains $Outcome
    $type = if ($ok) { "Information" } else { "Error" }
    $id = if ($ok) { 1000 } else { 1001 }
    if (-not [System.Diagnostics.EventLog]::SourceExists("datacli")) {
        Write-Error "event source 'datacli' is not registered; run New-EventLog -LogName Application -Source datacli from an elevated shell"
        exit 2
    }
    Write-EventLog -LogName Application -Source datacli -EntryType $type -EventId $id -Message "datacli scheduler: $Outcome`n$Message"
    exit 0
} catch {
    Write-Error "notify_eventlog: $($_.Exception.Message)"
    exit 1
}
