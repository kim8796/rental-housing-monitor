# 우이 Codex Pet Design

## Goal

Create and install a Codex-compatible v2 animated pet named **우이**, grounded in `/Users/kimyong/Downloads/1782207822.jpeg`.

## Visual identity

- Preserve the compact retro pixel-art silhouette.
- Keep the orange reptile body, cream belly, teal-green eyes, black hooded outfit, and attached tail flame.
- Exclude the source image's striped backdrop, dialogue frame, and text.
- Keep the character readable inside a 192×208 sprite cell with crisp, consistent pixel clusters.

## Animation contract

Produce an 8×11 v2 atlas containing idle, running right, running left, waving, jumping, failed, waiting, active work, and review rows, followed by sixteen clockwise look directions. Directional running uses body and limb motion without speed lines or detached effects. The look loop uses eye, head, hood, and upper-body turns while the feet and lower body remain registered; the flame stays attached and follows the turn subtly.

## Pipeline and QA

Generate the base and pose strips from the supplied reference on a removable chroma background. Assemble cells deterministically, validate frame geometry and transparency, review contact sheets and motion previews, verify all cardinal gaze directions independently, and package only a passing `spriteVersionNumber: 2` atlas under the Codex pets directory.

## Deliverables

- Installed Codex pet package named `우이`
- Final v2 WebP spritesheet
- Validation report, contact sheet, direction sheet, motion previews, and run summary
