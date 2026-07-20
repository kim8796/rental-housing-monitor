# 우이 Codex Pet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate, validate, package, and install the Codex-compatible v2 animated pet **우이** from the supplied JPEG reference.

**Architecture:** Use the installed `hatch-pet` scripts to prepare prompts and deterministic geometry, and `$imagegen` workers to create the base, standard animation strips, cardinal anchors, and two coherent gaze strips. The parent copies selected generated assets into the run, performs deterministic extraction and validation, conducts independent visual QA, then installs the passing v2 package.

**Tech Stack:** Codex built-in image generation, hatch-pet Python scripts, bundled Python/Pillow, jq, WebP/PNG assets.

## Global Constraints

- Display name is exactly `우이`; stable package id is `uyi`.
- Ground identity in `/Users/kimyong/Downloads/1782207822.jpeg`.
- Preserve orange reptile body, cream belly, teal-green eyes, black hooded outfit, and attached tail flame in crisp retro pixel art.
- Exclude source backdrop, dialogue frame, and text.
- Final asset must be an 8×11 atlas with `spriteVersionNumber: 2` and pass deterministic and visual QA.

---

### Task 1: Prepare the pet run

**Files:**
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/pet_request.json`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/imagegen-jobs.json`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/prompts/`

**Interfaces:**
- Consumes: approved design and source JPEG.
- Produces: complete image-generation job graph and layout guides.

- [ ] **Step 1: Prepare the run**

Run `prepare_pet_run.py` with `--pet-name 우이 --pet-id uyi --style-preset pixel`, the exact reference path, and the approved identity notes.

- [ ] **Step 2: Inspect the manifest**

Run `jq` over every job and verify that base is ready, row jobs depend on base, look cardinals depend on standard-row completion, and look row 10 depends on look row 9.

### Task 2: Generate and approve the canonical base

**Files:**
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/decoded/base.png`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/references/canonical-base.png`

**Interfaces:**
- Consumes: `prompts/base-pet.md` and the source JPEG.
- Produces: canonical identity image used by every pose row.

- [ ] **Step 1: Generate the base with `$imagegen`**

Require one centered whole-body pixel pet on the prepared flat chroma background, preserving every global identity constraint.

- [ ] **Step 2: Copy and record the selected output**

Copy the selected PNG to both output paths and mark only the base job complete in `imagegen-jobs.json`.

- [ ] **Step 3: Visually verify identity**

Reject text, scenery, shadows, detached effects, or a changed hood/body/eye/flame design.

### Task 3: Generate and validate standard animation rows

**Files:**
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/decoded/{idle,running-right,running-left,waving,jumping,failed,waiting,running,review}.png`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/frames/`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/qa/previews/`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/final/spritesheet.webp`

**Interfaces:**
- Consumes: canonical base and per-row layout guides/prompts.
- Produces: validated 8×9 intermediate atlas and motion previews.

- [ ] **Step 1: Generate idle and running-right**

Use isolated `$imagegen` workers; immediately extract and inspect each strip for frame count, clipping, component connectivity, identity, and visible motion.

- [ ] **Step 2: Decide running-left derivation**

Mirror running-right only if hood, face, flame, shading, and direction meaning remain valid; otherwise generate running-left independently.

- [ ] **Step 3: Generate remaining six semantic rows**

Generate waving, jumping, failed, waiting, running, and review independently with canonical-base and layout-guide grounding.

- [ ] **Step 4: Assemble and inspect rows 0–8**

Run extraction, frame inspection, atlas composition, contact-sheet generation, and GIF preview rendering. Repair only a failing containing row.

### Task 4: Generate and validate all look directions

**Files:**
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/qa/look-mechanics.md`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/decoded/look-cardinals.png`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/decoded/look-row-9.png`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/decoded/look-row-10.png`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/qa/direction-semantics.json`

**Interfaces:**
- Consumes: approved standard atlas, canonical base, and cardinal/layout guides.
- Produces: coherent clockwise sixteen-direction gaze family.

- [ ] **Step 1: Define look mechanics**

Keep feet/lower body registered; let eyes lead, head and hood follow, upper torso turn subtly, and the attached flame lag without detaching.

- [ ] **Step 2: Generate and approve four cardinal anchors**

Require unambiguous 000 up, 090 screen-right, 180 down, and 270 screen-left before continuing.

- [ ] **Step 3: Generate and register look row 9**

Generate 000 through 157.5 as one family and run deterministic registration, edge checks, semantics, and continuity review.

- [ ] **Step 4: Generate and register look row 10**

Generate 180 through 337.5 as one family grounded in the approved cardinals and completed row 9; run the same checks.

### Task 5: Assemble, independently QA, package, and install

**Files:**
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/final/spritesheet-extended.webp`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/final/validation-extended.json`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/qa/contact-sheet-extended.png`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/qa/look-directions.png`
- Create: `/Users/kimyong/Documents/temp/uyi-pet-run/qa/run-summary.json`
- Create: `/Users/kimyong/.codex/pets/uyi/pet.json`
- Create: `/Users/kimyong/.codex/pets/uyi/spritesheet.webp`

**Interfaces:**
- Consumes: all approved standard and look rows.
- Produces: installed Codex v2 pet and retained QA evidence.

- [ ] **Step 1: Assemble and clean the extended atlas**

Assemble both gaze rows onto the 8×9 atlas, run the single deterministic chroma despill pass, and require `validate_atlas.py --require-v2` to pass.

- [ ] **Step 2: Run direction and motion QA**

Create contact, focused direction, blind A/B, and continuity artifacts; obtain three isolated blind direction verdicts and one independent final visual verdict.

- [ ] **Step 3: Package and install**

Install only a passing WebP and `pet.json` with `id: "uyi"`, `displayName: "우이"`, and `spriteVersionNumber: 2`.

- [ ] **Step 4: Verify the installed package**

Re-read `pet.json`, validate the installed spritesheet, confirm retained QA artifacts, and report absolute output paths.
