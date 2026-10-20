# FOBOX — RAW SORTER

Local RAW sorting for school photo delivery. **Version 1.0** (PowerShell).

## Features

- Reads `Name,V,P1,P2,D1,D2` from UTF-8 CSV.
- Finds Sony `ANTxxxxx.ARW` files by the frame numbers chosen in the worksheet.
- Always includes five required RAW files and adds nearby frames (±8 frame numbers), up to 18 total per student by default.
- Renames **only copied selected files**: `Фамилия Имя_ANTxxxxx.ARW`, `ЛР_Фамилия Имя_ANTxxxxx.ARW`, `ЛС_Фамилия Имя_ANTxxxxx.ARW`, `Друг1_ANTxxxxx.ARW`, `Друг2_ANTxxxxx.ARW`.
- Other copied RAW files keep original filenames; source files are never modified.
- Checks missing required frames, output folders and free space before confirmation.

**Limitations:** v1 selects neighbors by filename number, not visual similarity, identity, or pose. Review the output for photos of other students, white frames and exposure-bracketing duplicates. It requires five distinct frame numbers per student. Do not upload student photographs or real worksheets to this public repository.

## Run on Windows

1. Save the real worksheet as UTF-8 CSV with columns `Name,V,P1,P2,D1,D2`. Put it alongside `sort_raw.ps1` or specify its full path. `Name` may contain patronymic; only first and last name are used in output.
2. Open PowerShell in the script directory and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\sort_raw.ps1 -Source 'D:\DCIM\100MSDCF' -Destination 'F:\RAW_SORTED' -Manifest '.\students.csv'
```

3. Review the dry-run totals. Type uppercase `YES` to copy. Existing student output folders are not overwritten.

Use UTF-8 **with BOM** when saving `sort_raw.ps1` locally for Windows PowerShell 5.1 to read Cyrillic correctly. PowerShell 7 reads UTF-8 directly.

## Version 2 roadmap

- Analyze local JPEG previews without uploading photos to external services.
- Exclude all-white/black technical frames and exposure-bracketing duplicates.
- Identify diverse poses and compositions around selected frame numbers; keep required five.
- Target 16–22 RAW per student **only when valid candidates exist**, with review for ambiguous matches.
- Produce audit report and allow interactive approval before copying.

All real photos and personal data remain local.