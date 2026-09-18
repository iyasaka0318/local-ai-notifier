$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$eventConfig = Join-Path $projectDir 'config\tasks_event.json'
$listenerVbs = Join-Path $projectDir 'run_tasks_listener_hidden.vbs'
$legacyTaskName = 'LocalAI Keep Watcher'
$listenerTaskName = 'LocalAI Tasks Event Listener'

if (-not (Test-Path -LiteralPath $eventConfig)) {
    throw 'config\tasks_event.json is missing. Run scripts\tasks_event_setup.py first.'
}
if (-not (Test-Path -LiteralPath $listenerVbs)) {
    throw 'The Tasks event listener launcher is missing.'
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$listenerAction = New-ScheduledTaskAction `
    -Execute $wscript `
    -Argument ('"{0}"' -f $listenerVbs)
$listenerTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$listenerSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$listenerPrincipal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $listenerTaskName `
    -Action $listenerAction `
    -Trigger $listenerTrigger `
    -Settings $listenerSettings `
    -Principal $listenerPrincipal `
    -Description 'Starts local AI processing when Google Tasks changes' `
    -Force | Out-Null

# 旧タスクは削除せず、毎分から6時間ごとの保険確認へ変更します。
$legacyTask = Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue
if ($legacyTask) {
    $expectedPath = (Join-Path $projectDir 'run_keep_hidden.vbs').ToLowerInvariant()
    $matchesProject = $false
    foreach ($action in $legacyTask.Actions) {
        $candidate = (($action.Execute + ' ' + $action.Arguments).Trim()).ToLowerInvariant()
        if ($candidate.Contains($expectedPath)) {
            $matchesProject = $true
            break
        }
    }
    if (-not $matchesProject) {
        throw 'The legacy task action is unexpected. No legacy task changes were made.'
    }
    $fallbackTrigger = New-ScheduledTaskTrigger `
        -Once `
        -At (Get-Date).AddMinutes(5) `
        -RepetitionInterval (New-TimeSpan -Hours 6)
    Set-ScheduledTask `
        -TaskName $legacyTaskName `
        -Trigger $fallbackTrigger | Out-Null
}

Start-ScheduledTask -TaskName $listenerTaskName
Write-Output 'Tasks event listener was registered and started.'
if ($legacyTask) {
    Write-Output 'Legacy one-minute polling was changed to a six-hour fallback.'
}
