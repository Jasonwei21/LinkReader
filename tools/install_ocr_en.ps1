$ErrorActionPreference = 'Continue'
$log = 'C:\Users\Jasonwei\AppData\Local\Temp\lr_ocr_install.log'
function W($m) { ((Get-Date).ToString('HH:mm:ss') + '  ' + $m) | Out-File -FilePath $log -Append -Encoding utf8 }

'=== LinkReader OCR 英文包安装 ===' | Out-File -FilePath $log -Encoding utf8
W ('管理员: ' + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))

try {
    $before = Get-WindowsCapability -Online -Name 'Language.OCR*' -ErrorAction Stop
    W '--- 安装前 ---'
    foreach ($c in $before) { W ('  ' + $c.Name + '  ' + $c.State) }
} catch {
    W ('读取能力失败: ' + $_.Exception.Message)
}

$target = 'Language.OCR~~~en-US~0.0.1.0'
try {
    W ('开始安装 ' + $target)
    $r = Add-WindowsCapability -Online -Name $target -ErrorAction Stop
    W ('安装返回: RestartNeeded=' + $r.RestartNeeded)
} catch {
    W ('安装失败: ' + $_.Exception.Message)
}

try {
    $after = Get-WindowsCapability -Online -Name 'Language.OCR*' -ErrorAction Stop
    W '--- 安装后 ---'
    foreach ($c in $after) { W ('  ' + $c.Name + '  ' + $c.State) }
} catch {
    W ('复查失败: ' + $_.Exception.Message)
}

W '=== 结束 ==='
