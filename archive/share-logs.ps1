<#
.SYNOPSIS
    Publishes a capture machine's log directory as a read-only share for the archive puller.

.DESCRIPTION
    Runs once per capture machine, elevated. It is the only change the pull design asks of a
    capture box, and it is deliberately a small one: a share with read access and nothing
    else. No inbound application port is opened, main.py keeps listening on 127.0.0.1 as
    shipped, and nothing about the capture's own behaviour changes.

    Read access, not Change. Share permissions and NTFS permissions are combined by taking
    the more restrictive of the two, so granting only Read at the share level means the
    puller cannot alter or delete a capture's logs whatever the file system would otherwise
    have allowed. That matters more than it looks: retention.py is entitled to delete old
    files here on its own schedule, and the archive is meant to be the copy that outlives
    them -- traffic in the other direction would defeat the point.

.PARAMETER ReadAccount
    Who may read the share. For a viewer machine pulling as SYSTEM this is that machine's
    account, DOMAIN\VIEWERBOX$ -- the trailing $ is not a typo. A group holding it is the
    better answer once there is more than one thing reading.

.PARAMETER Path
    Directory to share. Defaults to the location the installed service writes to.

.PARAMETER ShareName
    Share name. The default is what sources.txt expects.

.PARAMETER OpenFirewall
    Also enable the built-in File and Printer Sharing rules for the domain and private
    profiles. Off by default -- on a managed fleet this is usually policy's job, and a
    script quietly changing firewall state is a surprise nobody needs.

.EXAMPLE
    .\share-logs.ps1 -ReadAccount "CONTOSO\VIEWERBOX$"
    .\share-logs.ps1 -ReadAccount "CONTOSO\Nicla-Archive-Readers" -OpenFirewall
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ReadAccount,
    [string] $Path = (Join-Path $env:ProgramData "NiclaSense\logs"),
    [string] $ShareName = "NiclaLogs",
    [switch] $OpenFirewall
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Path)) {
    throw "no log directory at $Path -- is the capture service installed and has it run?"
}

$existing = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Path -ne $Path) {
        throw ("a share called $ShareName already points at {0}, not $Path -- " +
               "remove it or choose another -ShareName" -f $existing.Path)
    }
    Write-Output "share $ShareName already exists; updating access"
} else {
    New-SmbShare -Name $ShareName -Path $Path -Description "Nicla capture logs (read-only)" |
        Out-Null
    Write-Output "created share $ShareName -> $Path"
}

# Everyone's default Read grant is removed rather than left alongside the explicit one: the
# most permissive share ACE wins, so leaving it would make the named account irrelevant.
Revoke-SmbShareAccess -Name $ShareName -AccountName "Everyone" -Force -ErrorAction SilentlyContinue |
    Out-Null

Grant-SmbShareAccess -Name $ShareName -AccountName $ReadAccount -AccessRight Read -Force |
    Out-Null
Write-Output "granted Read to $ReadAccount"

if ($OpenFirewall) {
    Enable-NetFirewallRule -DisplayGroup "File and Printer Sharing" -Profile Domain, Private
    Write-Output "enabled File and Printer Sharing for the domain and private profiles"
} else {
    Write-Output "not touching the firewall; File and Printer Sharing must be reachable from the viewer"
}

Write-Output ""
Write-Output "add this line to the viewer's archive\sources.txt:"
Write-Output ("    {0}    \\{1}\{2}" -f [Environment]::MachineName.ToLower(), [Environment]::MachineName, $ShareName)
