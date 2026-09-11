# Show a Windows toast notification. Used as the scheduler's notify_command:
#
#   [scheduler]
#   notify_command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
#                     "-File", "C:/Users/<you>/PycharmProjects/datacli/scripts/notify_toast.ps1",
#                     "-Title", "datacli {job_id}: {outcome}",
#                     "-Message", "{summary}"]
#
# Toasts need an interactive session for the same user; that is exactly the
# session the InteractiveToken task runs in. Nothing here is fatal.
param(
    [string]$Title = "datacli",
    [string]$Message = ""
)

try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

    $safeTitle = [System.Security.SecurityElement]::Escape($Title)
    $safeMessage = [System.Security.SecurityElement]::Escape($Message)
    $payload = "<toast scenario=`"reminder`"><visual><binding template=`"ToastGeneric`"><text>$safeTitle</text><text>$safeMessage</text></binding></visual></toast>"

    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($payload)
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    # PowerShell's own AppUserModelId so no app registration is needed.
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    exit 0
} catch {
    Write-Error "notify_toast: $($_.Exception.Message)"
    exit 1
}
