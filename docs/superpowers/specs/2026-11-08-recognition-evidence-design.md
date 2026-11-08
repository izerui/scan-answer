# Recognition Quality and Evidence Consistency

Package 5 implements RC-02 through RC-04 of the mainline acceptance analysis.
The authorized direction remains scanner/image/filled PDF input, identification
and answers, then per-question images and portable JSON. A3 and smaller paper,
including the existing 390 x 270 mm 8K template, remain supported.

## Approach

Keep the existing placement, measurement, decision and rendering stages.
Extend their evidence rather than replacing the recognition engine or relaxing
acceptance thresholds. Only adding warnings would leave false blanks and
misleading crops; replacing alignment is unnecessary for these defects.

Measurements carry explicit invalidity and local displacement. Internally an
invalid measurement is NaN, never zero; persisted arrays convert it to JSON null.
Decision code marks affected rows failed and excludes invalid samples from
threshold fitting. A warped source coverage mask distinguishes padded white
pixels from real blank paper. Sampling metadata records face, shift in mm and
cell failure reasons. A failed absent measurement means unknown, not false.
Batch light-mark diagnostics do not require five dark marks; adaptive threshold
fitting retains its conservative prerequisites.

Barcode detection retains spatially distinct candidates, even for identical
text. Original and binarized detection results merge by spatial identity.
Association processes region, expanded region and nearby page candidates in
that priority order; weaker fallback cannot contest an established stronger
association. Within each tier, reject ambiguous/contended assignment and retain candidate polygons and
reasons. Keep the template-region crop and add candidate crops with canonical
paths, including the actual selected hit. Candidate evidence must survive state,
redecision, delivery audit and result viewing. Independent exam number/barcode
values and ID_MISMATCH remain.

New analyses use effective local sampling offsets for OMR crops, region JSON
and overlays. Template geometry remains available separately; delivery checks
validate bounded finite offsets and the derived effective geometry. New crops
preserve light gray handwriting on both faces. Processing metadata identifies
geometric resampling and legacy back whitening; existing images must not be
rewritten while OCR reads them. Legacy analysis without sampling metadata keeps
legacy crop geometry and processing, and does not claim new evidence.

## Constraints and Verification

No grading, ZIP, business API, scanner redesign, dependency upgrades, commits
or packaging. Preserve earlier uncommitted changes. Verify focused failure
cases, old-state compatibility, full fast tests and original 200-card regression.
Keep existing synthetic random sequence and accuracy gates. Real scanner,
Windows and paid OCR validation remain explicitly separate.
