# Visual Bar (hard gate)

The bar is per game, not a fixed global list. Locked refs are usually shipped AAA or high-end captures, and the game must look like it belongs in the same SxS frame. Judge every locked criterion on the current capture set at full resolution, then on the blurred SxS. Every criterion is PASS or FAIL. One failing still fails the criterion. WIN requires every criterion in `art/BAR.md` to PASS.

## Contents

- Building the bar (Stage A)
- Universal core
- Game-specific criteria
- Automatic FAIL (visual)
- Automatic FAIL (process)
- Banned phrases

## Building the bar (Stage A)

1. Start from the universal core below. It is always present.
2. Add game-specific criteria from `BRIEF.md`, the locked refs, and `art/LOOK.md`: what would make a player or reviewer reject this genre's still next to the refs. Prefer criteria you can see in the refs.
3. Write the full list to `art/BAR.md` before Stage B. Each row: id, name, PASS when, FAIL signs, justifying ref ids, and whether it is `core` or `game`.
4. Size band: minimum 5, maximum 15 criteria. The universal core is five. Add game-specific criteria until the refs are covered, without exceeding 15. A bar with only the five core criteria is allowed when the refs do not justify more. Fewer than 5 or more than 15 is a process FAIL.
5. Freeze after Stage A. Mid-loop you may only `BAR-EXPAND` (add criteria) with a logged reason, and only while staying at or under 15. Never remove, merge away, or soften a criterion to exit. Never invent a weaker bar after FAILs.

## Universal core

These five are in every `art/BAR.md`. Wording may adapt for 2D when the locked refs are themselves 2D, but the intent stays.

**C1. Materials and lighting.** PASS when large surfaces show material response (roughness or specular variation, normal or displacement detail, color variation); one readable key light with cast shadows; contact darkening at bases; exposure and grade inside the matched ref's palette; atmosphere consistent with the matched ref. FAIL: flat or unlit surfaces, fullbright, no shadows or contact darkening, toy-plastic palette.

**C2. Geometry and silhouette density.** PASS when matched crops show terrain, prop, and unit breakup comparable to the refs; readable sub-shapes; silhouette count in the same range; no primitive standing in for a real object. FAIL: stamped boxes, empty planes, tile seams, sparse where the ref is dense.

**C3. SxS blur test.** Blur both halves equally until text is unreadable, then downscale. PASS when value structure, palette, density, and light direction read as the same genre and production tier in every pair. FAIL when the game half reads flatter, emptier, or like a web page next to a shipped game.

**C4. Live capture provenance.** PASS when every still and walkthrough frame is from the running build at the delivered commit, at native resolution, with `MANIFEST.md` complete. Only cropping and labeling are allowed after capture. FAIL: mockups, composited plates, retouching, wrong build.

**C5. Walkthrough consistency.** PASS when sampled frames match still quality: no placeholders, no missing textures, no flicker, popping, or z-fighting, steady pacing, relevant UI states shown. FAIL: any of those in motion.

## Game-specific criteria

Pick from what the locked refs actually show, and write PASS when and FAIL signs observables into `art/BAR.md`. These are examples, not a checklist to paste blindly:

- HUD or diegetic UI craft, when refs show designed chrome (fighters, MOBAs, FPS, survival). Skip or narrow it when refs are HUD-light.
- Hero or unit silhouette readability (fighters, MOBAs, stealth predators).
- Cockpit or interior volume (space sims, racing cockpits).
- Far-field or lane density (MOBAs, open battlefields).
- Night grade or threat lighting (horror, night defense).
- Readable gaze, cones, or threat volumes (stealth).
- VFX that lights the scene (fighters, abilities, weapons).
- Wet surfaces and reflections (harbors, rain).
- Crowd or horde density (base defense, RTS).
- Genre signature named from `LOOK.md` (whatever the refs share that a generic bar would miss).

Do not add vague criteria ("feels premium"). Every game-specific criterion needs a PASS when and FAIL signs pair and at least one justifying ref.

## Automatic FAIL (visual)

Never excuse any of these when they apply to the locked bar and refs:

- Any placeholder primitive standing in for a real object in any still or walkthrough frame.
- A flat single-color surface dominating a still where the matched ref shows texture.
- No cast shadows or no contact darkening where the refs have them.
- Default fonts or unstyled form controls on any UI criterion that is in `art/BAR.md`.
- A 2D plate as backdrop where the ref shows 3D space (unless the locked refs are themselves 2D).
- Missing textures, z-fighting, flicker, or popping in the walkthrough.
- Any SxS pair that fails the blur test.
- Stills that are not from the live build at the delivered commit.

## Automatic FAIL (process)

The Orchestrator fixes these before any score counts:

- Refs not locked: no `SOURCES.md`, fewer than four refs, or refs swapped for weaker ones.
- `art/BAR.md` missing, with fewer than 5 or more than 15 criteria, or with criteria that lack PASS and FAIL observables.
- Any `art/LEDGER.md` row still `placeholder` at Stage D.
- Fewer stills than locked refs, or SxS pairs not matched by scene and camera.
- Missing manifest, missing walkthrough, or walkthrough under spec.
- A Critic verdict that scores a different criterion set than `art/BAR.md`, contains a banned phrase, uses numeric scores, or cites fewer stills than delivered.
- Model identifiers or credentials in any artifact.
- Ref imagery found inside the game's asset or build directories.

## Banned phrases

Any of these in a verdict invalidates it. Any of these in a Builder handback or Orchestrator status line is a process FAIL. Rewordings with the same meaning count.

- "fine for a browser toy", "fine for a homage", "fine for a slice", "fine for a prototype"
- "not photoreal, but", "not a AAA remake", "homage, not a remake"
- "stylized low-poly is a valid choice" (when the locked refs are not low-poly stylized)
- "close enough", "good enough", "mostly there", "acceptable for now", "nearly parity"
- "WIN with reservations", "conditional WIN", "soft WIN", "provisional pass", "PASS with notes"
- "given time constraints", "for this round", "considering the stack", "impressive for a web build"
- "gameplay is solid, so", "big improvement over last round" used as grounds for PASS

If the stack cannot reach the bar with current art, change the stack or the art pipeline (see the escalation ladder in `builder-protocol.md`). Never lower the bar. Never delete criteria from `art/BAR.md` to make WIN easier.
