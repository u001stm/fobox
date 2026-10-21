# FOBOX 2.0 — experimental RAW sorter

This is a **test version**, not a validated fully automatic face/pose sorter. Review every contact sheet before copying RAW.

## Windows

Install Python 3.10–3.12. Open PowerShell in this folder and run:

```powershell
py -m pip install -r requirements.txt
py sort_v2.py --source "D:\DCIM\100MSDCF" --destination "F:\RAW_SORTED_V2" --manifest students.csv --contact-sheets
```

First run may download InsightFace buffalo_l weights; subsequent image analysis is local. Preview output: `fobox_v2_plan.json` and `fobox_review` contact sheets. **No RAW is copied** until you add `--copy` and confirm with `COPY`. Destination must be empty; source files remain untouched.

## Worksheet

Copy `students_example.csv` to `students.csv` and replace the sample rows with your actual data. Columns: `Name,V,P1,P2,D1,D2`. The first three are portraits; D1/D2 are friends photos. Five chosen frames are mandatory; additional frames are selected by face similarity and perceptual difference, with no fixed number limit.

## Camera filename prefix

`ANT` is **not** fixed. The three Latin letters in names such as `ANT01750.ARW` belong to the photographer's surname. FOBOX autodetects a unique three-letter prefix; if multiple are found, use `--prefix PET` (or the relevant prefix). Copied RAW filenames retain the original camera prefix.

## Privacy and limitations

Do not commit student names or images to this public GitHub repository. This version may miss useful images or include wrong people. It does **not** implement full body-pose estimation. Test on three students first.

## FOBOX 2.1 — mixed-reference protection

Run `sort_v2_1.py` instead of `sort_v2.py`:

```powershell
.\.venv\Scripts\python.exe sort_v2_1.py --contact-sheets
```

The new version compares all three mandatory portrait reference faces pairwise. If any portrait has zero/multiple detected faces or any pair falls below `--reference-min-score 0.50`, that student is **blocked** and listed in `fobox_v2_blocked.json`. No additional frames are selected for blocked students, and `--copy` is disabled globally until all reference conflicts are corrected. Portrait anchors belonging to other students are excluded from extra candidates. Review `fobox_v2_plan.json` and contact sheets before export. This is a conservative heuristic: a block may be a false positive and a passed reference set does not prove identity.


## Review UX requirements — portrait series vs friends (planned, not implemented)

- Every review sheet is headed by **student surname and given name** from the worksheet, not a generic student number. A sequence counter may appear only as secondary metadata.
- **Portrait/vignette review:** show mandatory V, P1, P2 anchors, selected portrait images, and a contextual strip of nearby frames **before and after each detected series**. Include adjacent other people to make the series boundary clear. Context frames are **not automatically selected for RAW export**.
- **Friends review:** show D1 and D2 and their nearby group-photo frames in a separate section. Multi-person photos are expected; do not require exactly one detected face or use portrait-only rejection rules.
- Permit quick keep/remove decisions, preserve five worksheet-mandatory selections, and require human confirmation before export. If a mandatory frame seems wrong, flag it for worksheet correction rather than silently deleting it.
- Distinguish visually between **selected**, **context only**, **mandatory**, and **uncertain** frames.

The existing `sort_v2_1.py` does **not yet implement** this split interactive review interface. This section records the specification for the next version.
