# RAW SORTER v1.0 — Windows PowerShell 5.1+
# Input CSV (UTF-8): Name,V,P1,P2,D1,D2
param(
 [string]$Source = 'D:\DCIM\100MSDCF',
 [string]$Destination = 'F:\RAW_SORTED',
 [string]$Manifest = '.\students.csv',
 [int]$Limit = 18
)
$ErrorActionPreference = 'Stop'
if ($Limit -lt 5) { throw 'Limit must be at least 5' }
if (-not (Test-Path -LiteralPath $Source -PathType Container)) { throw "Source not found: $Source" }
if (-not (Test-Path -LiteralPath $Manifest -PathType Leaf)) { throw "CSV not found: $Manifest" }
$students = @(Import-Csv -LiteralPath $Manifest -Encoding UTF8)
if ($students.Count -eq 0) { throw 'Empty CSV' }
$files = @(Get-ChildItem -LiteralPath $Source -File -Filter '*.ARW')
$byNumber = @{}
foreach ($f in $files) {
 if ($f.BaseName -match '^ANT0*(\d+)$') {
  $n = [int]$Matches[1]
  if ($byNumber.ContainsKey($n)) { throw "Duplicate frame number: $n" }
  $byNumber[$n] = $f
 }
}
$plan = @()
foreach ($s in $students) {
 $name = [string]$s.Name
 if ($name -notmatch '^\S+\s+\S+' -or $name -match '[\\/:*?"<>|]') { throw "Invalid name: $name" }
 $name = (($name.Trim() -split '\s+') | Select-Object -First 2) -join ' '
 $fields = @('V','P1','P2','D1','D2')
 $seeds = @()
 foreach ($field in $fields) {
  $n = 0
  if (-not [int]::TryParse([string]$s.$field, [ref]$n) -or $n -lt 1) { throw "Invalid $field for $name" }
  $seeds += $n
 }
 if (@($seeds | Select-Object -Unique).Count -ne 5) { throw "Repeated selected number for $name; review manually" }
 foreach ($n in $seeds) { if (-not $byNumber.ContainsKey($n)) { throw "Missing RAW $n for $name" } }
 $selected = [System.Collections.Generic.HashSet[int]]::new()
 foreach ($n in $seeds) { [void]$selected.Add($n) }
 $candidates = @($byNumber.Keys | Where-Object {
  $n = $_
  if ($selected.Contains($n)) { return $false }
  foreach ($seed in $seeds) { if ([math]::Abs($n - $seed) -le 8) { return $true } }
  return $false
 } | Sort-Object @{Expression={ $n=$_; ($seeds | ForEach-Object { [math]::Abs($n - $_) } | Measure-Object -Minimum).Minimum }}, @{Expression={$_}})
 foreach ($n in $candidates) { if ($selected.Count -ge $Limit) { break }; [void]$selected.Add([int]$n) }
 $rename = @{}
 $rename[$seeds[0]] = "${name}_$($byNumber[$seeds[0]].Name)"
 $rename[$seeds[1]] = "ЛР_${name}_$($byNumber[$seeds[1]].Name)"
 $rename[$seeds[2]] = "ЛС_${name}_$($byNumber[$seeds[2]].Name)"
 $rename[$seeds[3]] = "Друг1_$($byNumber[$seeds[3]].Name)"
 $rename[$seeds[4]] = "Друг2_$($byNumber[$seeds[4]].Name)"
 $entries = @($selected | Sort-Object | ForEach-Object {
  $n = [int]$_
  $f = $byNumber[$n]
  $out = if ($rename.ContainsKey($n)) { $rename[$n] } else { $f.Name }
  [pscustomobject]@{ Source=$f.FullName; OutputName=$out; Size=$f.Length }
 })
 $plan += [pscustomobject]@{ Name=$name; Entries=$entries }
}
$bytes = ($plan | ForEach-Object {$_.Entries} | Measure-Object -Property Size -Sum).Sum
$root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Destination))
if (-not (Test-Path -LiteralPath $root)) { throw "Destination drive missing: $root" }
$driveName = $root.Substring(0,1)
$drive = Get-PSDrive -Name $driveName
foreach ($p in $plan) {
 $folder = Join-Path $Destination $p.Name
 if (Test-Path -LiteralPath $folder) { throw "Destination folder exists: $folder" }
 Write-Host "$($p.Name): $($p.Entries.Count) RAW"
}
Write-Host ('Total {0:N2} GB; free {1:N2} GB' -f ($bytes/1GB),($drive.Free/1GB))
if ($drive.Free -lt ($bytes + 200MB)) { throw 'Insufficient free space' }
Write-Host 'DRY RUN: no files changed. The algorithm chooses nearby frame numbers, not poses.'
if ((Read-Host 'Type YES to copy') -cne 'YES') { Write-Host 'Cancelled'; return }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
foreach ($p in $plan) {
 $folder = Join-Path $Destination $p.Name
 New-Item -ItemType Directory -Path $folder -ErrorAction Stop | Out-Null
 foreach ($e in $p.Entries) {
  Copy-Item -LiteralPath $e.Source -Destination (Join-Path $folder $e.OutputName) -ErrorAction Stop
 }
 Write-Host "Done: $($p.Name)"
}
Write-Host "FINISHED: $Destination"
